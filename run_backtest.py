"""ETF 周频动量轮动策略回测入口。

用法示例：
    python run_backtest.py
    python run_backtest.py --start 20200101 --end 20231231 --lookback 60 --top-n 3
    python run_backtest.py --freq D --mode return --min-score -999

策略规则：
- 每个交易日开盘时，用截至昨收的前复权数据给全池 ETF 打动量分；
- 每周第一个交易日调仓：持有分数最高的 top_n 只（等权），卖出跌出名单者；
- 分数 <= min_score 的标的不买（默认 0，即动量为负时空仓等待）；
- 初始资金 10 万，按开盘价整手（100 股）成交，收取佣金。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtest.broker import Portfolio
from backtest.data import DataFeed
from backtest.dashboard import save_dashboard
from backtest.engine import BacktestEngine
from backtest.metrics import summarize, yearly_returns
from backtest.report import print_summary, save_all
from backtest.strategy import MomentumRotationStrategy

ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ETF 动量轮动回测")
    p.add_argument("--start", default="20210101", help="回测起始日 YYYYMMDD，默认 20210101")
    p.add_argument("--end", default=None, help="回测结束日 YYYYMMDD，默认数据最后一天")
    p.add_argument("--lookback", type=int, default=25, help="动量回看交易日数，默认 25")
    p.add_argument("--top-n", type=int, default=2, help="持仓数量，默认 2")
    p.add_argument("--min-score", type=float, default=0.0, help="最低入选分数，默认 0")
    p.add_argument("--mode", default="sharpe", choices=["sharpe", "return"], help="打分模式")
    p.add_argument("--freq", default="W", choices=["W", "M", "D"], help="调仓频率，默认每周")
    p.add_argument("--cash", type=float, default=100000.0, help="初始资金（元），默认 10 万")
    p.add_argument("--commission", type=float, default=1e-4, help="单边佣金率，默认万 1")
    p.add_argument("--benchmark", default="000001.SH", help="基准指数代码")
    p.add_argument("--out", default=str(ROOT / "results"), help="结果输出目录")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    feed = DataFeed(ROOT)
    strategy = MomentumRotationStrategy(
        lookback=args.lookback,
        top_n=args.top_n,
        min_score=args.min_score,
        mode=args.mode,
        freq=args.freq,
    )
    strategy.prepare(feed)
    pf = Portfolio(cash=args.cash, commission_rate=args.commission)

    engine = BacktestEngine(feed, strategy, pf, start=args.start, end=args.end)
    nav, trades = engine.run()

    # 基准：指数收盘价归一到回测起点
    bench = feed.index_close(args.benchmark).reindex(nav.index).ffill()
    bench_nav = bench / bench.iloc[0]
    bench_name = {"000001.SH": "上证指数", "399001.SZ": "深证成指", "399006.SZ": "创业板指"}.get(
        args.benchmark, args.benchmark
    )

    m = summarize(nav["nav"], trades)
    bm = summarize(bench_nav)
    bm["年度收益"] = yearly_returns(bench_nav)

    # 期末持仓快照
    last_close = feed.panel("close").loc[nav.index[-1]]
    rows = []
    for pos in pf.positions.values():
        value = pos.shares * float(last_close[pos.code])
        rows.append(
            {
                "code": pos.code,
                "name": feed.names.get(pos.code, ""),
                "shares": pos.shares,
                "value": value,
                "weight": value / nav["total"].iloc[-1],
                "pnl_pct": value / pos.cost - 1 if pos.cost > 0 else 0.0,
            }
        )
    holdings = pd.DataFrame(rows)

    print_summary(m, nav["nav"], pf.init_cash, bench_name, bm, holdings)

    title = (
        f"ETF 动量轮动（{args.freq} 频率, lookback={args.lookback}, 持仓 {args.top_n} 只）"
        f" vs {bench_name}"
    )
    png = save_all(Path(args.out), nav, trades, bench_nav, title, bench_name)
    freq_cn = {"W": "周频", "M": "月频", "D": "日频"}[args.freq]
    params_desc = (
        f"{freq_cn}动量轮动 · lookback={args.lookback} · 持仓 {args.top_n} 只 · "
        f"初始 {args.cash / 10000:.0f} 万 · 佣金万 {args.commission * 10000:.0f}"
    )
    dash = save_dashboard(
        Path(args.out), nav, bench_nav, m, bm,
        title="ETF 动量轮动回测看板", bench_name=bench_name,
        params_desc=params_desc, init_cash=pf.init_cash,
    )
    print(f"\n结果已输出到 {Path(args.out).resolve()}:")
    print(f"  - {Path(args.out) / 'nav.csv'}（每日净值）")
    print(f"  - {Path(args.out) / 'trades.csv'}（成交明细）")
    print(f"  - {png.name}（净值曲线图）")
    print(f"  - {dash.name}（HTML 看板，浏览器打开查看）")


if __name__ == "__main__":
    main()
