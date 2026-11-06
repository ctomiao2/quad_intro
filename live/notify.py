"""飞书推送（群自定义机器人 Webhook，免费、无需企业应用）。

配置文件 live/state/feishu.json（目录已被 .gitignore 忽略，webhook 不会进仓库）：

{
  "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx",
  "secret": "创建机器人若开启「签名校验」则填，否则留空"
}

机器人创建路径：飞书群 -> 设置 -> 群机器人 -> 添加机器人 -> 自定义机器人。
未配置时推送直接跳过（打印告警），绝不影响实盘主任务。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import requests

RETRIES = 2


def load_config(state_dir: Path) -> dict | None:
    path = state_dir / "feishu.json"
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if obj.get("webhook") else None
    except Exception as e:  # noqa: BLE001
        print(f"[notify] feishu.json 解析失败: {e}")
        return None


def _sign(timestamp: str, secret: str) -> str:
    """飞书自定义机器人签名: base64(hmac_sha256(key='timestamp\\n{secret}', msg=''))。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_text(state_dir: Path, text: str) -> bool:
    """推送文本消息；任何失败只打日志并返回 False。"""
    cfg = load_config(state_dir)
    if cfg is None:
        print("[notify] 未配置飞书 webhook（live/state/feishu.json），跳过推送")
        return False

    body: dict = {"msg_type": "text", "content": {"text": text}}
    if cfg.get("secret"):
        ts = str(int(time.time()))
        body["timestamp"] = ts
        body["sign"] = _sign(ts, cfg["secret"])

    for i in range(RETRIES):
        try:
            r = requests.post(cfg["webhook"], json=body, timeout=10)
            data = r.json()
            # 新版返回 {"code":0,...}，旧版 {"StatusCode":0}
            if data.get("code", data.get("StatusCode")) == 0:
                return True
            print(f"[notify] 飞书返回错误: {data}")
        except Exception as e:  # noqa: BLE001
            print(f"[notify] 推送失败({i + 1}/{RETRIES}): {e}")
        time.sleep(3)
    return False
