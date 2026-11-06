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
from datetime import datetime
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
    sub.add_parser("notify-test", help="发送一条飞书测试消息（验证 webhook 配置）")

    args = parser.parse_args()
    live_root = ROOT / "live"
    if args.cmd == "bootstrap":
        from live.store import LiveStore

        LiveStore(live_root).bootstrap(ROOT)
        # 播种交易日历：仓库种子(覆盖至2026-12-31)与 quad-live 缓存取更新者
        import json as _json

        state = live_root / "state"
        state.mkdir(parents=True, exist_ok=True)
        if not (state / "trade_calendar.json").exists():
            candidates = [live_root / "trade_calendar_seed.json",
                          Path("D:/Learn/quad-live/cache/trade_dates.json")]
            best = None
            for p in candidates:
                if p.exists():
                    try:
                        d = _json.loads(p.read_text(encoding="utf-8"))["dates"]
                        if best is None or max(d) > max(best[1]):
                            best = (p, d)
                    except Exception:  # noqa: BLE001
                        continue
            if best:
                (state / "trade_calendar.json").write_text(
                    _json.dumps({"fetched_at": str(__import__("datetime").date.today()),
                                 "dates": best[1]}, ensure_ascii=False), encoding="utf-8")
                print(f"[bootstrap] 已播种交易日历({len(best[1])} 天, 覆盖至 {max(best[1])})")
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
    if args.cmd == "notify-test":
        from live.notify import load_config, send_text

        state = live_root / "state"
        if load_config(state) is None:
            print("未配置: 请把 live/feishu.example.json 复制为 live/state/feishu.json 并填入 webhook")
            return 1
        ok = send_text(state, f"【quad实盘】飞书推送测试成功 {datetime.now():%Y-%m-%d %H:%M}")
        print("测试消息已发送，请到飞书群确认" if ok else "发送失败，请检查 webhook/secret 或网络")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
