#  Futu Trends
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#  Written by Joey <wzzhaoyi@outlook.com>, 2025
#  Copyright (c)  Joey - All Rights Reserved

"""
通用 Webhook 通知工具
只负责组装请求并发送，不做任何内容加工。接收端相关配置全部写在 config.ini。

config.ini 配置示例（以 OpenClaw 为例）:
    [CONFIG]
    WEBHOOK_URL = https://hook.yourdomain.com/hooks/agent

    [WEBHOOK_HEADERS]
    ; 所有请求头写在这里，包括鉴权 header
    x-openclaw-token    = your-hook-token
    ; Cloudflare Access 认证（可选）
    ; CF-Access-Client-Id     = your-cf-access-client-id
    ; CF-Access-Client-Secret = your-cf-access-client-secret

    [WEBHOOK_PAYLOAD]
    ; 静态 payload 基础字段，新增字段无需修改代码
    wakeMode       = now
    deliver        = true
    thinking       = low
    timeoutSeconds = 30
    ; 接收方地址：以 to_ 为前缀，send() 时自动遍历所有 to_* 字段逐一发送
    to_qq          = c2c:YOUR_OPENID
    to_telegram    = 123456789
"""

import configparser
import logging
from typing import Optional
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

_OK_FIELD = "ok"
_ID_FIELD = "runId"


@dataclass
class HookResult:
    ok: bool
    dest: str = ""
    run_id: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[dict] = field(default_factory=dict)


class WebhookNotifier:
    """通用 Webhook 通知器，配置从 config.ini 读取"""

    def __init__(self, config: configparser.ConfigParser):
        self._url = config.get("CONFIG", "WEBHOOK_URL", fallback="")
        self._headers = {"Content-Type": "application/json"}
        if config.has_section("WEBHOOK_HEADERS"):
            self._headers.update(config.items("WEBHOOK_HEADERS"))
        self._payload = dict(config.items("WEBHOOK_PAYLOAD")) if config.has_section("WEBHOOK_PAYLOAD") else {}

    def send(self, content: str, name: str = "Notify") -> list[HookResult]:
        """遍历 [WEBHOOK_PAYLOAD] 中所有 to_* 接收方并逐一发送"""
        if not self._url:
            logger.warning("WEBHOOK_URL 未配置，跳过发送")
            return [HookResult(ok=False, error="WEBHOOK_URL 未配置")]

        destinations = {k[3:]: v for k, v in self._payload.items() if k.startswith("to_")}
        if not destinations:
            logger.warning("[WEBHOOK_PAYLOAD] 中无 to_* 接收方配置，跳过发送")
            return [HookResult(ok=False, error="无接收方配置")]

        static = {k: v for k, v in self._payload.items() if not k.startswith("to_")}
        timeout = int(static.get("timeoutseconds", 30)) + 10

        return [self._post(dest, to, static, content, name, timeout) for dest, to in destinations.items()]

    def _post(self, dest: str, to: str, static: dict, content: str, name: str, timeout: int) -> HookResult:
        payload = {**static, "to": to, "message": content, "name": name}
        logger.info("发送通知 → dest=%s | to=%s | 内容长度=%d", dest, to, len(content))
        try:
            resp = requests.post(self._url, headers=self._headers, json=payload, timeout=timeout)
            logger.debug("响应状态: %d", resp.status_code)

            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("application/json"):
                body = resp.text[:500]
                logger.error("非 JSON 响应 | status=%d | body=%s", resp.status_code, body)
                return HookResult(ok=False, dest=dest, error=f"HTTP {resp.status_code}: {body}")

            if not resp.text.strip():
                logger.error("空响应 | status=%d", resp.status_code)
                return HookResult(ok=False, dest=dest, error=f"HTTP {resp.status_code}: empty response")

            data = resp.json()
            if resp.ok and data.get(_OK_FIELD):
                logger.info("发送成功: dest=%s | id=%s", dest, data.get(_ID_FIELD))
                return HookResult(ok=True, dest=dest, run_id=data.get(_ID_FIELD), raw=data)

            err = data.get("error", f"HTTP {resp.status_code}")
            logger.error("发送失败: dest=%s | %s | raw=%s", dest, err, data)
            return HookResult(ok=False, dest=dest, error=err, raw=data)

        except requests.Timeout:
            logger.error("请求超时: dest=%s", dest)
            return HookResult(ok=False, dest=dest, error="request timeout")
        except requests.RequestException as e:
            logger.error("请求异常: dest=%s | %s", dest, e)
            return HookResult(ok=False, dest=dest, error=str(e))
