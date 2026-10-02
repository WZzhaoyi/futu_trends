"""多市场交易日历：Futu 日历查询 + 市场时区换算 + 本地缓存。

四条规则（live 策略踩过的坑都在这里）：

1. 交易日一律按“市场当地日期”判断：美股用 America/New_York，A 股用 Asia/Shanghai，
   港股用 Asia/Hong_Kong。
   上海时间 10-01 09:53 对应美东 09-30 21:53 —— 拿机器本地日期去问美股会整体错一天。
   同理，跨 DST 时美东与上海相差 12 或 13 小时，只能用 ZoneInfo 换算，不能手算偏移。
2. Futu 的 trade_date_type 区分全天（WHOLE）与半日市（MORNING，如美股感恩节次日、
   圣诞前夜、港股圣诞前夜）。半日市仍然是交易日，只影响收盘时间，不影响交易日判断。
3. 各市场日历互相独立：A 股国庆休市时美股照常开市，港股只休 10-01。缓存按市场分开存。
4. 市场必须由调用方显式给出，既没有默认参数也没有兜底市场；空值/未知市场直接报错。
5. 与交易日有关的配置（MARKET / TRADING_DAY_GATE）统一在本模块解析校验，
   各策略不再各读各的，避免同一个语义在多个调用方里各自解释。

缓存策略：只无条件信任“已经过去的日期”；当天（市场当地日期）的结论一律现查，
查询失败时才退回缓存，避免 OpenD 抖动把错误结论写进缓存后长期生效。
"""

from __future__ import annotations

import configparser
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = PROJECT_ROOT / "data" / "calendar"

WHOLE_DAY = "WHOLE"
HALF_DAY = "MORNING"

# 与交易日相关的两个配置键由本模块统一解析，各策略不再各读各的
MARKET_KEY = "MARKET"
GATE_KEY = "TRADING_DAY_GATE"
GATE_ON_VALUES = {"on", "true", "1", "yes"}
GATE_OFF_VALUES = {"off", "false", "0", "no"}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11111


@dataclass(frozen=True)
class Market:
    """一个市场的交易日历：时区 + 日历锚点符号。"""

    name: str
    timezone: Any
    calendar_symbol: str


MARKETS: dict[str, Market] = {
    "CN": Market("CN", ZoneInfo("Asia/Shanghai"), "SH.000001"),
    "HK": Market("HK", ZoneInfo("Asia/Hong_Kong"), "HK.800000"),
    "US": Market("US", ZoneInfo("America/New_York"), "US.QQQ"),
}


def market_of(market: Any) -> Market:
    """接受市场名或 Market，返回 Market。

    市场必须由调用方显式给定：没有默认市场，空值或未知市场一律报错。
    """
    if isinstance(market, Market):
        return market
    key = "" if market is None else str(market).strip().upper()
    if not key:
        raise ValueError(
            f"必须显式指定市场（可选: {sorted(MARKETS)}），本模块不提供默认市场"
        )
    if key not in MARKETS:
        raise ValueError(f"未知市场: {market}（可选: {sorted(MARKETS)}）")
    return MARKETS[key]


def now_in(market: Any, moment: Optional[datetime] = None) -> datetime:
    """把某一时刻换算成市场当地时区；裸时间按运行机器本地时区解释。"""
    spec = market_of(market)
    moment = moment or datetime.now()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(spec.timezone)


def today_in(market: Any, moment: Optional[datetime] = None) -> date:
    """市场当地日期（判断该市场交易日时唯一可用的“今天”）。"""
    return now_in(market, moment).date()


def to_local(moment: datetime) -> datetime:
    """市场当地时刻换算回运行机器本地时区。"""
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone()


def latest_closed_date(market: Any, close_time: time, moment: Optional[datetime] = None) -> date:
    """此刻最近一个已过收盘时间的市场当地日期（不判断该日是否交易日）。

    策略用它筛选“已收盘的完整日K”：市场当地还没到收盘时间就退回前一天。
    """
    current = now_in(market, moment)
    return current.date() if current.time() >= close_time else current.date() - timedelta(days=1)


def cache_file(market: Any, cache_dir: Optional[Path] = None) -> Path:
    spec = market_of(market)
    root = Path(cache_dir) if cache_dir is not None else CACHE_DIR
    return root / f"trading_days_{spec.name.lower()}.json"


