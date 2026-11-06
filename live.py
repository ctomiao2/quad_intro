"""实盘入口（工作目录无关，可被计划任务直接调用）。

用法：
  python live.py bootstrap              # 首次部署：复制数据快照到 live/data（只做一次）
  python live.py open                   # 开盘任务：抓开盘行情 -> 策略打分 -> 生成交易指令
  python live.py close                  # 收盘任务（15:00 后）：定稿行情入库 -> 记净值
  python live.py backfill [--days 10]   # 补最近 N 天缺失数据（开机错过收盘任务后使用）
  python live.py fill --code 512800.SH --side 买 --shares 41200 --price 0.88
                                        # 成交登记：人工在券商端成交后回填，更新持仓
  python live.py status                 # 查看持仓/现金/净值/与回测同期对比
  python live.py selfcheck              # 实盘 vs 回测 一致性自检

实盘与回测的一致性由三层保证（详见 live/runner.py 模块注释）：
同一策略类、同一撮合代码、同一参数来源；live/ 目录的配置与数据回测不读不改。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 计划任务无控制台/GBK 环境兜底
except Exception:  # noqa: BLE001
    pass

from live import runner  # noqa: E402  (需先插入 ROOT 到 sys.path)


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 动量轮动实盘")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bootstrap", help="首次部署：复制数据快照到 live/data")
    sub.add_parser("open", help="开盘任务：打分并生成交易指令")
    p_close = sub.add_parser("close", help="收盘任务：行情入库并记净值")
    p_close.add_argument("--force", action="store_true", help="未到 15:00 也强制执行")
    p_bf = sub.add_parser("backfill", help="补最近 N 天缺失数据")
    p_bf.add_argument("--days", type=int, default=10)
    p_fill = sub.add_parser("fill", help="登记一笔实际成交")
    p_fill.add_argument("--code", required=True)
    p_fill.add_argument("--side", required=True, help="买|卖")
    p_fill.add_argument("--shares", type=int, required=True)
    p_fill.add_argument("--price", type=float, required=True)
    p_fill.add_argument("--date", default=None, help="成交日 YYYYMMDD，默认今天")
    sub.add_parser("status", help="查看账户与净值状态")
    sub.add_parser("selfcheck", help="实盘与回测一致性自检")

    args = parser.parse_args()
    live_root = ROOT / "live"
    if args.cmd == "bootstrap":
        from live.store import LiveStore

        LiveStore(live_root).bootstrap(ROOT)
        # 播种交易日历缓存（若 quad-live 有现成缓存）
        seed = Path("D:/Learn/quad-live/cache/trade_dates.json")
        state = live_root / "state"
        if seed.exists() and not (state / "trade_calendar.json").exists():
            state.mkdir(parents=True, exist_ok=True)
            (state / "trade_calendar.json").write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
            print("[bootstrap] 已播种交易日历缓存（来自 quad-live）")
        print("[bootstrap] 完成。下一步: python live.py selfcheck && python live.py open")
        return 0

    runner.setup_logging(live_root)
    cfg = runner.load_config(live_root)
    if args.cmd == "open":
        return runner.cmd_open(live_root, cfg)
    if args.cmd == "close":
        return runner.cmd_close(live_root, cfg, force=args.force)
    if args.cmd == "backfill":
        return runner.cmd_backfill(live_root, cfg, days=args.days)
    if args.cmd == "fill":
        return runner.cmd_fill(live_root, cfg, args.code, args.side, args.shares, args.price, args.date)
    if args.cmd == "status":
        return runner.cmd_status(live_root, cfg)
    if args.cmd == "selfcheck":
        return runner.cmd_selfcheck(live_root, cfg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
