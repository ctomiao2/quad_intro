"""绩效指标。

年化按 A 股一年约 244 个交易日折算；夏普无风险利率取 0。
"""
from __future__ import annotations

import math

import pandas as pd

TRADING_DAYS_PER_YEAR = 244


def drawdown_series(nav: pd.Series) -> pd.Series:
    return nav / nav.cummax() - 1


def summarize(nav: pd.Series, trades: pd.DataFrame | None = None) -> dict:
    """nav: 以日期为索引的净值序列（初始=1）。"""
    ret = nav.pct_change().dropna()
    total = nav.iloc[-1] / nav.iloc[0] - 1
    years = len(nav) / TRADING_DAYS_PER_YEAR
    ann = (1 + total) ** (1 / years) - 1 if years > 0 else math.nan
    std = ret.std()
    vol = std * math.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = ret.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR) if std > 0 else math.nan

    dd = drawdown_series(nav)
    maxdd = dd.min()
    trough = dd.idxmin()
    peak = nav.loc[:trough].idxmax()
    calmar = ann / abs(maxdd) if maxdd < 0 else math.nan

    m = {
        "总收益": total,
        "年化收益": ann,
        "年化波动": vol,
        "夏普比率": sharpe,
        "最大回撤": maxdd,
        "回撤区间": (peak, trough),
        "卡玛比率": calmar,
        "日胜率": (ret > 0).mean() if len(ret) else math.nan,
    }
    if trades is not None and len(trades):
        m["调仓日数"] = trades["date"].nunique()
        m["成交笔数"] = len(trades)
        m["买入总额"] = trades.loc[trades["side"] == "买入", "value"].sum()
        m["卖出总额"] = trades.loc[trades["side"] == "卖出", "value"].sum()
        m["手续费合计"] = trades["commission"].sum()
    return m


def yearly_returns(nav: pd.Series) -> pd.Series:
    """按自然年统计年度收益（首年为区间收益）。"""
    grp = nav.groupby(nav.index.year)
    return grp.last() / grp.first() - 1
