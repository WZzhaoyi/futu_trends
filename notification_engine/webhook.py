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
多目标 Webhook 通知工具

只负责组装请求并发送，不做任何内容加工。接收端相关配置全部写在 config.ini。
同一份 config.ini 可以配置多个投递目标，每个目标一个 [WEBHOOK.<name>] 分区：

    [CONFIG]
    # 必填：启用的目标，逗号分隔并按此顺序投递
    WEBHOOK_TARGETS = hermes, openclaw

    # Hermes agent：HMAC-SHA256 V2 签名
    [WEBHOOK.hermes]
    url             = https://hermes.example/api/webhook
    signing         = hmac-sha256-v2
    secret          = your-webhook-secret
    content_field   = message
    event_id_field  = event_id

    [WEBHOOK.hermes.headers]
    X-Tenant = team-quant

    [WEBHOOK.hermes.payload]
    channel = qqbot

    # OpenClaw：静态鉴权头
    [WEBHOOK.openclaw]
    url     = https://hook.yourdomain.com/hooks/agent
    signing = none

    [WEBHOOK.openclaw.headers]
    x-openclaw-token = your-hook-token

    [WEBHOOK.openclaw.payload]
    channel = qqbot
    to      = c2c:YOUR_OPENID

目标分区支持的选项：
    url              必填，Webhook 地址
    signing          必填：none | hmac-sha256-v2；缺失或取值非法时该目标不投递
    secret           签名密钥，signing=hmac-sha256-v2 时必填
    content_field    用户内容写入 payload 的字段名，默认 message
    event_id_field   把本次投递的 event_id 写入 payload 的字段名，默认不写入
    timeout_seconds  请求超时秒数，默认 30
    proxy            该目标的代理，缺省沿用 [CONFIG] PROXY
    payload_json     精确 payload，保留大小写与类型，覆盖 [WEBHOOK.<name>.payload]
                     （分区键会被 ini 统一小写，同名但大小写不同的键以此为准）

投递成功判定只看 HTTP 2xx；响应是 JSON 对象时顺带取 runId 作为投递 ID。

event_id 由 event_type + 内容确定性生成（sha256 前 32 位，event_type 取该目标 payload
里的 event_type 字段），不含日期：同一事件无论哪天投递都得到同一个 event_id，接收端
可以据此去重。反过来，内容一字不改的通知永远算同一个事件 —— 需要按期多次独立送达的
通知（心跳、日报类）请在内容里带上区分信息，例如时间或序号。

hmac-sha256-v2 签名约定：
    X-Request-ID           = event_id（确定性生成，body 里的 event_id 同值）
    X-Webhook-Timestamp    = 当前 Unix 秒
    X-Webhook-Signature-V2 = HMAC-SHA256(secret, "<timestamp>.<body>") 的 hex

body 只序列化一次并以原始字节发送，接收端按收到的字节验签（不要用 requests 的
json= 参数，它会在发送前重新序列化并转义非 ASCII 内容，导致签名字节不一致）。
本模块不做内部重试；补发同一事件会算出同一个 event_id，接收端可据此判重。