def load_cached_calendar(market: Any, cache_dir: Optional[Path] = None) -> dict[str, Optional[str]]:
    """读取该市场已缓存的历史结论：日期 -> 交易日类型（None 表示非交易日）。"""
    path = cache_file(market, cache_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    days = payload.get("days")
    return dict(days) if isinstance(days, dict) else {}


def _store_day(market: Market, target: str, value: Optional[str], cache_dir: Optional[Path]) -> None:
    path = cache_file(market, cache_dir)
    days = load_cached_calendar(market, cache_dir)
    days[target] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "market": market.name,
                "symbol": market.calendar_symbol,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "days": dict(sorted(days.items())),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _resolve_connection(config: Any, host: Optional[str], port: Optional[int]) -> tuple[str, int]:
    if config is not None:
        if host is None:
            host = config.get("CONFIG", "FUTU_HOST", fallback=DEFAULT_HOST)
        if port is None:
            port = config.get("CONFIG", "FUTU_PORT", fallback=DEFAULT_PORT)
    return host or DEFAULT_HOST, int(port or DEFAULT_PORT)


def _query_day_type(
    market: Market,
    target: str,
    context: Any,
    ret_ok: Any,
) -> Optional[str]:
    ret, days = context.request_trading_days(
        start=target,
        end=target,
        code=market.calendar_symbol,
    )
    if ret != ret_ok:
        raise RuntimeError(f"Futu 交易日查询失败 ({market.name} {target}): {days}")
    for day in days:
        if str(day.get("time", ""))[:10] == target:
            return str(day.get("trade_date_type") or WHOLE_DAY)
    return None


def trading_day_type(
    market: Any,
    trading_date: Optional[date] = None,
    *,
    context: Any = None,
    config: Any = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    use_cache: bool = True,
    cache_dir: Optional[Path] = None,
) -> Optional[str]:
    """该市场当地日期是否为交易日：返回 WHOLE/HALF_DAY/None（非交易日）。

    trading_date 默认取市场当地的今天。context 已连接时直接复用（不新建、不关闭），
    否则用 host/port（或 config 里的 Futu 配置）临时连接。
    """
    spec = market_of(market)
    target = str(trading_date or today_in(spec))[:10]
    settled = target < today_in(spec).isoformat()

    cached = load_cached_calendar(spec, cache_dir) if use_cache else {}
    if use_cache and settled and target in cached:
        return cached[target]

    from futu import OpenQuoteContext, RET_OK

    owned = None
    if context is None:
        connection_host, connection_port = _resolve_connection(config, host, port)
        owned = OpenQuoteContext(host=connection_host, port=connection_port)
        context = owned
    try:
        value = _query_day_type(spec, target, context, RET_OK)
    except Exception:
        if use_cache and target in cached:
            return cached[target]
        raise
    finally:
        if owned is not None:
            try:
                owned.close()
            except Exception as exc:  # noqa: BLE001 - SDK 关闭异常不该掩盖查询结果
                print(f"警告: 关闭 Futu context 失败: {exc}")

    if use_cache:
        _store_day(spec, target, value, cache_dir)
    return value


def is_trading_day(
    market: Any,
    trading_date: Optional[date] = None,
    **kwargs: Any,
) -> bool:
    """该市场当地日期是否开市（半日市也算开市）。"""
    return trading_day_type(market, trading_date, **kwargs) is not None


def markets_from_config(config: configparser.ConfigParser) -> tuple[str, ...]:
    """读取配置声明的所属市场（CONFIG/MARKET，逗号分隔可多个）。

    必须显式配置：缺失、空值或未知市场都直接报错，不做兜底猜测。
    市场词汇表就是本模块的 MARKETS，调用方不必自己再维护一份。
    """
    raw = config.get("CONFIG", MARKET_KEY, fallback="").strip()
    markets = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    if not markets:
        raise ValueError(
            f"配置缺少 CONFIG/{MARKET_KEY}：必须显式声明所属市场，"
            f"如 {MARKET_KEY}=CN 或 {MARKET_KEY}=CN,HK"
        )
    unknown = [market for market in markets if market not in MARKETS]
    if unknown:
        raise ValueError(
            f"CONFIG/{MARKET_KEY} 含未知市场 {unknown}（可选: {sorted(MARKETS)}）"
        )
    return markets


def trading_day_gate_enabled(config: configparser.ConfigParser) -> bool:
    """读取交易日闸门开关（CONFIG/TRADING_DAY_GATE）。

    必须显式配置，不设默认值：on 按交易日过滤，off 表示本任务按自己的排期
    运行、不受交易日约束（例如数据来自上一根已收盘周K的周报）。
    """
    raw = config.get("CONFIG", GATE_KEY, fallback="").strip().lower()
    if not raw:
        raise ValueError(
            f"配置缺少 CONFIG/{GATE_KEY}：必须显式声明 on（按交易日过滤）"
            f"或 off（本任务不受交易日约束）"
        )
    if raw in GATE_ON_VALUES:
        return True
    if raw in GATE_OFF_VALUES:
        return False
    raise ValueError(f"CONFIG/{GATE_KEY} 取值无效: {raw}（可选 on / off）")
