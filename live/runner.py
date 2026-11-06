"""实盘任务：开盘打分出指令 / 收盘入库记净值 / 成交登记 / 状态与自检。

与回测的一致性保证（用户核心要求）：
1. 策略类与撮合代码直接 import backtest 包（同一份代码，不做任何复制）；
2. 策略参数来自 live/config.json，selfcheck 校验其与 run_backtest.py 默认值一致；
3. 指令生成直接调用 BacktestEngine._rebalance（与回测完全相同的先卖后买、
   整手、目标权重算法），在「克隆组合」上执行——实盘账户状态不因建议单而变动；
4. 数据格式与快照同构（live/store.py），信号计算走同一条 DataFeed 路径。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from backtest.broker import Portfolio, Position
from backtest.data import DataFeed
from backtest.engine import BacktestEngine
from backtest.strategy import Context, MomentumRotationStrategy

from . import quotes
from .store import LiveStore

# 与 run_backtest.py 的 argparse 默认值严格一致（cmd_selfcheck 会校验，改动需两边同步）
BACKTEST_DEFAULT_STRATEGY = {
    "lookback": 25,
    "top_n": 2,
    "min_score": 0.0,
    "mode": "sharpe",
    "freq": "W",
}

INDEX_CODES = ["000001.SH", "399001.SZ", "399006.SZ"]


def load_config(live_root: Path) -> dict:
    cfg = json.loads((live_root / "config.json").read_text(encoding="utf-8"))
    cfg.setdefault("trading", {})
    return cfg


def setup_logging(live_root: Path) -> None:
    log_dir = live_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_dir / f"live_{datetime.now():%Y%m}.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def log(msg: str) -> None:
    logging.info(msg)


# ---------------- 账户状态 ----------------

def _positions_path(state_dir: Path) -> Path:
    return state_dir / "positions.json"


def load_portfolio(cfg: dict, live_root: Path) -> Portfolio:
    """从 state/positions.json 恢复账户；不存在则按 config 初始化。"""
    state = live_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    acct = cfg["account"]
    pf = Portfolio(acct["cash"], commission_rate=acct.get("commission_rate", 1e-4),
                   lot=cfg["trading"].get("lot", 100))
    path = _positions_path(state)
    if path.exists():
        obj = json.loads(path.read_text(encoding="utf-8"))
        pf.cash = float(obj["cash"])
        for code, p in obj.get("positions", {}).items():
            pf.positions[code] = Position(code, int(p["shares"]), float(p["cost"]))
    else:
        save_portfolio(pf, live_root)
    return pf


def save_portfolio(pf: Portfolio, live_root: Path) -> None:
    path = _positions_path(live_root / "state")
    obj = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "init_cash": pf.init_cash,
        "cash": round(pf.cash, 2),
        "positions": {
            c: {"shares": p.shares, "cost": round(p.cost, 2)} for c, p in pf.positions.items()
        },
    }
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _clone_portfolio(pf: Portfolio) -> Portfolio:
    p2 = Portfolio(pf.init_cash, pf.commission_rate, pf.min_commission, pf.lot)
    p2.cash = pf.cash
    for c, p in pf.positions.items():
        p2.positions[c] = Position(c, p.shares, p.cost)
    return p2


def _append_trades_log(trades: list, live_root: Path) -> None:
    path = live_root / "state" / "trades.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for t in trades:
            f.write(json.dumps(t.as_dict(), ensure_ascii=False) + "\n")


# ---------------- 开盘任务 ----------------

def cmd_open(live_root: Path, cfg: dict) -> int:
    today_d = date.today()
    state_dir = live_root / "state"
    if not quotes.is_trade_date(today_d, state_dir):
        log(f"{today_d:%Y-%m-%d} 非交易日，开盘任务跳过")
        return 0

    now = datetime.now()
    if not (now.hour, now.minute) >= (9, 25) or now.hour >= 15:
        log(f"提示: 当前 {now:%H:%M} 不在开盘时段(09:25~15:00)，指令仅供补跑参考")

    store = LiveStore(live_root)
    feed = DataFeed(store.data)
    strategy = MomentumRotationStrategy(**cfg["strategy"])
    strategy.prepare(feed)
    last = feed.calendar[-1]
    prev_td = quotes.prev_trade_date(today_d, state_dir)
    if prev_td and last < pd.Timestamp(prev_td):
        log(f"警告: 实盘数据截止 {last:%Y-%m-%d}，落后于上一交易日 {prev_td}，"
            f"请先运行 python live.py backfill 补数据")

    # 1) 抓开盘行情（腾讯批量 -> 东财单只兜底）
    spot = quotes.fetch_spot_tencent(feed.universe + INDEX_CODES)
    etf_open = quotes.fetch_etf_open_with_fallback(feed.universe, spot)
    if cfg.get("benchmark") not in spot:
        log(f"警告: 基准 {cfg.get('benchmark')} 实时行情缺失")
    snapshot = {
        "date": today_d.strftime("%Y-%m-%d"),
        "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "benchmark": {
            "close": spot.get(cfg.get("benchmark"), {}).get("close"),
            "prev_close": spot.get(cfg.get("benchmark"), {}).get("prev_close"),
        },
        "etf": {c: {k: v for k, v in r.items()} for c, r in etf_open.items()},
    }
    (state_dir / f"open_{today_d:%Y%m%d}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log(f"开盘行情已抓取: ETF {len(etf_open)}/{len(feed.universe)} 只, 快照存至 state/open_{today_d:%Y%m%d}.json")

    # 2) 判断今日是否调仓日（跨进程持久化上次调仓日，与回测共用周期规则）
    rb_path = state_dir / "rebalance_state.json"
    last_rb = None
    if rb_path.exists():
        last_rb = pd.Timestamp(json.loads(rb_path.read_text(encoding="utf-8"))["last_rebalance_date"])
    if not strategy.is_rebalance_day(pd.Timestamp(today_d), last_rb):
        log(f"{today_d:%Y-%m-%d} 非调仓日（本周已于 {last_rb:%Y-%m-%d} 调仓，freq={cfg['strategy'].get('freq')}），今日无操作")
        return 0
    rb_path.write_text(
        json.dumps({"last_rebalance_date": str(today_d)}, ensure_ascii=False), encoding="utf-8"
    )

    # 3) 策略打分（数据截至昨收，无未来函数）
    pf = load_portfolio(cfg, live_root)
    ctx = Context(date=pd.Timestamp(today_d), prev_date=last, portfolio=pf, feed=feed)
    targets = strategy.on_date(ctx)
    if targets is None:
        log(f"{today_d:%Y-%m-%d} 为非调仓日（freq={cfg['strategy'].get('freq')}），今日无操作")
        return 0
    if not targets:
        log("今日调仓: 全部标的动量分数不达标 -> 目标空仓")
    else:
        picked = ", ".join(f"{c}({feed.names.get(c, '')})" for c in targets)
        log(f"今日调仓: 目标持仓 {picked}")

    # 3) 用引擎同一段撮合代码在克隆组合上生成指令（不动真实账户状态）
    pf2 = _clone_portfolio(pf)
    engine = BacktestEngine(
        feed, strategy, pf2, start=str(last), end=str(last),
        min_order_value=cfg["trading"].get("min_order_value", 500.0),
    )
    closes = feed.panel("close").ffill().loc[last]
    opens = {}
    for c in pf.positions:  # 持仓缺当日开盘价 -> NaN，引擎按「不可交易」顺延处理
        opens[c] = float(etf_open[c]["open"]) if c in etf_open else float("nan")
    opens.update({c: r["open"] for c, r in etf_open.items()})  # 当日有效开盘价
    engine._rebalance(pd.Timestamp(today_d), targets, pd.Series(opens), closes)

    if not pf2.trades:
        log("目标持仓与当前持仓一致（或调仓金额过小），无需交易")
        return 0

    # 4) 输出交易指令（人工在券商端执行，成交后用 fill 登记真实回报）
    log("=" * 60)
    log(f"今日交易指令 {today_d:%Y-%m-%d}（按开盘价参考，整手申报）:")
    orders = []
    for t in pf2.trades:
        name = feed.names.get(t.code, "")
        est_fee = max(t.value * pf2.commission_rate, 0)
        log(f"  {t.side} {t.code} {name}: {t.shares} 股 @ {t.price:.3f}"
            f" ≈ {t.value:,.0f} 元 (费≈{est_fee:.0f})")
        orders.append({"code": t.code, "name": name, "side": t.side,
                       "shares": t.shares, "ref_price": t.price, "value": t.value})
    log("=" * 60)
    log("成交后请登记: python live.py fill --code <代码> --side <买|卖> --shares <股数> --price <成交价>")
    (live_root / "logs" / f"orders_{today_d:%Y%m%d}.json").write_text(
        json.dumps({"date": str(today_d), "targets": targets, "orders": orders},
                   ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


# ---------------- 收盘任务 ----------------

def cmd_close(live_root: Path, cfg: dict, force: bool = False) -> int:
    today_d = date.today()
    state_dir = live_root / "state"
    if not quotes.is_trade_date(today_d, state_dir):
        log(f"{today_d:%Y-%m-%d} 非交易日，收盘任务跳过")
        return 0
    now = datetime.now()
    if now.time().strftime("%H:%M") < "15:00" and not force:
        log(f"当前 {now:%H:%M} 未到收盘(15:00)，行情未定稿。确需运行请加 --force")
        return 1

    store = LiveStore(live_root)
    feed = DataFeed(store.data)
    today_ts = pd.Timestamp(today_d)

    # 1) 抓当日定稿行情：东财日K(官方定稿) -> 腾讯批量兜底 -> 新浪兜底
    spot = quotes.fetch_spot_tencent(feed.universe + INDEX_CODES)
    today_str = today_d.strftime("%Y%m%d")

    def row_for(code: str, prev_close: float | None) -> dict | None:
        """ETF 行：东财日K -> 腾讯 -> 新浪，过三重校验（量额自洽+价格偏离）。"""
        rec = src = None
        rows = quotes.fetch_daily_em(code, limit=5)
        rec = next((r for r in rows if r["date"] == today_str), None)
        if rec is None:
            t = spot.get(code)
            if t and t["close"] > 0:
                rec = {"date": today_str, **{k: t[k] for k in
                                             ("open", "high", "low", "close", "volume", "amount")}}
                rec["prev_close"] = t["prev_close"]
                src = "腾讯实时"
        if rec is None:
            sk = [r for r in quotes.fetch_kline_sina(code) if r["date"] == today_str]
            if sk:
                rec, src = sk[-1], "新浪kline"
        if rec is None:
            print(f"[close] {code} 当日无可用行情，跳过")
            return None
        base = rec.get("prev_close", prev_close)
        if not quotes.sanity_ok(rec["close"], rec["amount"], base, rec["volume"]):
            print(f"[close] {code} 校验不通过(close={rec['close']} prev={base})，跳过")
            return None
        return rec

    def index_row_for(code: str) -> dict | None:
        """指数行：东财日K -> 腾讯。指数的量/额是两种统计口径，量额自洽校验不适用，
        只校验收盘>0 与较昨收偏离（quad-live UpdateIndexData 同样不对指数做量额校验）。"""
        rows = quotes.fetch_daily_em(code, index=True, limit=5)
        rec = next((r for r in rows if r["date"] == today_str), None)
        prev = next((r["close"] for r in rows if r["date"] < today_str), None)
        if rec is None:
            t = spot.get(code)
            if t and t["close"] > 0:
                rec = {"date": today_str, **{k: t[k] for k in
                                             ("open", "high", "low", "close", "volume", "amount")}}
                prev = t["prev_close"]
        if rec is None or rec["close"] <= 0:
            print(f"[close] 指数 {code} 当日无可用行情，跳过")
            return None
        if prev and abs(rec["close"] / prev - 1) > 0.30:
            print(f"[close] 指数 {code} 价格异常({rec['close']} vs 昨收 {prev})，跳过")
            return None
        return rec

    closes_panel = feed.panel("close").ffill()
    last = feed.calendar[-1]
    prev_closes = closes_panel.loc[last]

    etf_rows, idx_rows = {}, {}
    for code in feed.universe:
        rec = row_for(code, prev_close=float(prev_closes.get(code, 0)) or None)
        if rec:
            etf_rows[code] = rec
    for code in INDEX_CODES:
        rec = index_row_for(code)
        if rec:
            idx_rows[code] = rec

    ok, skip = store.upsert_close(today_ts, etf_rows, idx_rows)
    log(f"收盘入库: 写入 {ok} 行, 跳过 {skip} 行（数据截至 {today_d:%Y-%m-%d}）")

    # 2) 记录实盘净值
    feed2 = DataFeed(store.data)  # 重读，含当日
    closes_today = feed2.panel("close").ffill().loc[today_ts]
    pf = load_portfolio(cfg, live_root)
    mv = pf.market_value(closes_today)
    total = pf.cash + mv
    # 基准收盘取刚入库的指数行情（指数不在 ETF 面板中）
    bench_close = float(idx_rows.get(cfg.get("benchmark"), {}).get("close", float("nan")))
    base = pf.init_cash
    nav_path = state_dir / "nav_log.csv"
    header = "date,cash,market_value,total,nav,benchmark_close"
    new_row = (f"{today_d:%Y-%m-%d},{pf.cash:.2f},{mv:.2f},{total:.2f},"
               f"{total / base:.6f},{bench_close:.2f}")
    rows: list[str] = []
    if nav_path.exists():
        rows = [
            l.strip() for l in nav_path.read_text(encoding="utf-8-sig").splitlines()
            if l.strip() and not l.startswith("date") and not l.startswith(str(today_d))
        ]
    rows.append(new_row)
    nav_path.write_text("\n".join([header] + rows) + "\n", encoding="utf-8-sig")
    holds = ", ".join(f"{c}×{p.shares}" for c, p in pf.positions.items()) or "空仓"
    log(f"实盘净值: 总资产 {total:,.0f} 元 (nav={total / base:.4f}), 持仓: {holds}, "
        f"基准收盘 {bench_close:.2f}")
    return 0


# ---------------- 补洞 ----------------

def cmd_backfill(live_root: Path, cfg: dict, days: int = 10) -> int:
    """用东财日K补最近 N 天缺失的行（只补「今天之前」的缺失日期，当日行归收盘任务）。"""
    store = LiveStore(live_root)
    feed = DataFeed(store.data)
    last = feed.calendar[-1]
    today_ts = pd.Timestamp.now().normalize()
    log(f"补洞: 当前数据截止 {last:%Y-%m-%d}，抓取最近 {days} 天")
    limit = max(days + 5, 15)
    for code in feed.universe:
        for r in quotes.fetch_daily_em(code, limit=limit):
            d = pd.Timestamp(r["date"])
            if last < d < today_ts:
                store.upsert_row(store.data / "etf_daily" / f"{code}.csv", code, d, r)
    for code in INDEX_CODES:
        for r in quotes.fetch_daily_em(code, index=True, limit=limit):
            d = pd.Timestamp(r["date"])
            if last < d < today_ts:
                store.upsert_row(store.data / "index_daily" / f"{code}.csv", code, d, r)
    feed2 = DataFeed(store.data)
    log(f"补洞完成: 数据截止 {feed2.calendar[-1]:%Y-%m-%d}")
    return 0


# ---------------- 成交登记 / 状态 ----------------

def cmd_fill(live_root: Path, cfg: dict, code: str, side: str, shares: int, price: float,
             trade_date: str | None = None) -> int:
    pf = load_portfolio(cfg, live_root)
    d = pd.Timestamp(trade_date) if trade_date else pd.Timestamp.now().normalize()
    side = "买" if side in ("买", "买入", "B", "b") else "卖"
    n0 = len(pf.trades)
    if side == "买":
        pf.buy(code, int(shares), float(price), d)
    else:
        pf.sell(code, int(shares), float(price), d)
    save_portfolio(pf, live_root)
    _append_trades_log(pf.trades[n0:], live_root)
    log(f"已登记: {side} {code} {shares} 股 @ {price}（手续费与回测口径一致）")
    log(f"当前现金 {pf.cash:,.2f} 元, 持仓 {', '.join(f'{c}×{p.shares}' for c, p in pf.positions.items()) or '空仓'}")
    return 0


def cmd_status(live_root: Path, cfg: dict) -> int:
    store = LiveStore(live_root)
    feed = DataFeed(store.data)
    pf = load_portfolio(cfg, live_root)
    last = feed.calendar[-1]
    closes = feed.panel("close").ffill().loc[last]
    print(f"数据截止: {last:%Y-%m-%d} | 策略参数: {cfg['strategy']} | 初始资金: {pf.init_cash:,.0f}")
    total = pf.cash
    for c, p in pf.positions.items():
        px = float(closes[c])
        val = p.shares * px
        total += val
        pnl = val / p.cost - 1 if p.cost > 0 else 0.0
        print(f"  持仓 {c} {feed.names.get(c, '')}: {p.shares} 股, 市值 {val:,.0f}, 浮动盈亏 {pnl * 100:+.2f}%")
    print(f"现金 {pf.cash:,.2f} | 总资产 {total:,.2f} (nav={total / pf.init_cash:.4f})")
    nav_path = live_root / "state" / "nav_log.csv"
    if nav_path.exists():
        nav = pd.read_csv(nav_path, parse_dates=["date"], index_col="date")
        print(f"净值记录 {len(nav)} 天, 最新 {nav.index[-1]:%Y-%m-%d} nav={nav['nav'].iloc[-1]:.4f}")
        # 与回测同期净值对比（若存在）
        bt_path = live_root.parent / "results" / "nav.csv"
        if bt_path.exists() and len(nav):
            bt = pd.read_csv(bt_path, parse_dates=["date"], index_col="date")
            common = nav.index.intersection(bt.index)
            if len(common):
                b0, l0 = bt.loc[common[0], "nav"], nav.loc[common[0], "nav"]
                gap = nav["nav"].iloc[-1] / l0 - bt.loc[common[-1], "nav"] / b0
                print(f"同期(自 {common[0]:%Y-%m-%d}) 实盘 vs 回测净值差: {gap * 100:+.2f}%")
    return 0


# ---------------- 一致性自检 ----------------

def cmd_selfcheck(live_root: Path, cfg: dict, replay_n: int = 30) -> int:
    """实盘与回测一致性自检。"""
    failures = []

    # 1) 策略参数与回测默认值一致
    if cfg["strategy"] != BACKTEST_DEFAULT_STRATEGY:
        failures.append(f"策略参数与回测默认值不一致: {cfg['strategy']} != {BACKTEST_DEFAULT_STRATEGY}")
    else:
        print("[ok] 策略参数与 run_backtest.py 默认值一致")

    # 2) 池/拆分事件文件与快照一致（部署以来未被改动）
    import hashlib

    for name in ("etf_pool.csv", "adj_factors.json"):
        h1 = hashlib.md5((live_root / "data" / name).read_bytes()).hexdigest()
        h2 = hashlib.md5((live_root.parent / name).read_bytes()).hexdigest()
        if h1 != h2:
            failures.append(f"{name} 与回测快照不一致（实盘副本被改动或快照已更新）")
        else:
            print(f"[ok] {name} 与回测快照一致")

    # 3) adj 前复权列重算结果 == 存储值（公式与导出脚本一致）
    store = LiveStore(live_root)
    events = store._events()
    worst = 0.0
    for path in sorted((store.data / "etf_daily").glob("*.csv")):
        df = pd.read_csv(path, encoding="utf-8-sig", parse_dates=["date"], index_col="date")
        rec = store.rebuild_adj(df.copy(), events.get(path.stem, []))
        for raw, adj in [("open", "adj_open"), ("close", "adj_close")]:
            worst = max(worst, float((rec[adj] - df[adj]).abs().max()))
    if worst > 1e-6:
        failures.append(f"adj 重算与存储不一致, 最大偏差 {worst}")
    else:
        print(f"[ok] adj 前复权列重算与存储一致 (最大偏差 {worst:.2e})")

    # 4) 信号回放: 实盘策略在回测成交日给出的目标 == 回测成交后的持仓
    bt_trades_path = live_root.parent / "results" / "trades.csv"
    if bt_trades_path.exists():
        bt = pd.read_csv(bt_trades_path, parse_dates=["date"])
        bt_dates = sorted(bt["date"].unique())[-replay_n:]
        # 逐日累计持仓（回测成交后口径）。重建时必须感知拆分事件：
        # 引擎在拆分日开盘前把持仓股数 ×ratio（否则股数对不齐、会残留幽灵持仓）
        ev_list = sorted(
            (ed, code, ratio)
            for code, evs in store._events().items()
            for ed, ratio in evs
        )
        applied: set = set()
        cur: dict[str, int] = {}
        traj: dict[pd.Timestamp, set] = {}
        for _, t in bt.sort_values("date").iterrows():
            d = t["date"]
            # 先补齐此笔成交前已生效、尚未应用的拆分（拆分日当天可能没有成交记录）
            for key in ev_list:
                ed, code, ratio = key
                if ed <= d and key not in applied:
                    applied.add(key)
                    if code in cur:
                        cur[code] = int(round(cur[code] * ratio))
            delta = t["shares"] if t["side"] == "买入" else -t["shares"]
            cur[t["code"]] = cur.get(t["code"], 0) + delta
            if cur[t["code"]] == 0:
                cur.pop(t["code"])
            traj[d] = set(cur.keys())
        feed = DataFeed(store.data)
        mismatch = 0
        for d in bt_dates:
            d = pd.Timestamp(d)
            if d not in feed.calendar:
                continue
            prev = feed.calendar[feed.calendar < d][-1]
            strat = MomentumRotationStrategy(**cfg["strategy"])
            strat.prepare(feed)
            targets = strat.on_date(Context(date=d, prev_date=prev, portfolio=None, feed=feed))
            expect = traj.get(d, set())
            got = set(targets.keys()) if targets else (set() if targets == {} else None)
            if targets is None:
                failures.append(f"回放 {d:%Y-%m-%d} 策略未产生调仓信号(预期调仓)")
                mismatch += 1
            elif got != expect:
                failures.append(f"回放 {d:%Y-%m-%d} 目标 {sorted(got)} != 回测持仓 {sorted(expect)}")
                mismatch += 1
        if mismatch == 0:
            print(f"[ok] 信号回放: 最近 {len(bt_dates)} 个回测调仓日, 实盘策略目标与回测持仓全部一致")
    else:
        print("[skip] 未找到回测成交明细 results/trades.csv, 跳过信号回放")

    if failures:
        print("\n== 自检未通过 ==")
        for f in failures:
            print(" -", f)
        return 1
    print("\n== 自检全部通过: 实盘与回测策略/配置/数据一致 ==")
    return 0
