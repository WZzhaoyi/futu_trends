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
    ; 所有 payload 字段，新增字段无需修改代码；用户内容由代码按 WEBHOOK_CONTENT_FIELD 附加
    channel        = qqbot
    to             = c2c:YOUR_OPENID
    name           = Notify
    wakeMode       = now
    deliver        = true
    thinking       = low
    timeoutSeconds = 30
"""

import configparser
import logging
from typing import Optional
from dataclasses import dataclass, field

import requests

from .delivery_log import log_delivery, log_delivery_exception

logger = logging.getLogger(__name__)

_OK_FIELD = "ok"
_ID_FIELD = "runId"


@dataclass
class HookResult:
    ok: bool
    run_id: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[dict] = field(default_factory=dict)


class WebhookNotifier:
    """通用 Webhook 通知器，配置从 config.ini 读取"""

    def __init__(self, config: configparser.ConfigParser):
        self._url = config.get("CONFIG", "WEBHOOK_URL", fallback="")
        self._content_field = config.get("CONFIG", "WEBHOOK_CONTENT_FIELD", fallback="message")
        self._headers = {"Content-Type": "application/json"}
        if config.has_section("WEBHOOK_HEADERS"):
            self._headers.update(config.items("WEBHOOK_HEADERS"))
        self._payload = dict(config.items("WEBHOOK_PAYLOAD")) if config.has_section("WEBHOOK_PAYLOAD") else {}
        # 单独为通知走代理：data.setup_global_proxy 已不再设全局 env 代理，
        # 与 engine 里 telegram/feishu 一致，按 config 的 PROXY 逐请求指定。
        proxy = config.get("CONFIG", "PROXY", fallback="").strip()
        self._proxies = {"http": proxy, "https": proxy} if proxy else None

    def send(self, content: str) -> HookResult:
        """将 content 附加到 [WEBHOOK_PAYLOAD] 并发送"""
        if not self._url:
            logger.warning("WEBHOOK_URL 未配置，跳过发送")
            return HookResult(ok=False, error="WEBHOOK_URL 未配置")

        payload = {**self._payload, self._content_field: content}
        try:
            timeout = int(self._payload.get("timeoutseconds", 30)) + 10
            if timeout <= 0:
                raise ValueError
        except (TypeError, ValueError):
            log_delivery(
                'webhook',
                'send',
                ok=False,
                error='invalid timeoutseconds',
            )
            return HookResult(ok=False, error="invalid timeoutseconds")

        try:
            resp = requests.post(
                self._url,
                headers=self._headers,
                json=payload,
                timeout=timeout,
                proxies=self._proxies,
            )
            if not resp.text.strip():
                log_delivery(
                    'webhook',
                    'send',
                    ok=False,
                    status=resp.status_code,
                    error='empty response',
                )
                return HookResult(ok=False, error=f"HTTP {resp.status_code}: empty response")

            try:
                data = resp.json()
            except ValueError:
                log_delivery(
                    'webhook',
                    'send',
                    ok=False,
                    status=resp.status_code,
                    error='non-JSON response',
                )
                return HookResult(
                    ok=False,
                    error=f"HTTP {resp.status_code}: non-JSON response",
                )
            if not isinstance(data, dict):
                data = {}

            if resp.ok and data.get(_OK_FIELD):
                log_delivery(
                    'webhook',
                    'send',
                    ok=True,
                    status=resp.status_code,
                    run_id=data.get(_ID_FIELD),
                )
                return HookResult(ok=True, run_id=data.get(_ID_FIELD), raw=data)

            err = data.get("error", f"HTTP {resp.status_code}")
            log_delivery(
                'webhook',
                'send',
                ok=False,
                status=resp.status_code,
                error=err,
            )
            return HookResult(ok=False, error=err, raw=data)

        except requests.Timeout:
            log_delivery('webhook', 'send', ok=False, exception='Timeout')
            return HookResult(ok=False, error="request timeout")
        except requests.RequestException as exc:
            exception_name = type(exc).__name__
            log_delivery_exception('webhook', 'send', exc)
            return HookResult(ok=False, error=exception_name)
