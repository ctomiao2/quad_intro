"""免费行情数据源适配（参考 D:/Learn/quad-live/code 的成熟实现）。

主链路只用 requests（带超时），akshare 可装可用时仅用于刷新交易日历，缺它不影响主流程：

- 腾讯批量实时 qt.gtimg.cn：整池 ETF + 指数一次拉齐（开盘任务主源；
  quad-live 20260831 已与东财 spot 交叉验证 OHLC 一致）
- 东财 push2his 日K：官方定稿日线（收盘任务主源、补洞），盘后当日行即终值
- 东财 push2 实时：单只兜底（f152 小数位解码价格）
- 新浪 kline：最后兜底（无 amount，按 quad-live 口径手造 vol×close 并标注）

统一口径（与 quad_intro 数据快照一致）：
- ETF volume=股、amount=元；
- 指数 volume=手（东财/腾讯原单位）、amount=元。

校验（沿用 quad-live 三重校验思路）：价格较昨收偏离 >30% 视为异常拒绝；
amount 与 volume×close 严重不自洽视为异常。
"""
from __future__ import annotations

import json
import socket
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

socket.setdefaulttimeout(20)  # 兜住无 timeout 的第三方请求（同 quad-live NetGuard 思路）

TIMEOUT = 10
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def sina_sym(code: str) -> str:
    """'510300.SH' -> 'sh510300'（腾讯/新浪代码格式，指数同规则）。"""
    pure, suffix = code.split(".")
    return suffix.lower() + pure


# ---------------- 腾讯批量实时 ----------------

def fetch_spot_tencent(codes: list[str], index: bool = False) -> dict[str, dict]:
    """腾讯批量实时快照 -> {code: {open/high/low/close/volume/amount/prev_close}}。

    index=True 时 codes 为指数（volume 保持「手」，与快照口径一致）；
    ETF 的 volume 由「手」换算为「股」。失败/无效行自动跳过。
    """
    if not codes:
        return {}
    s = requests.Session()
    s.trust_env = False
    out: dict[str, dict] = {}
    # 接口一次最多支持 ~60 个代码，30 个以内直接一批
    try:
        r = s.get(
            "https://qt.gtimg.cn/q=" + ",".join(sina_sym(c) for c in codes), timeout=TIMEOUT
        )
        r.encoding = "gbk"
        text = r.text
    except Exception as e:  # noqa: BLE001
        print(f"[quotes] 腾讯批量行情失败: {e}")
        return out
    sym2code = {sina_sym(c): c for c in codes}
    for line in text.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        code = sym2code.get(line.split("=")[0].replace("v_", "").strip())
        if not code:
            continue
        try:
            parts = line.split('"')[1].split("~")
            if len(parts) < 38:
                continue
            close = float(parts[3])
            if close <= 0:
                continue
            volume = int(float(parts[6]) * 100) if not index else int(float(parts[6]))
            out[code] = {
                "open": float(parts[5]),
                "close": close,
                "high": float(parts[33]),
                "low": float(parts[34]),
                "volume": volume,
                "amount": int(float(parts[37]) * 10000),  # 万 -> 元
                "prev_close": float(parts[4]),
                # 行情时间戳 yyyymmddHHMMSS（parts[30]）：节假日兜底判断用
                "time": parts[30].split(".")[0][:14] if len(parts) > 30 else "",
            }
        except (ValueError, IndexError):
            continue
    return out


# ---------------- 东财 ----------------

def _secid(code: str) -> str:
    pure, suffix = code.split(".")
    return f"1.{pure}" if suffix == "SH" else f"0.{pure}"


def fetch_daily_em(code: str, index: bool = False, limit: int = 15) -> list[dict]:
    """东财 push2his 日K（官方定稿，不复权）-> 按日期升序的行列表。

    行: {date:'YYYYMMDD', open, high, low, close, volume, amount}；
    ETF volume 手->股，指数保持手；失败返回 []。
    """
    s = requests.Session()
    s.trust_env = False
    s.headers.update(HEADERS)
    try:
        r = s.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": _secid(code),
                "klt": "101",
                "fqt": "0",
                "end": "20500101",
                "lmt": str(limit),
                # 2026-11 实测：缺 ut/fields1/fields2 时接口返回 rc=102 data=null
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            },
            timeout=TIMEOUT,
        )
        klines = (r.json().get("data") or {}).get("klines") or []
    except Exception as e:  # noqa: BLE001
        print(f"[quotes] 东财日K({code}) 失败: {e}")
        return []
    rows = []
    for k in klines:
        try:
            p = k.split(",")
            d = p[0].replace("-", "")
            vol = int(float(p[5]))
            rows.append(
                {
                    "date": d,
                    "open": float(p[1]),
                    "high": float(p[3]),
                    "low": float(p[4]),
                    "close": float(p[2]),
                    "volume": vol * 100 if not index else vol,
                    "amount": int(float(p[6])),
                }
            )
        except (ValueError, IndexError):
            continue
    return rows


