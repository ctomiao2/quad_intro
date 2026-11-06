"""实盘数据存储：backtest 数据快照的一次性副本 + 每日收盘行情追加。

设计要点：
- 与 backtest/data.py 完全同构（etf_daily/<code>.csv 宽表、utf-8-sig、date 为
  YYYYMMDD 整数、adj_* 前复权列），因此实盘可直接用 backtest.DataFeed 读取，
  保证信号计算路径与回测一致；
- 隔离：回测读写 D:/Learn/quad_intro 下的原始快照，实盘只读写 live/data/，
  互不影响；
- 前复权列每次写入后按 adj_factors.json 全量重算（公式与导出脚本一致：
  adj = raw / prod(ratio for event_date > 当日)）。若未来发生新的拆分/合股，
  只需在 live/data/adj_factors.json 登记事件，历史 adj 列自动重算。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd

SNAPSHOT_FILES = ["etf_pool.csv", "adj_factors.json"]
SNAPSHOT_DIRS = ["etf_daily", "index_daily"]


class LiveStore:
    def __init__(self, live_root: Path):
        self.root = Path(live_root)
        self.data = self.root / "data"

    # ---- 部署 ----
    def bootstrap(self, src_root: Path) -> None:
        """首次部署：复制数据快照。已初始化则拒绝，防止覆盖实盘数据。"""
        if self.data.exists():
            raise SystemExit(f"[store] {self.data} 已存在，拒绝重复初始化（如需重置请手动删除）")
        src = Path(src_root)
        self.data.mkdir(parents=True)
        for name in SNAPSHOT_FILES:
            shutil.copy2(src / name, self.data / name)
        for d in SNAPSHOT_DIRS:
            shutil.copytree(src / d, self.data / d)
        print(f"[store] 数据快照已复制到 {self.data}")

    # ---- 工具 ----
    def _events(self) -> dict[str, list[tuple[pd.Timestamp, float]]]:
        with open(self.data / "adj_factors.json", encoding="utf-8") as f:
            raw = json.load(f)
        return {
            code: [(pd.to_datetime(str(e["date"])), float(e["ratio"])) for e in ev]
            for code, ev in raw.items()
        }

    @staticmethod
    def rebuild_adj(df: pd.DataFrame, events: list[tuple[pd.Timestamp, float]]) -> pd.DataFrame:
        """按导出脚本公式重算 adj_* 列：adj = raw / prod(事件日 > 当日的 ratio)。"""
        if "adj_open" not in df.columns:
            return df
        cum = pd.Series(1.0, index=df.index)
        for ev_date, ratio in events:
            cum = cum * df.index.to_series().apply(lambda d, ed=ev_date: ratio if d < ed else 1.0)
        for raw, adj in [("open", "adj_open"), ("high", "adj_high"), ("low", "adj_low"), ("close", "adj_close")]:
            if adj in df.columns:
                df[adj] = df[raw] / cum
        return df

    def upsert_row(self, csv_path: Path, code: str, day: pd.Timestamp, row: dict) -> str:
        """写入某标的当日整行。

        写入规则（沿用 quad-live 铁律）：
        - 当日无该行 -> 追加（含补洞）；
        - 当日 == 今天且已有行 -> 用定稿行替换（收盘任务重跑时）；
        - 当日 < 今天且已有行 -> 跳过（历史行严禁覆盖）。
        返回 'appended' / 'finalized' / 'skipped' / 'rejected'。
        """
        # 与 backtest.data 相同的脏行规则：high/low 与 open/close 矛盾则拒绝
        if not (row["high"] >= max(row["open"], row["close"]) - 1e-9
                and row["low"] <= min(row["open"], row["close"]) + 1e-9):
            print(f"[store] {code} {day:%Y%m%d} 行情异常(high/low 矛盾)，跳过: {row}")
            return "rejected"

        df = pd.read_csv(csv_path, encoding="utf-8-sig", parse_dates=["date"], index_col="date")
        df = df[~df.index.duplicated(keep="last")].sort_index()
        today = pd.Timestamp.now().normalize()
        if day in df.index and day < today:
            return "skipped"
        action = "finalized" if day in df.index else "appended"
        values = {c: row.get(c, 0.0) for c in ["open", "high", "low", "close", "volume", "amount"]}
        if "adj_open" in df.columns:  # adj 列由 rebuild_adj 重算，先占位
            values.update({"adj_open": None, "adj_high": None, "adj_low": None, "adj_close": None})
        df.loc[day, list(values.keys())] = values  # 当日无则追加，有则覆盖
        df["code"] = code
        df = self.rebuild_adj(df, self._events().get(code, []))
        # 写回时保持快照的 date 格式（YYYYMMDD 整数串），避免 pandas 往返后变成 ISO 日期
        out = df.copy()
        out.index = out.index.strftime("%Y%m%d")
        out.to_csv(csv_path, encoding="utf-8-sig")
        return action

    def upsert_close(self, day: pd.Timestamp, etf_rows: dict, index_rows: dict) -> tuple[int, int]:
        """收盘任务/补洞：把 ETF 与指数行情整行入库，返回 (写入数, 跳过数)。"""
        ok = skip = 0
        for code, q in etf_rows.items():
            path = self.data / "etf_daily" / f"{code}.csv"
            if not path.exists():  # 不在池内的标的不建文件
                continue
            action = self.upsert_row(path, code, day, q)
            ok += action in ("appended", "finalized")
            skip += action in ("skipped", "rejected")
        for code, q in index_rows.items():
            path = self.data / "index_daily" / f"{code}.csv"
            if path.exists():
                action = self.upsert_row(path, code, day, q)
                ok += action in ("appended", "finalized")
                skip += action in ("skipped", "rejected")
        return ok, skip

    def last_etf_date(self) -> pd.Timestamp | None:
        """全池 ETF 数据的最后完整日期（取各文件最后日期的最小值）。"""
        import glob

        lasts = []
        for path in glob.glob(str(self.data / "etf_daily" / "*.csv")):
            df = pd.read_csv(path, encoding="utf-8-sig", usecols=["date"])
            if len(df):
                lasts.append(pd.to_datetime(str(int(df["date"].iloc[-1]))))
        return min(lasts) if lasts else None
