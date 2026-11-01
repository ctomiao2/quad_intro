"""结果输出：控制台摘要 + results/ 目录下的净值 CSV、成交 CSV、净值曲线图。"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无窗口环境（Git Bash / 服务器）下渲染
import matplotlib.pyplot as plt
import pandas as pd

from .metrics import drawdown_series, yearly_returns

# 中文字体（Windows 自带微软雅黑；缺字体时标签会显示方框）
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def print_summary(
    m: dict,
    nav: pd.Series,
    init_cash: float,
    bench_name: str | None = None,
    bench_m: dict | None = None,
    holdings: pd.DataFrame | None = None,
) -> None:
    line = "=" * 56
    print(line)
    print("回测结果摘要")
    print(line)
    print(f"回测区间     {nav.index[0]:%Y-%m-%d} ~ {nav.index[-1]:%Y-%m-%d}（{len(nav)} 个交易日）")
    print(f"期末总资产   {init_cash * nav.iloc[-1]:>14,.0f} 元（初始 {init_cash:,.0f} 元）")
    print(f"策略总收益   {_pct(m['总收益'])}    年化 {_pct(m['年化收益'])}")
    if bench_m is not None:
        print(
            f"基准（{bench_name}） 总收益 {_pct(bench_m['总收益'])}    年化 {_pct(bench_m['年化收益'])}"
            f"    最大回撤 {_pct(bench_m['最大回撤'])}"
        )
    print(f"年化波动     {_pct(m['年化波动'])}")
    print(f"夏普比率     {m['夏普比率']:.2f}    卡玛比率 {m['卡玛比率']:.2f}")
    peak, trough = m["回撤区间"]
    print(f"最大回撤     {_pct(m['最大回撤'])}（{peak:%Y-%m-%d} ~ {trough:%Y-%m-%d}）")
    print(f"日胜率       {_pct(m['日胜率'])}")
    if "调仓日数" in m:
        print(
            f"交易统计     调仓 {m['调仓日数']} 天 / 成交 {m['成交笔数']} 笔"
            f" / 手续费 {m['手续费合计']:,.0f} 元"
        )
    print("")
    print("年度收益对比（策略 vs 基准）")
    yr = yearly_returns(nav)
    if bench_m is not None and "年度收益" in bench_m:
        byr = bench_m["年度收益"]
        for year in yr.index:
            b = _pct(byr.get(year, float("nan"))) if year in byr.index else "—"
            print(f"  {year}    策略 {_pct(yr[year]):>9}    基准 {b:>9}")
    else:
        for year, r in yr.items():
            print(f"  {year}    策略 {_pct(r):>9}")
    if holdings is not None and len(holdings):
        print("")
        print("期末持仓")
        for _, h in holdings.iterrows():
            print(
                f"  {h['code']} {h['name']}  {h['shares']:,.0f} 股"
                f"  市值 {h['value']:,.0f} 元（{h['weight'] * 100:.1f}%）"
                f"  浮动盈亏 {_pct(h['pnl_pct'])}"
            )
    print(line)


def plot_equity_curve(
    nav: pd.Series,
    bench_nav: pd.Series | None,
    out_path: Path,
    title: str,
    bench_name: str = "基准",
) -> None:
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax1.plot(nav.index, nav.values, label="策略净值", color="#c0392b", linewidth=1.4)
    if bench_nav is not None:
        ax1.plot(bench_nav.index, bench_nav.values, label=bench_name, color="#7f8c8d", linewidth=1.1)
    ax1.axhline(1.0, color="gray", linewidth=0.6, linestyle="--", alpha=0.6)
    ax1.set_title(title, fontsize=13)
    ax1.set_ylabel("净值（初始 = 1）")
    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.3)

    dd = drawdown_series(nav)
    ax2.fill_between(dd.index, dd.values * 100, 0, color="#c0392b", alpha=0.35)
    ax2.set_ylabel("回撤 (%)")
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def save_all(
    out_dir: Path,
    nav_df: pd.DataFrame,
    trades: pd.DataFrame,
    bench_nav: pd.Series | None,
    title: str,
    bench_name: str = "基准",
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_nav = nav_df.copy()
    if bench_nav is not None:
        out_nav["benchmark"] = bench_nav
    out_nav.to_csv(out_dir / "nav.csv", encoding="utf-8-sig")
    trades.to_csv(out_dir / "trades.csv", index=False, encoding="utf-8-sig")
    png = out_dir / "equity_curve.png"
    plot_equity_curve(nav_df["nav"], bench_nav, png, title, bench_name)
    return png
