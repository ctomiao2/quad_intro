"""账户与撮合层：现金、持仓、成交记录。

约定：
- 以原始价（非复权价）成交，买卖按 100 股整数手申报；
- 拆分/合股事件在生效日开盘前执行：股数 × ratio（本池 ratio 均为整数，
  因此持仓始终是整手的倍数）；
- 手续费 = max(成交额 × commission_rate, min_commission)，买卖同率（ETF 免印花税）。
"""
from __future__ import annotations

import pandas as pd

SIDE_BUY = "买入"
SIDE_SELL = "卖出"


class Position:
    __slots__ = ("code", "shares", "cost")

    def __init__(self, code: str, shares: int = 0, cost: float = 0.0):
        self.code = code
        self.shares = shares
        self.cost = cost  # 累计买入成本（含手续费），用于浮动盈亏统计

    def __repr__(self):
        return f"Position({self.code}, {self.shares}股, 成本{self.cost:.0f})"


class Trade:
    __slots__ = ("date", "code", "side", "shares", "price", "value", "commission", "cash_after")

    def __init__(self, date, code, side, shares, price, value, commission, cash_after):
        self.date = date
        self.code = code
        self.side = side
        self.shares = shares
        self.price = price
        self.value = value
        self.commission = commission
        self.cash_after = cash_after

    def as_dict(self):
        return {
            "date": self.date.strftime("%Y-%m-%d"),
            "code": self.code,
            "side": self.side,
            "shares": self.shares,
            "price": self.price,
            "value": self.value,
            "commission": self.commission,
            "cash_after": self.cash_after,
        }


class Portfolio:
    """现金 + 持仓账户，撮合在引擎的驱动下进行。"""

    def __init__(
        self,
        cash: float,
        commission_rate: float = 1e-4,
        min_commission: float = 0.0,
        lot: int = 100,
    ):
        self.init_cash = float(cash)
        self.cash = float(cash)
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.lot = lot
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []

    # ---- 查询 ----
    def shares(self, code: str) -> int:
        return self.positions[code].shares if code in self.positions else 0

    def market_value(self, prices: pd.Series) -> float:
        """按给定估值价（原始收盘、已前向填充）计算持仓市值。"""
        total = 0.0
        for pos in self.positions.values():
            price = prices.get(pos.code)
            if price is None or pd.isna(price):
                raise ValueError(f"{pos.code} 在估值日无价格，无法估值")
            total += pos.shares * float(price)
        return total

    # ---- 事件 ----
    def apply_split(self, code: str, ratio: float) -> None:
        """拆分/合股生效：股数 × ratio。价格尺度由行情切换自然承接。"""
        pos = self.positions[code]
        pos.shares = int(round(pos.shares * ratio))

    def _fee(self, value: float) -> float:
        return max(value * self.commission_rate, self.min_commission)

    def buy(self, code: str, shares: int, price: float, date) -> None:
        value = shares * price
        fee = self._fee(value)
        if value + fee > self.cash + 1e-6:
            raise RuntimeError(f"资金不足：需 {value + fee:.2f}，可用 {self.cash:.2f}")
        self.cash -= value + fee
        pos = self.positions.setdefault(code, Position(code))
        pos.shares += shares
        pos.cost += value + fee
        self.trades.append(
            Trade(date, code, SIDE_BUY, shares, round(price, 4), round(value, 2), round(fee, 2), self.cash)
        )

    def sell(self, code: str, shares: int, price: float, date) -> None:
        pos = self.positions[code]
        if shares > pos.shares:
            raise RuntimeError(f"持仓不足：{code} 拟卖 {shares}，持有 {pos.shares}")
        value = shares * price
        fee = self._fee(value)
        self.cash += value - fee
        pos.cost *= (pos.shares - shares) / pos.shares  # 按比例结转成本
        pos.shares -= shares
        if pos.shares == 0:
            del self.positions[code]
        self.trades.append(
            Trade(date, code, SIDE_SELL, shares, round(price, 4), round(value, 2), round(fee, 2), self.cash)
        )