配置文件缺项不抛异常：记一条 error 日志并跳过该目标，单个目标失败不影响其他目标，
结果汇总在 HookResult，逐目标明细见 HookResult.targets。
"""

from __future__ import annotations

import configparser
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from .delivery_log import log_delivery

logger = logging.getLogger(__name__)

_ID_FIELD = "runId"

_TARGET_SECTION_PREFIX = "WEBHOOK."
_SIGNING_NONE = "none"
_SIGNING_HMAC_V2 = "hmac-sha256-v2"

_DEFAULT_CONTENT_FIELD = "message"
_DEFAULT_TIMEOUT_SECONDS = 30.0


def sign_hmac_sha256_v2(secret: str, timestamp: str, body: bytes) -> str:
    """按 Hermes 约定计算签名：HMAC-SHA256(secret, "<timestamp>.<body>")。"""
    return hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()


@dataclass
class TargetResult:
    """单个投递目标的发送结果。"""

    target: str
    ok: bool
    status: Optional[int] = None
    run_id: Optional[str] = None
    request_id: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[dict] = None


@dataclass
class HookResult:
    """一次 send() 的聚合结果；单目标时字段与旧实现保持一致。"""

    ok: bool
    run_id: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[dict] = field(default_factory=dict)
    request_id: Optional[str] = None
    targets: dict = field(default_factory=dict)


@dataclass
class WebhookTarget:
    """一个已解析的投递目标。"""

    name: str
    url: str
    content_field: str = _DEFAULT_CONTENT_FIELD
    signing: str = _SIGNING_NONE
    secret: str = ""
    event_id_field: str = ""
    timeout: Optional[float] = _DEFAULT_TIMEOUT_SECONDS
    proxy: str = ""
    headers: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)
    payload_json: dict = field(default_factory=dict)

    @property
    def proxies(self) -> Optional[dict]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}


def _read_section(config: configparser.ConfigParser, section: str) -> dict:
    """按原样读取分区（不插值），返回 {小写键: 值}。"""
    if not config.has_section(section):
        return {}
    return dict(config.items(section, raw=True))


def _get_option(config, option, fallback=""):
    if not config.has_section("CONFIG"):
        return fallback
    return config.get("CONFIG", option, fallback=fallback, raw=True)


def _normalize_names(names) -> list:
    if isinstance(names, str):
        names = names.split(",")
    return [str(name).strip() for name in names if str(name).strip()]


def _build_target(config, name: str, default_proxy: str) -> Optional[WebhookTarget]:
    section = f"{_TARGET_SECTION_PREFIX}{name}"
    options = _read_section(config, section)
    if not options:
        log_delivery("webhook", "config", ok=False, target=name, error="missing section")
        return None

    url = options.get("url", "").strip()
    if not url:
        log_delivery("webhook", "config", ok=False, target=name, error="missing url")
        return None

    raw_signing = options.get("signing", "").strip()
    if not raw_signing:
        log_delivery("webhook", "config", ok=False, target=name, error="missing signing")
        return None
    signing = raw_signing.lower()
    if signing not in (_SIGNING_NONE, _SIGNING_HMAC_V2):
        log_delivery("webhook", "config", ok=False, target=name,
                     error=f"unsupported signing: {raw_signing}")
        return None

    secret = options.get("secret", "").strip()
    if signing == _SIGNING_HMAC_V2 and not secret:
        # 宁可这个目标不投递，也不能把该签名的请求静默发成明文。
        log_delivery("webhook", "config", ok=False, target=name, error="missing secret")
        return None

    timeout = _DEFAULT_TIMEOUT_SECONDS
    raw_timeout = options.get("timeout_seconds", "").strip()
    if raw_timeout:
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = 0.0
        if timeout <= 0:
            log_delivery("webhook", "config", ok=False, target=name, error="invalid timeout_seconds")
            return None

    payload_json = {}
    raw_payload_json = options.get("payload_json", "").strip()
    if raw_payload_json:
        try:
            parsed = json.loads(raw_payload_json)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            log_delivery("webhook", "config", ok=False, target=name, error="invalid payload_json")
            return None
        payload_json = parsed

    return WebhookTarget(
        name=name,
        url=url,
        content_field=options.get("content_field", "").strip() or _DEFAULT_CONTENT_FIELD,
        signing=signing,
        secret=secret,
        event_id_field=options.get("event_id_field", "").strip(),
        timeout=timeout,
        proxy=options.get("proxy", "").strip() or default_proxy,
        headers=_read_section(config, f"{section}.headers"),
        payload=_read_section(config, f"{section}.payload"),
        payload_json=payload_json,
    )


def _load_targets(config) -> list:
    """目标必须由 [CONFIG] WEBHOOK_TARGETS 显式列出，缺失即报错不投递。"""
    declared = _get_option(config, "WEBHOOK_TARGETS").strip()
    if not declared:
        log_delivery("webhook", "config", ok=False, error="missing WEBHOOK_TARGETS")
        return []

    default_proxy = _get_option(config, "PROXY").strip()
    targets = []
    for name in dict.fromkeys(_normalize_names(declared)):
        target = _build_target(config, name, default_proxy)
        if target is not None:
            targets.append(target)
    return targets


def _event_id(event_type: str, content: str) -> str:
    """按 event_type + 内容生成确定性 event_id：同一事件重复投递（含跨天）都得到同一个 id。"""
    seed = f"{event_type}\n{content}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _merge_payload(section_payload: dict, payload_json: dict) -> dict:
    """精确 payload 覆盖同名分区键；大小写不同的同名字段以 payload_json 为准。"""
    exact = {key.lower() for key in payload_json}
    merged = {
        key: value for key, value in section_payload.items()
        if key.lower() not in exact
    }
    merged.update(payload_json)
    return merged


def _evaluate_response(target: WebhookTarget, response) -> TargetResult:
    """成功判定只看 HTTP 2xx；响应是 JSON 对象时顺带取 runId 与 error。"""
    status = getattr(response, "status_code", None)
    data = {}
    if (getattr(response, "text", "") or "").strip():
        try:
            parsed = response.json()
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            data = parsed

    if response.ok:
        return TargetResult(target=target.name, ok=True, status=status,
                            run_id=data.get(_ID_FIELD), raw=data)
    return TargetResult(target=target.name, ok=False, status=status,
                        error=data.get("error", f"HTTP {status}"), raw=data)


class WebhookNotifier:
    """多目标 Webhook 通知器，配置从 config.ini 读取。"""

    def __init__(self, config: configparser.ConfigParser):
        self._targets = _load_targets(config)

    @property
    def target_names(self) -> list:
        return [target.name for target in self._targets]

    def send(self, content: str) -> HookResult:
        """把 content 投递到各目标；单个目标失败不影响其他目标。"""
        if not self._targets:
            logger.warning("Webhook 没有可用投递目标，跳过发送")
            return HookResult(ok=False, error="WEBHOOK_TARGETS 未配置或目标无效")

        results = {
            target.name: self._deliver(target, content)
            for target in self._targets
        }

        ok = all(result.ok for result in results.values())
        first = next(iter(results.values()))
        return HookResult(
            ok=ok,
            run_id=first.run_id,
            error=None if ok else next(
                (result.error for result in results.values() if not result.ok), None
            ),
            raw=first.raw,
            request_id=first.request_id if len(results) == 1 else None,
            targets=results,
        )

    def _deliver(self, target: WebhookTarget, content: str) -> TargetResult:
        payload = _merge_payload(target.payload, target.payload_json)
        payload[target.content_field] = content
        # event_id 由目标自己的 event_type 决定，各接收端在自己的域里去重
        request_id = _event_id(payload.get("event_type", ""), content)
        if target.event_id_field:
            payload[target.event_id_field] = request_id
        # 只序列化一次，签名与实发字节就是同一份 bytes。
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {"Content-Type": "application/json", **target.headers}
        headers["X-Request-ID"] = request_id
        if target.signing == _SIGNING_HMAC_V2:
            timestamp = str(int(time.time()))
            headers["X-Webhook-Timestamp"] = timestamp
            headers["X-Webhook-Signature-V2"] = sign_hmac_sha256_v2(
                target.secret, timestamp, body
            )

        try:
            response = requests.post(
                target.url,
                headers=headers,
                data=body,
                timeout=target.timeout,
                proxies=target.proxies,
            )
        except requests.Timeout:
            log_delivery("webhook", "send", ok=False, target=target.name,
                         request_id=request_id, exception="Timeout")
            return TargetResult(target=target.name, ok=False, request_id=request_id,
                                error="request timeout")
        except requests.RequestException as exc:
            failure = getattr(exc, "response", None)
            log_delivery(
                "webhook", "send", ok=False, target=target.name,
                request_id=request_id,
                status=getattr(failure, "status_code", None),
                exception=type(exc).__name__,
            )
            return TargetResult(target=target.name, ok=False, request_id=request_id,
                                error=type(exc).__name__)

        result = _evaluate_response(target, response)
        result.request_id = request_id
        log_delivery(
            "webhook",
            "send",
            ok=result.ok,
            target=target.name,
            request_id=request_id,
            status=result.status,
            run_id=result.run_id,
            error=result.error,
        )
        return result
