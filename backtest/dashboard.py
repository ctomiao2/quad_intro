"""HTML 回测看板生成器。

生成自包含的 dashboard.html（内嵌 echarts 与回测数据，离线双击可开）：
- 指标卡：策略/基准收益、年化、超额、夏普、最大回撤；
- 主图：策略 vs 大盘累计收益率曲线 + 回撤副图，悬停弹出 tips
  （日期、策略与大盘的当日/累计收益率、当日超额、当前回撤），支持缩放；
- 年度收益对比柱状图、策略月度收益热力图。

模板见 dashboard_template.html，用字面量 token 替换（避免 str.format
与 JS/CSS 花括号冲突）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .metrics import drawdown_series, yearly_returns

TEMPLATE = Path(__file__).parent / "dashboard_template.html"
ECHARTS_LOCAL = Path(__file__).parent / "vendor" / "echarts.min.js"
ECHARTS_CDN = (
    '<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"></script>'
    '<script src="https://registry.npmmirror.com/echarts/5.5.1/files/dist/echarts.min.js"></script>'
)

_WEEKDAY_CN = "一二三四五六日"


def _f(x, nd=4) -> float | None:
    """numpy 标量 -> 内置 float（NaN -> None），便于 JSON 序列化。"""
    if x is None or pd.isna(x):
        return None
    return round(float(x), nd)


def _pct(x, signed=True) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{x * 100:+.2f}%" if signed else f"{x * 100:.2f}%"


def _cards_html(m: dict, bm: dict, nav: pd.Series, bench_nav: pd.Series, init_cash: float) -> str:
    final_assets = init_cash * nav.iloc[-1]
    excess_ann = m["年化收益"] - bm["年化收益"]
    s_yr, b_yr = yearly_returns(nav), yearly_returns(bench_nav)
    win_years = sum(1 for y in s_yr.index if y in b_yr.index and s_yr[y] > b_yr[y])

    def card(label, value, sub="", color=""):
        style = f' style="color:{color}"' if color else ""
        sub_html = f'<div class="card-sub">{sub}</div>' if sub else ""
        return (
            f'<div class="card"><div class="card-label">{label}</div>'
            f'<div class="card-value"{style}>{value}</div>{sub_html}</div>'
        )

    up = "#d63031"
    return "".join(
        [
            card("策略总收益", _pct(m["总收益"]), f"期末资产 {final_assets:,.0f} 元", up),
            card("策略年化收益", _pct(m["年化收益"]), f"年化波动 {_pct(m['年化波动'])} · 夏普 {m['夏普比率']:.2f}"),
            card(f"基准总收益", _pct(bm["总收益"]), f"基准年化 {_pct(bm['年化收益'])}"),
            card("超额年化", _pct(excess_ann), f"{len(s_yr)} 年中 {win_years} 年跑赢基准", up if excess_ann > 0 else "#009432"),
            card("最大回撤", _pct(m["最大回撤"]), f"基准最大回撤 {_pct(bm['最大回撤'])}", "#009432"),
            card("日胜率", _pct(m["日胜率"], signed=False), f"卡玛 {m['卡玛比率']:.2f} · 调仓 {m.get('调仓日数', '—')} 天"),
        ]
    )


def save_dashboard(
    out_dir: Path,
    nav_df: pd.DataFrame,
    bench_nav: pd.Series,
    m: dict,
    bm: dict,
    title: str,
    bench_name: str,
    params_desc: str = "",
    init_cash: float = 100000.0,
) -> Path:
    s = nav_df["nav"]
    b = bench_nav
    s_cum = (s / s.iloc[0] - 1) * 100
    b_cum = (b / b.iloc[0] - 1) * 100
    s_day = s.pct_change() * 100
    b_day = b.pct_change() * 100
    dd = drawdown_series(s) * 100

    s_yr = yearly_returns(s)
    b_yr = yearly_returns(b)

    # 月度热力图数据：[x=月份序号(0~11, 12=全年), y=年份序号, 收益%]
    mon = s.groupby([s.index.year, s.index.month]).agg(["first", "last"])
    mon_ret = (mon["last"] / mon["first"] - 1) * 100
    hm_years = sorted({y for y, _ in mon_ret.index})
    year_idx = {y: i for i, y in enumerate(hm_years)}
    hm_data = [[mth - 1, year_idx[yr], _f(v, 2)] for (yr, mth), v in mon_ret.items()]
    hm_data += [[12, year_idx[yr], _f(v, 2)] for yr, v in s_yr.items()]

    payload = {
        "benchName": bench_name,
        "dates": [d.strftime("%Y-%m-%d") for d in nav_df.index],
        "wd": ["周" + _WEEKDAY_CN[d.dayofweek] for d in nav_df.index],
        "sCum": [_f(v) for v in s_cum],
        "bCum": [_f(v) for v in b_cum],
        "sDay": [_f(v) for v in s_day],
        "bDay": [_f(v) for v in b_day],
        "dd": [_f(v) for v in dd],
        "years": [int(y) for y in s_yr.index],
        "sYearly": [_f(v, 4) for v in s_yr.values],
        "bYearly": [_f(b_yr.get(y), 4) for y in s_yr.index],
        "hmMonths": [f"{i}月" for i in range(1, 13)] + ["全年"],
        "hmYears": hm_years,
        "hmData": hm_data,
    }

    period = f"{nav_df.index[0]:%Y-%m-%d} ~ {nav_df.index[-1]:%Y-%m-%d}"
    footer = (
        f"回测区间 {period} · {len(nav_df)} 个交易日 · 初始资金 {init_cash:,.0f} 元 · "
        f"开盘价整手成交 · 佣金含于成交明细<br>"
        f"策略参数：{params_desc} · 基准：{bench_name} · "
        f"看板生成时间 {datetime.now():%Y-%m-%d %H:%M} · 数据仅用于教学研究，不构成投资建议"
    )

    if ECHARTS_LOCAL.exists():
        echarts_script = f"<script>\n{ECHARTS_LOCAL.read_text(encoding='utf-8')}\n</script>"
    else:
        echarts_script = ECHARTS_CDN

    html = TEMPLATE.read_text(encoding="utf-8")
    for token, value in [
        ("__TITLE__", title),
        ("__SUBTITLE__", params_desc + " · " + period),
        ("__CARDS_HTML__", _cards_html(m, bm, s, b, init_cash)),
        ("__BENCH_NAME__", bench_name),
        ("__FOOTER__", footer),
        ("__ECHARTS_SCRIPT__", echarts_script),
        ("__PAYLOAD_JSON__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
    ]:
        html = html.replace(token, value)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "dashboard.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path
