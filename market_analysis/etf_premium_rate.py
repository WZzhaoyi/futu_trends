#!/usr/bin/env python3
"""ETF T-1 净值折溢价回测与实时通知。

回测复现原聚宽策略的网格、磁滞仓位和收盘到次日收盘收益逻辑；
数据口径修正为未复权市价 / 决策日前最新可见单位净值。

示例：
    python market_analysis/etf_premium_rate.py backtest --refresh
    python market_analysis/etf_premium_rate.py live \
        --runtime-dir /absolute/path/runtime --config config.ini
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import math
import os
import queue
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


STRATEGY = "etf-premium-t1"
VERSION = "etf-premium-t1-v1"
SCHEMA_VERSION = 1
DEFAULT_SYMBOL = "159941"
DEFAULT_START = "2016-01-01"
DEFAULT_COST = 0.001
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
LIVE_MAX_NAV_AGE_DAYS = 14
LIVE_MAX_QUOTE_AGE_SECONDS = 180
LIVE_MAX_CONSECUTIVE_ERRORS = 5


@dataclass(frozen=True)
class StrategyParams:
    buy_threshold: float
    sell_threshold: float
    low_position: float
    base_position: float = 1.0


@dataclass(frozen=True)
class HistoryPaths:
    root: Path
    prices: Path
    navs: Path

    @classmethod
    def build(cls, symbol: str, raw: Optional[str]) -> "HistoryPaths":
        root = (
            Path(raw).expanduser().resolve()
            if raw
            else PROJECT_ROOT / "data" / "etf_premium" / symbol
        )
        return cls(root, root / "prices.json", root / "navs.json")


@dataclass(frozen=True)
class LiveRuntimePaths:
    root: Path
    state_file: Path
    lock_file: Path

    @classmethod
    def from_argument(cls, raw: str) -> "LiveRuntimePaths":
        root = Path(raw).expanduser()
        if not root.is_absolute():
            raise ValueError("--runtime-dir 必须使用绝对路径")
        root = Path(os.path.abspath(root))
        return cls(root, root / "state.json", root / "live.lock")

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)


class RuntimeFileLock:
    """防止同一运行目录启动多个 live 实例；进程退出时由 OS 释放。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "RuntimeFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write(b"\0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    self.handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError(
                f"已有 live 实例占用运行目录: {self.path.parent}"
            ) from exc
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def retry(operation: Callable[[], Any], attempts: int = 3) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:  # network clients expose different exceptions
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_csv_atomic(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, path)


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        return []
    return list(payload.get("records") or [])


def merge_records(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = {str(row["date"]): row for row in existing}
    rows.update({str(row["date"]): row for row in incoming})
    return [rows[key] for key in sorted(rows)]


def cache_covers(records: list[dict[str, Any]], start: str, end: str) -> bool:
    if not records:
        return False
    start_limit = date.fromisoformat(start) + timedelta(days=7)
    end_limit = date.fromisoformat(end) - timedelta(days=7)
    return (
        date.fromisoformat(records[0]["date"]) <= start_limit
        and date.fromisoformat(records[-1]["date"]) >= end_limit
    )


def save_records(
    path: Path,
    symbol: str,
    source: str,
    records: list[dict[str, Any]],
) -> None:
    write_json_atomic(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "symbol": symbol,
            "source": source,
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "records": records,
        },
    )


def fetch_price_history(
    symbol: str, start: str, end: str
) -> list[dict[str, Any]]:
    import akshare as ak

    frame = retry(lambda: ak.fund_etf_hist_sina(symbol=f"sz{symbol}"))
    if frame.empty:
        raise RuntimeError(f"AkShare 新浪接口未返回 {symbol} 历史行情")
    return [
        {"date": str(row["date"]), "close": float(row["close"])}
        for _, row in frame.iterrows()
        if start <= str(row["date"]) <= end
    ]


