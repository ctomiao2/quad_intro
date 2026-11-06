"""策略层。

打分逻辑（每个交易日开盘时执行，只使用截至昨收的数据，无未来函数）：
- 收益率用前复权价 adj_close 计算（否则拆分日会出现 -50% 的假暴跌）；
- mode="sharpe"：score = lookback 日均收益 / 日收益波动（窗口夏普，风险调整动量）；
- mode="return"：score = lookback 区间涨跌幅（朴素动量）；
- 上市不足 lookback 个有效交易日的标的分数为 NaN，自动不具备资格。

调仓逻辑（freq="W" 时每周第一个交易日，节假日顺延到该周首个交易日）：
- 取 score 最高的 top_n 只等权持有；
- score <= min_score 的标的剔除（动量为负时空仓等待）；
- 已持有且仍在名单内的标的随净值再平衡回目标权重。

on_date 返回约定：
- None  —— 非调仓日，不产生任何交易；
- dict  —— 目标持仓 {code: 权重}，空 dict 表示清仓观望。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from .broker import Portfolio
from .data import DataFeed


@dataclass
class Context:
    """传给策略的当日上下文。"""

    date: pd.Timestamp                # 当前交易日（决策发生在开盘时点）
    prev_date: pd.Timestamp | None    # 上一交易日（信号数据截止日）
    portfolio: Portfolio
    feed: DataFeed


class BaseStrategy(ABC):
    """策略基类：prepare 里预计算因子，on_date 里给出目标持仓。"""

    def prepare(self, feed: DataFeed) -> None:  # noqa: B027（可选钩子）
        pass

    @abstractmethod
    def on_date(self, ctx: Context) -> dict[str, float] | None:
        ...


class MomentumRotationStrategy(BaseStrategy):
    """动量轮动：定期按动量分数取 top_n 等权持有。"""

    def __init__(
        self,
        lookback: int = 25,
        top_n: int = 2,
        min_score: float = 0.0,
        mode: str = "sharpe",
        freq: str = "W",  # W=每周, M=每月, D=每个交易日
    ):
        assert mode in ("sharpe", "return"), f"未知打分模式: {mode}"
        assert freq in ("W", "M", "D"), f"未知调仓频率: {freq}"
        self.lookback = lookback
        self.top_n = top_n
        self.min_score = min_score
        self.mode = mode
        self.freq = freq
        self.scores: pd.DataFrame | None = None
        self._last_period = None

    def prepare(self, feed: DataFeed) -> None:
        adj = feed.panel("adj_close")
        if self.mode == "sharpe":
            ret = adj.pct_change(fill_method=None)
            n = self.lookback
            mean = ret.rolling(n, min_periods=n).mean()
            std = ret.rolling(n, min_periods=n).std()
            self.scores = mean / (std + 1e-12)
        else:
            self.scores = adj / adj.shift(self.lookback) - 1

    def _period_key(self, date: pd.Timestamp):
        if self.freq == "W":
            iso = date.isocalendar()
            return (iso.year, iso.week)
        if self.freq == "M":
            return (date.year, date.month)
        return date  # D：每个交易日都是新周期

    def is_rebalance_day(self, date: pd.Timestamp, last_rebalance_date: pd.Timestamp | None) -> bool:
        """date 相对「上一次调仓日」是否开启新调仓周期。

        回测引擎靠 on_date 的内部状态（连续交易日循环）隐式完成同一判断；
        实盘每日新建进程，需要把上次调仓日持久化后显式调用本方法，
        两边共用同一段周期规则，保证周频语义一致。
        """
        if last_rebalance_date is None:
            return True
        return self._period_key(date) != self._period_key(last_rebalance_date)

    def on_date(self, ctx: Context) -> dict[str, float] | None:
        key = self._period_key(ctx.date)
        if self._last_period is not None and key == self._last_period:
            return None  # 非调仓日
        self._last_period = key

        if ctx.prev_date is None:  # 回测首日无历史数据
            return {}
        row = self.scores.loc[ctx.prev_date].dropna()
        row = row[row > self.min_score]
        top = row.sort_values(ascending=False).head(self.top_n)
        if top.empty:
            return {}  # 无合格标的 → 清仓观望
        weight = 1.0 / len(top)
        return {code: weight for code in top.index}
