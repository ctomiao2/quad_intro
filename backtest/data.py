"""数据层：加载并清洗 ETF 日线（原始价 + 前复权价）、指数行情与拆分事件。

数据来自 quad 目录导出的只读快照（字段与坑点见 quad_intro/README.md）：
- etf_daily/<code>.csv     单只 ETF 日线宽表，含 adj_* 前复权列
- index_daily/<code>.csv   指数日线宽表
- etf_pool.csv             ETF 池清单（disabled=1 的标的不纳入交易池）
- adj_factors.json         拆分/合股事件表

本层对 README「已知坑点」的处理：
- 脏行：high < max(open, close) 或 low > min(open, close) 的行直接丢弃；
- 上市时间不一：交易日历取全池并集，缺失位置为 NaN（不填充）；
- 拆分断崖：信号层一律用 adj_* 前复权价，撮合层用原始价并在拆分日调整股数。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _read_daily_csv(path: Path) -> pd.DataFrame:
    """读单只日线宽表：去重排序 + 丢弃 high/low 与 open/close 矛盾的脏行。"""
    df = pd.read_csv(path, encoding="utf-8-sig", parse_dates=["date"], index_col="date")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    ok = (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-9) & (
        df["low"] <= df[["open", "close"]].min(axis=1) + 1e-9
    )
    if (~ok).any():
        bad = df.index[~ok].strftime("%Y%m%d").tolist()
        print(f"[data] {path.stem}: 丢弃脏行 {bad}")
        df = df[ok]
    return df


class DataFeed:
    """统一数据入口：交易日历 + 行情面板 + 拆分事件 + ETF 名称。"""

    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        pool = pd.read_csv(self.root / "etf_pool.csv", encoding="utf-8-sig", dtype={"code": str})
        self.names = dict(zip(pool["code"], pool["name"]))
        # disabled=1 的标的（当前策略禁用）不进入交易池
        self.universe = sorted(pool.loc[pool["disabled"].fillna(0).astype(int) != 1, "code"])

        with open(self.root / "adj_factors.json", encoding="utf-8") as f:
            raw = json.load(f)
        # {code: [(生效日, ratio), ...]}，生效日当日开盘价为拆分后价格
        self.splits: dict[str, list[tuple[pd.Timestamp, float]]] = {
            code: [(pd.to_datetime(str(e["date"])), float(e["ratio"])) for e in events]
            for code, events in raw.items()
        }

        self.daily = {
            code: _read_daily_csv(self.root / "etf_daily" / f"{code}.csv")
            for code in self.universe
        }
        # 交易日历 = 全池交易日并集
        self.calendar = pd.DatetimeIndex(
            sorted(set().union(*(d.index for d in self.daily.values())))
        )
        self._panels: dict[str, pd.DataFrame] = {}

    def panel(self, field: str) -> pd.DataFrame:
        """行情宽表（行=交易日历，列=ETF 代码，缺失为 NaN），按字段缓存。"""
        if field not in self._panels:
            df = pd.DataFrame({c: d[field] for c, d in self.daily.items()})
            self._panels[field] = df.reindex(self.calendar)
        return self._panels[field]

    def index_close(self, code: str) -> pd.Series:
        """指数收盘价序列（用作基准对比）。"""
        return _read_daily_csv(self.root / "index_daily" / f"{code}.csv")["close"]
