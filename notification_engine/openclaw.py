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
OpenClaw Webhook 通知工具
支持通过 OpenClaw hooks 将消息投递到 QQBot / Telegram 渠道

config.ini 配置示例:
    [CONFIG]
    OPENCLAW_HOOK_URL   = https://hook.yourdomain.com/hooks/agent
    OPENCLAW_HOOK_TOKEN = your-hooks-token
    OPENCLAW_DEFAULT_QQ_TO = c2c:YOUR_OPENID   ; 可选
    OPENCLAW_DEFAULT_TG_TO = 123456789          ; 可选

    [OPENCLAW_HEADERS]
    ; 自定义请求头，新增字段无需修改代码
    CF-Access-Client-Id     = your-cf-access-client-id
    CF-Access-Client-Secret = your-cf-access-client-secret

    [OPENCLAW_PAYLOAD]
    ; Hook 请求体默认字段，新增字段无需修改代码
    wakeMode       = now
    deliver        = true
    thinking       = low
    timeoutSeconds = 30
"""

import configparser
import logging
from enum import Enum
from typing import Optional
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)


# ── 常量与枚举 ──────────────────────────────────────────

RELAY_PROMPT_TEMPLATE = (
    "请直接回复以下内容，保留原始信息"
    "不要使用 message tool，不要做其他操作。\n\n{content}"
)

AI_PROMPT_TEMPLATE = (
    "请对以下内容进行简要的金融市场分析后回复给用户，保留原始信息，"
    "使用简洁的格式。不要使用 message tool。\n\n{content}"
)


class Channel(str, Enum):
    QQBOT = "qqbot"
    TELEGRAM = "telegram"


class SendMode(str, Enum):
    RELAY = "relay"
    AI = "ai"


# ── 配置 ─────────────────────────────────────────────────

@dataclass
class OpenClawConfig:
    """OpenClaw Hook 连接配置

    headers 和 payload 以 dict 形式存储，对应 config.ini 中的
    [OPENCLAW_HEADERS] 和 [OPENCLAW_PAYLOAD] section。
    新增 header / payload 字段只需修改配置文件，无需改动代码。
    """
    hook_url: str
    hook_token: str
    headers: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)
    default_qq_to: Optional[str] = None
    default_tg_to: Optional[str] = None

    @classmethod
    def from_config(cls, config: configparser.ConfigParser) -> "OpenClawConfig":
        url = config.get("CONFIG", "OPENCLAW_HOOK_URL", fallback="")
        token = config.get("CONFIG", "OPENCLAW_HOOK_TOKEN", fallback="")
        headers = dict(config.items("OPENCLAW_HEADERS")) if config.has_section("OPENCLAW_HEADERS") else {}
        payload = dict(config.items("OPENCLAW_PAYLOAD")) if config.has_section("OPENCLAW_PAYLOAD") else {}
        return cls(
            hook_url=url,
            hook_token=token,
            headers=headers,
            payload=payload,
            default_qq_to=config.get("CONFIG", "OPENCLAW_DEFAULT_QQ_TO", fallback=None) or None,
            default_tg_to=config.get("CONFIG", "OPENCLAW_DEFAULT_TG_TO", fallback=None) or None,
        )


# ── 响应 ─────────────────────────────────────────────────

@dataclass
class HookResult:
    ok: bool
    run_id: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[dict] = field(default_factory=dict)


# ── 核心类 ────────────────────────────────────────────────

class OpenClawNotifier:
    """OpenClaw Webhook 通知器"""

    def __init__(self, config: configparser.ConfigParser):
        self._cfg = OpenClawConfig.from_config(config)

    # ── 公开方法 ──────────────────────────────────────────

    def relay_to_qq(self, content: str, to: Optional[str] = None, name: str = "QQRelay") -> HookResult:
        """原样转发到 QQBot"""
        return self._send(Channel.QQBOT, to or self._cfg.default_qq_to, content, SendMode.RELAY, name)

    def relay_to_telegram(self, content: str, to: Optional[str] = None, name: str = "TGRelay") -> HookResult:
        """原样转发到 Telegram"""
        return self._send(Channel.TELEGRAM, to or self._cfg.default_tg_to, content, SendMode.RELAY, name)

    def send_to_qq(
        self,
        content: str,
        to: Optional[str] = None,
        mode: str = "relay",
        name: str = "QQNotify",
        prompt_template: Optional[str] = None,
    ) -> HookResult:
        """发送到 QQBot，可选 relay / ai 模式"""
        return self._send(Channel.QQBOT, to or self._cfg.default_qq_to, content, SendMode(mode), name, prompt_template)

    def send_to_telegram(
        self,
        content: str,
        to: Optional[str] = None,
        mode: str = "relay",
        name: str = "TGNotify",
        prompt_template: Optional[str] = None,
    ) -> HookResult:
        """发送到 Telegram，可选 relay / ai 模式"""
        return self._send(Channel.TELEGRAM, to or self._cfg.default_tg_to, content, SendMode(mode), name, prompt_template)

    def send(
        self,
        channel: str,
        to: str,
        content: str,
        mode: str = "relay",
        name: str = "Notify",
        prompt_template: Optional[str] = None,
    ) -> HookResult:
        """通用发送方法"""
        return self._send(Channel(channel), to, content, SendMode(mode), name, prompt_template)

    # ── 内部实现 ──────────────────────────────────────────

    def _build_message(self, content: str, mode: SendMode, prompt_template: Optional[str] = None) -> str:
        if mode == SendMode.RELAY:
            return RELAY_PROMPT_TEMPLATE.format(content=content)
        if prompt_template:
            return prompt_template.format(content=content)
        return AI_PROMPT_TEMPLATE.format(content=content)

    def _build_headers(self) -> dict:
        """合并必要鉴权 header 与 [OPENCLAW_HEADERS] 中的自定义 header"""
        return {
            "Content-Type": "application/json",
            "x-openclaw-token": self._cfg.hook_token,
            **self._cfg.headers,
        }

    def _send(
        self,
        channel: Channel,
        to: Optional[str],
        content: str,
        mode: SendMode,
        name: str,
        prompt_template: Optional[str] = None,
    ) -> HookResult:
        if not self._cfg.hook_url:
            logger.warning("OPENCLAW_HOOK_URL 未配置，跳过发送")
            return HookResult(ok=False, error="OPENCLAW_HOOK_URL 未配置")
        if not self._cfg.hook_token:
            logger.warning("OPENCLAW_HOOK_TOKEN 未配置，跳过发送")
            return HookResult(ok=False, error="OPENCLAW_HOOK_TOKEN 未配置")
        if not to:
            logger.warning("未指定 %s 的接收者 (to)，跳过发送", channel.value)
            return HookResult(ok=False, error=f"未指定 {channel.value} 的接收者")

        # [OPENCLAW_PAYLOAD] 中的配置作为 payload 基础，再叠加本次调用的动态字段
        payload = {
            **self._cfg.payload,
            "message": self._build_message(content, mode, prompt_template),
            "name": name,
            "channel": channel.value,
            "to": to,
        }

        logger.info(
            "发送通知 → %s | to=%s | mode=%s | 内容长度=%d",
            channel.value, to, mode.value, len(content),
        )

        timeout = int(self._cfg.payload.get("timeoutSeconds", 30)) + 10

        try:
            resp = requests.post(
                self._cfg.hook_url,
                headers=self._build_headers(),
                json=payload,
                timeout=timeout,
            )

            logger.debug("响应状态: %d", resp.status_code)
            logger.debug("响应头: %s", dict(resp.headers))

            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("application/json"):
                body = resp.text[:500]
                logger.error(
                    "非 JSON 响应 | status=%d | content-type=%s | body=%s",
                    resp.status_code, content_type, body,
                )
                return HookResult(ok=False, error=f"HTTP {resp.status_code}: {body}")

            if not resp.text.strip():
                logger.error("空响应 | status=%d", resp.status_code)
                return HookResult(ok=False, error=f"HTTP {resp.status_code}: empty response")

            data = resp.json()

            if resp.ok and data.get("ok"):
                logger.info("发送成功: runId=%s", data.get("runId"))
                return HookResult(ok=True, run_id=data.get("runId"), raw=data)

            err = data.get("error", f"HTTP {resp.status_code}")
            logger.error("发送失败: %s | raw=%s", err, data)
            return HookResult(ok=False, error=err, raw=data)

        except requests.Timeout:
            logger.error("请求超时")
            return HookResult(ok=False, error="request timeout")
        except requests.RequestException as e:
            logger.error("请求异常: %s", e)
            return HookResult(ok=False, error=str(e))
