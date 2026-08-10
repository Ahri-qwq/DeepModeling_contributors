"""飞书 webhook 发送：签名、POST、重试。

不关心卡片内容 —— 传进来什么 JSON 就发什么。
"""
import base64
import hashlib
import hmac
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

# 网络层失败重试次数与退避基数。应用层错误（签名错、机器人被移除）
# 不重试 —— 重试一个签名错误没有意义，只会拖慢整次运行。
RETRIES = 3
BACKOFF = 1.0
TIMEOUT = 10

# 飞书文档明确列出的错误码，用于日志里给出可读原因
ERROR_HINTS = {
    9499: "请求体格式错误或超过 20 KB",
    19021: "签名不匹配，或时间戳超出 1 小时",
    19022: "源 IP 不在白名单",
    19024: "未命中自定义关键词",
    11232: "触发限流（单机器人 100 次/分钟、5 次/秒）",
}


class FeishuError(RuntimeError):
    pass


def gen_sign(timestamp: str, secret: str) -> str:
    """飞书签名算法。

    注意这个算法是反直觉的：拼接后的 "timestamp\\n密钥" 整体作为 HMAC
    的密钥，而被摘要的内容是空字符串。照抄官方文档，别按常规签名习惯
    改成"用密钥对时间戳摘要"，那样飞书会返回 19021。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"),
                      b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def load_env(path: str = ".env") -> None:
    """把 .env 里的键值读进环境变量，不覆盖已有的。

    只做最简解析（KEY=VALUE、# 注释、空行），不引入 python-dotenv
    依赖 —— 第一期的依赖清单只有 requests，保持精简。
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def mask(url: str) -> str:
    """脱敏 webhook URL，只留末四位。

    URL 本身就是凭据，日志文件不能变成泄露源。
    """
    if not url:
        return "(未配置)"
    return f"...{url[-4:]}"


def send(payload: dict, url: Optional[str] = None,
         secret: Optional[str] = None, session=None) -> None:
    """发一条消息。失败抛 FeishuError。

    调用方负责捕获 —— 推送失败不该影响统计的退出码。
    """
    url = url or os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not url:
        raise FeishuError("未配置 FEISHU_WEBHOOK_URL")
    secret = secret if secret is not None else os.environ.get(
        "FEISHU_WEBHOOK_SECRET", "")

    body = dict(payload)
    if secret:
        ts = str(int(time.time()))
        body["timestamp"] = ts
        body["sign"] = gen_sign(ts, secret)

    http = session or requests
    last_exc = None

    for attempt in range(RETRIES):
        try:
            resp = http.post(url, json=body, timeout=TIMEOUT)
        except Exception as exc:                      # 网络层：重试
            last_exc = exc
            if attempt < RETRIES - 1:
                wait = BACKOFF * (2 ** attempt)
                log.warning("推送到 %s 失败（%s），%.0f 秒后重试",
                            mask(url), exc, wait)
                time.sleep(wait)
            continue

        try:
            data = resp.json()
        except ValueError:
            data = {}

        code = data.get("code", data.get("StatusCode"))
        if code == 0:
            return

        # 应用层错误不重试
        hint = ERROR_HINTS.get(code, "")
        msg = data.get("msg") or data.get("StatusMessage") or resp.text[:200]
        raise FeishuError(
            f"飞书返回 code={code} msg={msg}" + (f"（{hint}）" if hint else ""))

    raise FeishuError(f"推送到 {mask(url)} 失败，已重试 {RETRIES} 次：{last_exc}")
