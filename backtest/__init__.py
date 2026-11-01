"""ETF 量化回测框架。

模块结构：
- data.DataFeed           数据加载与清洗（ETF 池 / 日线 / 指数 / 拆分事件）
- broker.Portfolio        账户、持仓与撮合（原始价、整手、手续费、拆分处理）
- strategy                策略基类与动量轮动策略（周频打分调仓）
- engine.BacktestEngine   事件驱动回测引擎（开盘撮合、收盘估值）
- metrics / report        绩效指标与结果输出
"""