def parse_nav_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        try:
            unit_nav = float(item["DWJZ"])
            accumulated_nav = float(item["LJJZ"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append(
            {
                "date": str(item["FSRQ"]),
                "unit_nav": unit_nav,
                "accumulated_nav": accumulated_nav,
            }
        )
    return rows


def fetch_nav_history(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    """按字段名解析东财净值，规避 AkShare 固定列数失效。"""
    import requests

    url = "https://api.fund.eastmoney.com/f10/lsjz"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://fundf10.eastmoney.com/jjjz_{symbol}.html",
    }
    # 该端点目前会把大 pageSize 静默限制为 20，必须据此分页。
    page_size = 20
    page = 1
    rows: list[dict[str, Any]] = []
    with requests.Session() as session:
        while True:
            response = retry(
                lambda: session.get(
                    url,
                    params={
                        "fundCode": symbol,
                        "pageIndex": page,
                        "pageSize": page_size,
                        "startDate": start,
                        "endDate": end,
                    },
                    headers=headers,
                    timeout=30,
                )
            )
            response.raise_for_status()
            payload = response.json()
            items = ((payload.get("Data") or {}).get("LSJZList") or [])
            rows.extend(parse_nav_items(items))
            total = int(payload.get("TotalCount") or 0)
            if not items or page * page_size >= total:
                break
            page += 1
    if not rows:
        raise RuntimeError(f"东财未返回 {symbol} 历史净值")
    return sorted(rows, key=lambda row: row["date"])


def load_history(
    symbol: str,
    start: str,
    end: str,
    paths: HistoryPaths,
    refresh: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prices = read_records(paths.prices)
    navs = read_records(paths.navs)
    if refresh or not cache_covers(prices, start, end):
        source = "akshare.fund_etf_hist_sina"
        fetched_prices = fetch_price_history(symbol, start, end)
        prices = merge_records(prices, fetched_prices)
        if not cache_covers(prices, start, end):
            raise RuntimeError(
                f"{source} 历史覆盖不足: "
                f"{prices[0]['date']} 至 {prices[-1]['date']}"
            )
        save_records(paths.prices, symbol, source, prices)
    if refresh or not cache_covers(navs, start, end):
        navs = merge_records(navs, fetch_nav_history(symbol, start, end))
        save_records(paths.navs, symbol, "eastmoney.f10.lsjz", navs)
    prices = [row for row in prices if start <= row["date"] <= end]
    navs = [row for row in navs if start <= row["date"] <= end]
    return prices, navs


def build_signal_frame(
    prices: list[dict[str, Any]],
    navs: list[dict[str, Any]],
) -> pd.DataFrame:
    """把每个交易日与严格早于该日的最新单位净值对齐。"""
    price_frame = pd.DataFrame(prices).rename(columns={"date": "trade_date"})
    nav_frame = pd.DataFrame(navs).rename(columns={"date": "nav_date"})
    if price_frame.empty or nav_frame.empty:
        raise ValueError("价格或净值数据为空")
    price_frame["trade_date"] = pd.to_datetime(price_frame["trade_date"])
    nav_frame["nav_date"] = pd.to_datetime(nav_frame["nav_date"])
    price_frame = price_frame.sort_values("trade_date")
    nav_frame = nav_frame.sort_values("nav_date")
    nav_frame["adjustment_factor"] = (
        nav_frame["accumulated_nav"] / nav_frame["unit_nav"]
    )
    frame = pd.merge_asof(
        price_frame,
        nav_frame[["nav_date", "adjustment_factor"]].rename(
            columns={"nav_date": "factor_nav_date"}
        ),
        left_on="trade_date",
        right_on="factor_nav_date",
        direction="backward",
        allow_exact_matches=False,
    )
    frame["total_return_close"] = (
        frame["close"] * frame["adjustment_factor"]
    )
    frame = pd.merge_asof(
        frame,
        nav_frame[["nav_date", "unit_nav"]],
        left_on="trade_date",
        right_on="nav_date",
        direction="backward",
        allow_exact_matches=False,
    )
    frame["premium"] = frame["close"] / frame["unit_nav"] - 1
    frame["ret"] = (
        frame["total_return_close"].shift(-1) / frame["total_return_close"] - 1
    )
    frame["nav_age_days"] = (
        frame["trade_date"] - frame["nav_date"]
    ).dt.days
    return frame.dropna(subset=["premium", "ret"]).reset_index(drop=True)


def positions_for_premium(
    premiums: np.ndarray,
    params: StrategyParams,
) -> np.ndarray:
    current = params.base_position
    positions = np.empty(len(premiums), dtype=float)
    for index, premium in enumerate(premiums):
        if premium > params.sell_threshold:
            current = params.low_position
        elif premium < params.buy_threshold:
            current = params.base_position
        positions[index] = current
    return positions


def evaluate_params(
    premiums: np.ndarray,
    returns: np.ndarray,
    params: StrategyParams,
    cost: float,
) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    positions = positions_for_premium(premiums, params)
    changes = np.abs(np.diff(positions, prepend=positions[0]))
    strategy_returns = positions * returns - changes * cost
    strategy_curve = np.cumprod(1 + strategy_returns)
    benchmark_curve = np.cumprod(1 + returns)
    total_return = float(strategy_curve[-1] - 1)
    benchmark_return = float(benchmark_curve[-1] - 1)
    return (
        {
            "buy_threshold": params.buy_threshold,
            "sell_threshold": params.sell_threshold,
            "low_position": params.low_position,
            "base_position": params.base_position,
            "total_return": total_return,
            "benchmark_return": benchmark_return,
            "excess": total_return - benchmark_return,
            "trades": int(np.count_nonzero(changes)),
        },
        positions,
        strategy_curve,
    )


def grid_search(
    frame: pd.DataFrame,
    cost: float = DEFAULT_COST,
) -> tuple[dict[str, Any], pd.DataFrame, np.ndarray, np.ndarray]:
    premiums = frame["premium"].to_numpy(dtype=float)
    returns = frame["ret"].to_numpy(dtype=float)
    rows: list[dict[str, Any]] = []
    best: Optional[dict[str, Any]] = None
    best_positions = np.array([])
    best_curve = np.array([])
    for low_int in range(9):
        for buy_bps in range(-200, 200, 50):
            for sell_bps in range(300, 801, 50):
                buy = buy_bps / 10_000
                sell = sell_bps / 10_000
                if sell - buy < 0.02:
                    continue
                params = StrategyParams(buy, sell, low_int / 10)
                metrics, positions, curve = evaluate_params(
                    premiums, returns, params, cost
                )
                rows.append(metrics)
                if best is None or (
                    metrics["excess"], metrics["total_return"]
                ) > (best["excess"], best["total_return"]):
                    best = metrics
                    best_positions = positions
                    best_curve = curve
    if best is None:
        raise RuntimeError("没有可评估的参数组合")
    return best, pd.DataFrame(rows), best_positions, best_curve


def run_backtest(args: argparse.Namespace) -> int:
    end = args.end or date.today().isoformat()
    paths = HistoryPaths.build(args.symbol, args.cache_dir)
    prices, navs = load_history(
        args.symbol,
        args.start,
        end,
        paths,
        refresh=args.refresh,
    )
    frame = build_signal_frame(prices, navs)
    if frame.empty:
        raise ValueError("T-1 对齐后没有可回测数据")
    best, grid, positions, curve = grid_search(frame, args.cost)
    params = StrategyParams(
        best["buy_threshold"],
        best["sell_threshold"],
        best["low_position"],
        best["base_position"],
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "strategy": STRATEGY,
        "version": VERSION,
        "symbol": args.symbol,
        "data_policy": (
            "signal=unadjusted_close/latest_prior_unit_nav;"
            "return=synthetic_post_adjusted_close"
        ),
        "start": frame.iloc[0]["trade_date"].date().isoformat(),
        "end": frame.iloc[-1]["trade_date"].date().isoformat(),
        "observations": len(frame),
        "max_nav_age_days": int(frame["nav_age_days"].max()),
        "cost": args.cost,
        "parameters": asdict(params),
        "metrics": {
            key: value
            for key, value in best.items()
            if key not in asdict(params)
        },
    }
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else paths.root / "backtest"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output_dir / "strategy.json", result)
    write_csv_atomic(output_dir / "grid_results.csv", grid)
    equity = frame[
        [
            "trade_date",
            "close",
            "total_return_close",
            "nav_date",
            "unit_nav",
            "premium",
        ]
    ].copy()
    equity["position"] = positions
    equity["strategy_equity"] = curve
    equity["benchmark_equity"] = np.cumprod(1 + frame["ret"].to_numpy())
    write_csv_atomic(output_dir / "equity.csv", equity)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{VERSION} 回测完成: {result['start']} 至 {result['end']}")
        print(
            f"最佳参数: Low={params.low_position:.0%}, "
            f"Buy={params.buy_threshold:.2%}, Sell={params.sell_threshold:.2%}"
        )
        print(
            f"策略={best['total_return']:.2%}, 基准={best['benchmark_return']:.2%}, "
            f"超额={best['excess']:.2%}, 交易={best['trades']}"
        )
        print(f"输出: {output_dir}")
    return 0


def load_strategy(path: Path, symbol: str) -> StrategyParams:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("strategy") != STRATEGY or payload.get("version") != VERSION:
        raise ValueError("策略文件类型或版本不匹配")
    if payload.get("symbol") != symbol:
        raise ValueError("策略文件标的与 live 标的不匹配")
    return StrategyParams(**payload["parameters"])


class LiveState:
    def __init__(self, path: Path, symbol: str, initial_position: str) -> None:
        self.path = path
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("strategy") != STRATEGY or payload.get("symbol") != symbol:
                raise ValueError("实时状态文件与当前策略不匹配")
            self.position = str(payload["position"])
            self.last_snapshot = payload.get("last_snapshot")
            self.last_error = payload.get("last_error")
        else:
            self.position = initial_position
            self.last_snapshot = None
            self.last_error = None
            self.save(symbol)
        if self.position not in {"base", "low"}:
            raise ValueError("实时仓位状态只能是 base 或 low")

    def save(self, symbol: str) -> None:
        write_json_atomic(
            self.path,
            {
                "strategy": STRATEGY,
                "version": VERSION,
                "symbol": symbol,
                "position": self.position,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "last_snapshot": self.last_snapshot,
                "last_error": self.last_error,
            },
        )

    def record_snapshot(self, symbol: str, event: dict[str, Any]) -> None:
        self.last_snapshot = event
        self.last_error = None
        self.save(symbol)

    def record_error(self, symbol: str, event: dict[str, Any]) -> None:
        self.last_error = event
        self.save(symbol)


def apply_live_signal(
    current_position: str,
    t1_premium: float,
    params: StrategyParams,
) -> tuple[str, str]:
    if t1_premium > params.sell_threshold and current_position != "low":
        return "low", "SELL"
    if t1_premium < params.buy_threshold and current_position != "base":
        return "base", "BUY"
    return current_position, "NONE"


class LiveNotifier:
    """异步发送通知，避免网络或 SMTP 阻塞行情轮询。"""

    def __init__(self, engine: Any, maxsize: int = 100) -> None:
        self.engine = engine
        self.recent: dict[str, float] = {}
        self.queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(
            maxsize=maxsize
        )
        self.thread = threading.Thread(
            target=self._run,
            name="etf-premium-notifier",
            daemon=True,
        )
        self.thread.start()

    def notify(self, event: dict[str, Any]) -> None:
        key = "|".join(
            str(event.get(name, ""))
            for name in ("type", "action", "message")
        )
        now = time.monotonic()
        if now - self.recent.get(key, -1e12) < 300:
            return
        self.recent[key] = now
        try:
            self.queue.put_nowait(dict(event))
        except queue.Full:
            eprint(f"通知队列已满，丢弃事件: {key}")

    def format_event(self, event: dict[str, Any]) -> tuple[str, str]:
        label = event.get("action") or event["type"]
        subject = f"ETF溢价 {event.get('symbol', '')} {label}"
        if event["type"] == "SIGNAL":
            iopv_text = "不可用"
            if event.get("iopv_premium") is not None:
                iopv_text = f"{event['iopv_premium']:.2%}"
                if event.get("iopv_estimate") is not None:
                    iopv_text += f" (估算IOPV {event['iopv_estimate']:.4f})"
            message = (
                f"[{event['symbol']}] {event['action']}\n"
                f"市价: {event['price']:.4f}\n"
                f"可见净值: {event['unit_nav']:.4f} ({event['nav_date']}, "
                f"滞后{event['nav_age_days']}天)\n"
                f"T-1净值溢价: {event['t1_premium']:.2%}（决策）\n"
                f"盘中IOPV溢价: {iopv_text}（仅提示）\n"
                f"阈值: Buy {event['buy_threshold']:.2%} / "
                f"Sell {event['sell_threshold']:.2%}\n"
                f"目标仓位: {event['target_position']:.0%}"
            )
        else:
            message = f"[{event.get('symbol', '')}] {event['type']}\n{event.get('message', '')}"
        return subject, message

    def _run(self) -> None:
        while True:
            event = self.queue.get()
            try:
                if event is None:
                    return
                subject, message = self.format_event(event)
                self._send(subject, message)
            except Exception as exc:
                eprint(f"通知处理失败: {exc}")
            finally:
                self.queue.task_done()

    def _send(self, subject: str, message: str) -> None:
        for send in (
            self.engine.send_webhook,
            lambda text: self.engine.send_telegram_message(
                text, "https://www.futunn.com/"
            ),
            lambda text: self.engine.send_email(subject, text),
        ):
            try:
                send(message)
            except Exception as exc:
                eprint(f"通知发送失败: {exc}")

    def close(self, timeout: float = 10.0) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            eprint("通知队列未清空，退出时无法等待全部通知")
            return
        self.thread.join(timeout=timeout)


def build_notifier(config_path: Optional[str]) -> Optional[LiveNotifier]:
    if not config_path:
        return None
    config = configparser.ConfigParser()
    if not config.read(Path(config_path).expanduser().resolve(), encoding="utf-8"):
        raise ValueError("配置文件不存在或不可读")
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from notification_engine import NotificationEngine

    return LiveNotifier(NotificationEngine(config))


def live_connection(args: argparse.Namespace) -> tuple[str, int]:
    host, port = args.host, args.port
    if args.config:
        config = configparser.ConfigParser()
        config.read(Path(args.config).expanduser().resolve(), encoding="utf-8")
        host = host or config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1")
        port = port or config.getint("CONFIG", "FUTU_PORT", fallback=11111)
    return host or "127.0.0.1", port or 11111


@contextmanager
def stop_event() -> Iterator[threading.Event]:
    stopped = threading.Event()
    old_handlers = {
        sig: signal.signal(sig, lambda *_: stopped.set())
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield stopped
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def latest_prior_nav(
    navs: list[dict[str, Any]],
    trading_date: str,
) -> dict[str, Any]:
    eligible = [row for row in navs if row["date"] < trading_date]
    if not eligible:
        raise ValueError(f"{trading_date} 以前没有可用单位净值")
    return max(eligible, key=lambda row: row["date"])


def in_cn_market_session(now: datetime) -> bool:
    local = now.astimezone(MARKET_TIMEZONE)
    if local.weekday() >= 5:
        return False
    clock = local.time().replace(tzinfo=None)
    return (
        datetime_time(9, 30) <= clock <= datetime_time(11, 30)
        or datetime_time(13, 0) <= clock <= datetime_time(15, 0)
    )


def parse_quote_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text or text in {"N/A", "None"}:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MARKET_TIMEZONE)
    return parsed.astimezone(MARKET_TIMEZONE)


def parse_futu_snapshot(row: Any) -> dict[str, Any]:
    getter = row.get if hasattr(row, "get") else lambda _key, default=None: default
    price = float(getter("last_price", 0))
    if not math.isfinite(price) or price <= 0:
        raise RuntimeError(f"Futu 返回无效价格: {price}")
    iopv_premium: Optional[float] = None
    iopv_estimate: Optional[float] = None
    if bool(getter("trust_valid", False)):
        try:
            candidate = float(getter("trust_premium", math.nan)) / 100
        except (TypeError, ValueError):
            candidate = math.nan
        if math.isfinite(candidate) and candidate > -1:
            iopv_premium = candidate
            iopv_estimate = price / (1 + candidate)
    return {
        "price": price,
        "quote_time": str(getter("update_time", "") or ""),
        "iopv_premium": iopv_premium,
        "iopv_estimate": iopv_estimate,
    }


def close_futu_context(context: Any) -> None:
    if context is None:
        return
    try:
        context.close()
    except Exception as exc:
        eprint(f"警告: 关闭 Futu context 失败: {exc}")


def run_live(args: argparse.Namespace) -> int:
    try:
        from futu import OpenQuoteContext, RET_OK
    except ImportError as exc:
        raise RuntimeError("live 模式需要 futu-api") from exc

    paths = HistoryPaths.build(args.symbol, args.cache_dir)
    runtime = LiveRuntimePaths.from_argument(args.runtime_dir)
    runtime.prepare()
    strategy_file = (
        Path(args.strategy_file).expanduser().resolve()
        if args.strategy_file
        else paths.root / "backtest" / "strategy.json"
    )
    params = load_strategy(strategy_file, args.symbol)
    state = LiveState(runtime.state_file, args.symbol, args.initial_position)
    notifier = build_notifier(args.config)
    host, port = live_connection(args)
    futu_symbol = f"SZ.{args.symbol}"
    navs = read_records(paths.navs)
    next_nav_refresh = 0.0
    started = time.monotonic()
    context = None
    consecutive_errors = 0
    last_idle_log = -1e12
    running_script_hash = sha256_file(Path(__file__).resolve())
    try:
        with stop_event() as stopped:
            with RuntimeFileLock(runtime.lock_file):
                while not stopped.is_set():
                    now_monotonic = time.monotonic()
                    if args.duration and now_monotonic - started >= args.duration:
                        break
                    if (
                        sha256_file(Path(__file__).resolve())
                        != running_script_hash
                    ):
                        raise RuntimeError(
                            "脚本文件在 live 运行期间发生变化；退出并由PM2重启"
                        )
                    now = datetime.now(MARKET_TIMEZONE)
                    if not args.once and not in_cn_market_session(now):
                        stopped.wait(min(args.interval, 60.0))
                        continue

                    if now_monotonic >= next_nav_refresh:
                        try:
                            fetched = fetch_nav_history(
                                args.symbol,
                                (now.date() - timedelta(days=45)).isoformat(),
                                now.date().isoformat(),
                            )
                            navs = merge_records(navs, fetched)
                            save_records(
                                paths.navs,
                                args.symbol,
                                "eastmoney.f10.lsjz",
                                navs,
                            )
                            next_nav_refresh = now_monotonic + args.nav_refresh
                        except Exception as exc:
                            next_nav_refresh = now_monotonic + min(
                                args.nav_refresh, 60.0
                            )
                            if not navs:
                                raise RuntimeError(
                                    f"刷新净值失败且没有缓存: {exc}"
                                ) from exc
                            warning = {
                                "type": "ERROR",
                                "symbol": args.symbol,
                                "message": f"刷新净值失败，继续使用缓存: {exc}",
                                "emitted_at": now.isoformat(timespec="seconds"),
                            }
                            eprint(json.dumps(warning, ensure_ascii=False))
                            if notifier is not None:
                                notifier.notify(warning)

                    try:
                        if context is None:
                            context = OpenQuoteContext(host=host, port=port)
                        ret, data = context.get_market_snapshot([futu_symbol])
                        if ret != RET_OK or data.empty:
                            raise RuntimeError(f"Futu 快照失败: {data}")
                        snapshot = parse_futu_snapshot(data.iloc[0])
                        quote_time = parse_quote_time(snapshot["quote_time"])
                        if not args.once and (
                            quote_time is None
                            or (now - quote_time).total_seconds()
                            > args.max_quote_age
                        ):
                            if now_monotonic - last_idle_log >= 300:
                                idle = {
                                    "type": "IDLE",
                                    "symbol": args.symbol,
                                    "message": "Futu 行情未更新，跳过信号计算",
                                    "quote_time": snapshot["quote_time"],
                                    "emitted_at": now.isoformat(timespec="seconds"),
                                }
                                print(
                                    json.dumps(idle, ensure_ascii=False),
                                    flush=True,
                                )
                                last_idle_log = now_monotonic
                            consecutive_errors = 0
                            stopped.wait(args.interval)
                            continue
                        nav = latest_prior_nav(navs, now.date().isoformat())
                        nav_age_days = (
                            now.date() - date.fromisoformat(nav["date"])
                        ).days
                        if nav_age_days > args.max_nav_age:
                            raise RuntimeError(
                                f"最新可见净值已滞后 {nav_age_days} 天，"
                                f"超过上限 {args.max_nav_age} 天"
                            )
                        t1_premium = (
                            snapshot["price"] / float(nav["unit_nav"]) - 1
                        )
                        new_position, action = apply_live_signal(
                            state.position, t1_premium, params
                        )
                        state.position = new_position
                        event = {
                            "type": "SIGNAL",
                            "symbol": args.symbol,
                            "action": action,
                            "price": snapshot["price"],
                            "quote_time": snapshot["quote_time"],
                            "unit_nav": float(nav["unit_nav"]),
                            "nav_date": nav["date"],
                            "nav_age_days": nav_age_days,
                            "premium": t1_premium,
                            "t1_premium": t1_premium,
                            "iopv_premium": snapshot["iopv_premium"],
                            "iopv_estimate": snapshot["iopv_estimate"],
                            "iopv_source": "futu.trust_premium",
                            "decision_basis": "t1_premium",
                            "buy_threshold": params.buy_threshold,
                            "sell_threshold": params.sell_threshold,
                            "target_position": (
                                params.base_position
                                if new_position == "base"
                                else params.low_position
                            ),
                            "emitted_at": now.isoformat(timespec="seconds"),
                        }
                        state.record_snapshot(args.symbol, event)
                        print(
                            json.dumps(event, ensure_ascii=False),
                            flush=True,
                        )
                        if notifier is not None and action != "NONE":
                            notifier.notify(event)
                        consecutive_errors = 0
                    except Exception as exc:
                        close_futu_context(context)
                        context = None
                        consecutive_errors += 1
                        event = {
                            "type": "ERROR",
                            "symbol": args.symbol,
                            "message": str(exc),
                            "consecutive_errors": consecutive_errors,
                            "emitted_at": now.isoformat(timespec="seconds"),
                        }
                        state.record_error(args.symbol, event)
                        eprint(json.dumps(event, ensure_ascii=False))
                        if notifier is not None:
                            notifier.notify(event)
                        if (
                            args.once
                            or consecutive_errors >= args.max_errors
                        ):
                            raise
                    if args.once:
                        break
                    delay = args.interval
                    if consecutive_errors:
                        delay = min(
                            args.interval * (2 ** (consecutive_errors - 1)),
                            300.0,
                        )
                    stopped.wait(delay)
    finally:
        close_futu_context(context)
        if notifier is not None:
            notifier.close()
    return 0


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于0")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ETF T-1 净值折溢价策略")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backtest = subparsers.add_parser("backtest", help="缓存数据并执行网格回测")
    backtest.add_argument("--symbol", default=DEFAULT_SYMBOL)
    backtest.add_argument("--start", default=DEFAULT_START)
    backtest.add_argument("--end")
    backtest.add_argument("--cost", type=float, default=DEFAULT_COST)
    backtest.add_argument("--cache-dir")
    backtest.add_argument("--output-dir")
    backtest.add_argument("--refresh", action="store_true")
    backtest.add_argument("--json", action="store_true")
    backtest.set_defaults(func=run_backtest)

    live = subparsers.add_parser("live", help="Futu 实时价格监控与通知；不下单")
    live.add_argument("--symbol", default=DEFAULT_SYMBOL)
    live.add_argument("--strategy-file")
    live.add_argument("--cache-dir")
    live.add_argument("--runtime-dir", required=True)
    live.add_argument("--config", help="复用 Futu 与通知配置")
    live.add_argument("--host")
    live.add_argument("--port", type=int)
    live.add_argument("--initial-position", choices=("base", "low"), default="base")
    live.add_argument("--interval", type=positive_float, default=60.0)
    live.add_argument("--nav-refresh", type=positive_float, default=900.0)
    live.add_argument(
        "--max-nav-age",
        type=positive_int,
        default=LIVE_MAX_NAV_AGE_DAYS,
        help=f"可见净值最大滞后天数（默认 {LIVE_MAX_NAV_AGE_DAYS}）",
    )
    live.add_argument(
        "--max-quote-age",
        type=positive_float,
        default=LIVE_MAX_QUOTE_AGE_SECONDS,
        help=f"交易时段报价最大延迟秒数（默认 {LIVE_MAX_QUOTE_AGE_SECONDS}）",
    )
    live.add_argument(
        "--max-errors",
        type=positive_int,
        default=LIVE_MAX_CONSECUTIVE_ERRORS,
        help=f"连续错误退出阈值（默认 {LIVE_MAX_CONSECUTIVE_ERRORS}）",
    )
    live.add_argument("--duration", type=float, default=0)
    live.add_argument("--once", action="store_true")
    live.set_defaults(func=run_live)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if getattr(args, "cost", 0) < 0:
        raise ValueError("--cost 不能为负")
    if getattr(args, "duration", 0) < 0:
        raise ValueError("--duration 不能为负")
    if not str(args.symbol).isdigit() or len(str(args.symbol)) != 6:
        raise ValueError("--symbol 必须是6位基金代码")
    return int(args.func(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        eprint(f"错误: {exc}")
        raise SystemExit(2)
