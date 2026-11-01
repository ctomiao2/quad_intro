"""事件驱动回测引擎（日频、开盘撮合）。

每个交易日的流程：
  1. 开盘前处理当日生效的拆分/合股事件（持仓股数 × ratio）；
  2. 策略基于截至昨收的数据给出目标持仓（None=当日不动）；
  3. 按当日开盘价、整手、先卖后买撮合（卖出不在目标中的 → 再平衡目标标的）；
  4. 收盘后用收盘价（缺失用最近收盘价前向填充）估值，记录净值。

简化假设（工程上可接受，注释备查）：
- 以开盘价成交、不设滑点与涨跌停约束，开盘价即集合竞价结果；
- 同一开盘时点内卖出的资金立即可用于买入。
"""
from __future__ import annotations

import pandas as pd

from .broker import Portfolio
from .data import DataFeed
from .strategy import BaseStrategy, Context


class BacktestEngine:
    def __init__(
        self,
        feed: DataFeed,
        strategy: BaseStrategy,
        portfolio: Portfolio,
        start: str | None = None,
        end: str | None = None,
        min_order_value: float = 500.0,  # 低于该金额的调仓单忽略，避免碎单手续费损耗
    ):
        self.feed = feed
        self.strategy = strategy
        self.pf = portfolio
        cal = feed.calendar
        start_ts = pd.to_datetime(str(start)) if start else cal[0]
        end_ts = pd.to_datetime(str(end)) if end else cal[-1]
        self.dates = cal[(cal >= start_ts) & (cal <= end_ts)]
        self.min_order_value = min_order_value

    def run(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """执行回测，返回 (每日净值表, 成交明细表)。"""
        open_p = self.feed.panel("open")                  # 原始开盘价，NaN=当日无行情
        close_ffill = self.feed.panel("close").ffill()    # 估值价：缺失日用最近收盘

        rows = []
        for i, date in enumerate(self.dates):
            prev_date = self.dates[i - 1] if i > 0 else None

            # 1) 拆分/合股事件在开盘前生效
            for code in list(self.pf.positions):
                for ev_date, ratio in self.feed.splits.get(code, []):
                    if ev_date == date:
                        self.pf.apply_split(code, ratio)

            # 2) 策略给出目标持仓
            targets = self.strategy.on_date(Context(date, prev_date, self.pf, self.feed))

            # 3) 以当日开盘价撮合
            if targets is not None:
                self._rebalance(date, targets, open_p.loc[date], close_ffill.loc[date])

            # 4) 收盘估值
            mv = self.pf.market_value(close_ffill.loc[date])
            rows.append(
                {"date": date, "cash": self.pf.cash, "market_value": mv, "total": self.pf.cash + mv}
            )

        nav = pd.DataFrame(rows).set_index("date")
        nav["nav"] = nav["total"] / self.pf.init_cash
        trades = pd.DataFrame([t.as_dict() for t in self.pf.trades])
        return nav, trades

    def _rebalance(
        self,
        date: pd.Timestamp,
        targets: dict[str, float],
        opens: pd.Series,
        closes: pd.Series,
    ) -> None:
        pf = self.pf

        def price_of(code: str):
            """返回 (价格, 当日是否可交易)。当日无行情的持仓用最近收盘价估值。"""
            p = opens.get(code)
            if p is not None and not pd.isna(p):
                return float(p), True
            return float(closes[code]), False

        # 先卖出所有不在目标中的持仓
        for code in list(pf.positions):
            if code not in targets:
                price, tradable = price_of(code)
                if tradable:
                    pf.sell(code, pf.shares(code), price, date)
                else:
                    print(f"[engine] {date:%Y-%m-%d} {code} 当日无行情，卖出顺延")

        # 总权益（开盘价口径，用于确定目标市值）
        equity = pf.cash + sum(
            pos.shares * price_of(pos.code)[0] for pos in pf.positions.values()
        )
        total_w = sum(targets.values())
        if total_w <= 0:
            return

        # 先算各标的的目标股数并按调整量排序，保证先卖后买
        adjustments = []
        for code, w in targets.items():
            price, tradable = price_of(code)
            if not tradable:
                print(f"[engine] {date:%Y-%m-%d} {code} 当日无行情，跳过买入")
                continue
            lots = int(equity * (w / total_w) / (price * pf.lot))
            tgt_shares = lots * pf.lot
            adjustments.append((tgt_shares - pf.shares(code), code, price))

        for delta, code, price in sorted(adjustments):
            value = abs(delta) * price
            if delta >= pf.lot:
                if value < self.min_order_value:
                    continue
                shares = delta
                # 现金不够则按手递减（整手价格取整导致的零头）
                while shares > 0 and shares * price * (1 + pf.commission_rate) > pf.cash:
                    shares -= pf.lot
                if shares > 0:
                    pf.buy(code, shares, price, date)
            elif delta <= -pf.lot:
                if value >= self.min_order_value:
                    pf.sell(code, -delta, price, date)
