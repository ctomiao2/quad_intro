# 实盘运行说明（live/）

实盘系统与回测（`../backtest/`）的关系——**同一份代码，隔离的数据与状态**：

| 层面 | 回测 | 实盘 | 一致性保证 |
| --- | --- | --- | --- |
| 策略逻辑 | `backtest/strategy.py` | 同一文件，`import` 复用 | 不复制代码，单一实现 |
| 撮合/整手/费用 | `backtest/engine.py` + `broker.py` | 同一文件（指令生成直接调用 `BacktestEngine._rebalance`） | 同一算法 |
| 策略参数 | `run_backtest.py` 默认值 | `live/config.json` | `selfcheck` 强校验相等 |
| 行情数据 | `etf_daily/` 快照 | `live/data/`（部署时一次性复制 + 每日追加） | 格式/复权公式同构，回测不读写实盘数据 |
| 账户状态 | 回测内存 | `live/state/positions.json` | 手续费口径同 `broker.py` |

## 目录

```
live/
  config.json            策略与账户配置（改参数前先跑 selfcheck）
  quotes.py              免费行情源：腾讯批量实时 + 东财日K/实时 + 新浪兜底
  store.py               数据副本管理：追加收盘行、重算前复权、历史行不可变
  runner.py              open/close/backfill/fill/status/selfcheck 任务实现
  data/                  行情数据副本（bootstrap 生成，git 忽略）
  state/                 持仓、净值、调仓状态、交易日历缓存（git 忽略，含个人交易隐私）
  logs/                  运行日志与每日交易指令（git 忽略）
  deploy_schedule.bat    注册 Windows 计划任务
  remove_schedule.bat    删除计划任务
```

## 首次部署（已完成）

```bash
python live.py bootstrap    # 复制数据快照到 live/data（只做一次，重复执行会被拒绝）
python live.py selfcheck    # 一致性自检：参数/数据/复权/信号回放 vs 回测
```

### 新电脑一键部署

拉下代码后**双击 `live/deploy_schedule.bat`** 即可完成全部部署：自动找 Python、
装依赖、初始化数据副本、跑一致性自检、注册 09:31/15:35 计划任务（幂等，可重复
运行）。唯一前提：机器上已装 Python 3.10+（勾选 Add to PATH）。删除部署用
`live/remove_schedule.bat`。

### 飞书推送

收盘任务每天推送「收盘日报」（持仓明细、现金、净值、基准，调仓日附当日指令回顾）；
调仓日开盘任务额外即时推送「调仓指令」（目标持仓与整手申报单）。未配置时自动跳过，
不影响主任务。

配置方法：飞书群 → 设置 → 群机器人 → 添加「自定义机器人」→ 复制 Webhook 地址，
然后：

```bash
cp live/feishu.example.json live/state/feishu.json   # 填入 webhook；若开启签名校验再填 secret
python live.py notify-test                            # 发测试消息确认
```

`live/state/` 已被 .gitignore 忽略，webhook 不会进公开仓库。

## 日常流程（计划任务自动执行）

| 时间 | 任务 | 动作 |
| --- | --- | --- |
| 09:31 周一~五 | `python live.py open` | 抓全池开盘价 → 动量打分（数据截至昨收）→ 调仓日生成交易指令到 `logs/orders_日期.json`，**按开盘价整手人工申报** |
| 15:35 周一~五 | `python live.py close` | 抓官方定稿日线入库（东财→腾讯→新浪三层兜底）→ 记录实盘净值 |

成交后手工登记实际回报（手续费按回测口径自动扣）：

```bash
python live.py fill --code 512800.SH --side 买 --shares 57500 --price 0.869
python live.py status      # 持仓/现金/净值/与回测同期对比
```

## 故障恢复

- **错过收盘任务**（关机/休眠）：第二天开盘任务会告警数据落后，补一句
  `python live.py backfill` 即可（只补今天之前的缺口，历史行绝不覆盖）。
- **错过开盘任务**：当日内任意时刻手动 `python live.py open` 补跑（过了 15:00
  会提示指令仅供参考）。若整个交易日的开盘任务都没跑，本周调仓自动顺延到
  下一次运行时补做（调仓状态按周持久化）。
- **重跑幂等**：open/close 同日重跑不会产生重复指令/重复净值行。

## 已知边界与维护事项

1. **拆分/合股**：若池内 ETF 发生新的拆分，在 `live/data/adj_factors.json`
   登记事件（`{"date":"YYYYMMDD","ratio":2.0}`），历史前复权列会在下次写入时
   自动重算；持仓股数在拆分日开盘自动 ×ratio（回测同款逻辑，515050 的 1拆3
   已在回放验证中确认）。
2. **数据源**：主链路免 token（腾讯/东财/新浪公开接口，参考 quad-live 的
   验证与兜底策略）。东财 push2his 自 2026-11 起必须带 `ut` + `fields1/2`
   参数（已在 quotes.py 处理并有注释）。akshare 非必需（仅交易日历刷新用，
   缺失时按工作日保守判断 + 行情数据兜底）。
3. **开盘价假设**：09:31 抓取的「今开」即集合竞价开盘价；指令按开盘价成交
   是与回测一致的假设，实际人工申报会有偏差，用 `fill` 登记真实成交即可，
   `status` 可随时观察实盘 vs 回测的净值漂移。
4. **涨跌停/停牌**：开盘价缺失或校验不通过的标的当日自动跳过（不买不卖，
   下个调仓日再处理），与回测引擎行为一致。
5. **本机依赖**：计划任务只在开机且到点时触发；睡眠/关机会漏跑，按故障恢复
   一节补跑即可。

## 与 quad_live（D:/Learn/quad-live）的关系

行情接口实现参考其 `ETFDataSource.py` / `UpdateIndexData.py` / `TradingCalendar.py`
（多层兜底、量纲换算、数据校验、历史行不可变等经验），本目录为独立实现，
不 import quad-live 代码。