def fetch_realtime_em(code: str) -> dict | None:
    """东财 push2 单只实时（价格按 f152 小数位解码），ETF volume 手->股。"""
    s = requests.Session()
    s.trust_env = False
    s.headers.update(HEADERS)
    try:
        r = s.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={
                "secid": _secid(code),
                "fields": "f43,f44,f45,f46,f47,f48,f60,f152",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            },
            timeout=TIMEOUT,
        )
        d = r.json().get("data") or {}
    except Exception as e:  # noqa: BLE001
        print(f"[quotes] 东财实时({code}) 失败: {e}")
        return None
    if not d or not d.get("f43"):
        return None
    scale = int(d.get("f152", 2) or 2)

    def _p(x, fb):
        return float(x) / (10 ** scale) if x else fb

    close = _p(d.get("f43"), 0)
    if close <= 0:
        return None
    return {
        "open": _p(d.get("f46"), close),
        "close": close,
        "high": _p(d.get("f44"), close),
        "low": _p(d.get("f45"), close),
        "volume": int(float(d.get("f47", 0) or 0)) * 100,
        "amount": int(float(d.get("f48", 0) or 0)),
        "prev_close": _p(d.get("f60"), close),
    }


# ---------------- 新浪兜底 ----------------

def fetch_kline_sina(code: str, datalen: int = 3) -> list[dict]:
    """新浪 kline 日K（最后兜底）：volume=股；无 amount 字段，手造 vol×close（quad-live 同口径）。"""
    s = requests.Session()
    s.trust_env = False
    try:
        r = s.get(
            "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
            params={"symbol": sina_sym(code), "scale": "240", "ma": "no", "datalen": str(datalen)},
            timeout=TIMEOUT,
        )
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[quotes] 新浪kline({code}) 失败: {e}")
        return []
    rows = []
    for item in data or []:
        try:
            c = float(item["close"])
            v = int(float(item["volume"]))
            rows.append(
                {
                    "date": str(item["day"]).replace("-", ""),
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": c,
                    "volume": v,
                    "amount": int(v * c),  # 手造（仅极端兜底时入库，信号不使用 amount）
                }
            )
        except (ValueError, KeyError, TypeError):
            continue
    return rows


# ---------------- 校验与组合 ----------------

def sanity_ok(close: float, amount: float, prev_close: float | None, volume: float = 0) -> bool:
    """三重校验（沿用 quad-live）：自洽 / 价格偏离>30% / 成交额偏离100倍。"""
    if close > 0 and volume > 0 and amount > 0:
        r = amount / (volume * close)
        if r < 0.2 or r > 5.0:
            return False
    if prev_close and prev_close > 0 and close > 0:
        if abs(close / prev_close - 1) > 0.30:
            return False
    return True


def fetch_etf_open_with_fallback(
    codes: list[str], spot: dict[str, dict]
) -> dict[str, dict]:
    """开盘任务取「今日开盘价」：腾讯批量为主，缺失/异常的逐只东财实时兜底。

    返回 {code: 行}（只含当日确有行情的标的），异常标的打印告警并被剔除。
    """
    out: dict[str, dict] = {}
    for code in codes:
        rec = spot.get(code)
        ok = rec and rec["open"] > 0 and sanity_ok(rec["close"], rec["amount"], rec["prev_close"], rec["volume"])
        if not ok:
            rec2 = fetch_realtime_em(code)
            if rec2 and rec2["open"] > 0 and sanity_ok(rec2["close"], rec2["amount"], rec2["prev_close"], rec2["volume"]):
                rec = rec2
                print(f"[quotes] {code} 腾讯数据异常/缺失, 已用东财实时兜底")
            else:
                print(f"[quotes] {code} 当日开盘数据不可用, 今日无法交易该标的")
                continue
        out[code] = rec
    return out


# ---------------- 交易日历 ----------------

def load_trade_dates(state_dir: Path) -> set[str] | None:
    """交易日历（YYYYMMDD 集合）。本地缓存；缓存不覆盖今天且 akshare 可用时刷新。

    参考 quad-live TradingCalendar：刷新失败不阻断，返回缓存或 None（按工作日保守判断）。
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    cache = state_dir / "trade_calendar.json"
    today = date.today().strftime("%Y%m%d")
    obj = None
    if cache.exists():
        try:
            obj = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            obj = None
    if obj and max(obj["dates"]) >= today:
        return set(obj["dates"])

    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        dates = sorted(str(pd.Timestamp(v).strftime("%Y%m%d")) for v in df[col])
        cache.write_text(
            json.dumps({"fetched_at": str(date.today()), "dates": dates}, ensure_ascii=False),
            encoding="utf-8",
        )
        return set(dates)
    except Exception as e:  # noqa: BLE001
        print(f"[quotes] 交易日历刷新失败({e})，使用缓存/工作日判断")
        return set(obj["dates"]) if obj else None


def is_trade_date(d: date, state_dir: Path) -> bool:
    dates = load_trade_dates(state_dir)
    if not dates:
        return d.weekday() < 5  # 保守：无日历时按工作日，再由行情数据兜底
    return d.strftime("%Y%m%d") in dates


def prev_trade_date(d: date, state_dir: Path) -> date | None:
    """d 之前（不含 d）最近的交易日。"""
    dates = load_trade_dates(state_dir)
    if not dates:
        return None
    before = sorted(x for x in dates if x < d.strftime("%Y%m%d"))
    return datetime.strptime(before[-1], "%Y%m%d").date() if before else None
