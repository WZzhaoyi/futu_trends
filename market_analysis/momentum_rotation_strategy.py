"""动量轮动：向量化回测/优化引擎与 Futu 实时信号监控。

回测核心为向量化模拟器（`simulate`），支撑大规模网格优化
（universe × window × 防抖参数）。扩展：
- 冷静期 N（`cooldown`）：资产->资产轮动的日历天冷却（SELL->现金 / 现金->资产 不受限）
- 分差阈值 ε（`gap_eps`）：决策日 score(新第一)-score(当前持仓) > ε 才轮动（与 N 互斥）
- 现金符号（`cash_symbols`）：登顶时全部平仓持现金（默认无）
- 滑点参数化：US $0.01/份（`DEFAULT_SLIPPAGE_US`）、CN ¥0.002/份（`DEFAULT_SLIPPAGE_CN`）

live 示例（每个交易日收盘后检测并通知一次；不下单）：
    python market_analysis/momentum_rotation_strategy.py live \
        --runtime-dir /absolute/path/runtime --config config.ini
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import math
import multiprocessing
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, time as datetime_time
from itertools import combinations
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
from tools import calc_momentum


def calculate_momentum_score(data: np.ndarray) -> float:
    if len(data) < 2 or np.any(data <= 0):
        return float("-inf")
    return float(
        calc_momentum(
            pd.Series(data, dtype=float), N=len(data), method="linear"
        ).iloc[-1]
    )


# 全时段最优配置（滑点 CN ¥0.002 / US $0.01）：
# US: QQQ SPY FXI GLD w22 N13 = 753.9% / -23.4% / Sortino 4.82（无现金符号）
# US: UUP QQQ FXI GLD w20 ε0.32 = 692.3% / -24.8% / Sortino 4.58（UUP 现金）
# CN: 159941 159949 510300 518880 w26 N10 = 1675.9% / -30.7% / Sortino 8.40

LIVE_STRATEGY = "momentum-rotation"
LIVE_VERSION = "momentum-rotation-live-v4"
LIVE_SCHEMA_VERSION = 2
US_MARKET_TIMEZONE = ZoneInfo("America/New_York")
CN_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")

DEFAULT_MODE = "optimize-universe"
DEFAULT_START = "2016-06-12"
DEFAULT_END = date.today().isoformat()
DEFAULT_CAPITAL = 1_000_000
DEFAULT_OPTIMIZATION_TARGET = "sortino_ratio"
DEFAULT_CANDIDATE_SYMBOLS = ["US.QQQ", "US.GLD", "US.SPY", "US.UUP", "US.FXI", "US.TLT"]
# DEFAULT_CANDIDATE_SYMBOLS = ["SZ.159941", "SH.518880", "SZ.159949", "SH.510300", "SH.510880"]
DEFAULT_SLIPPAGE_US = 0.01   # USD/份，美股口径
DEFAULT_SLIPPAGE_CN = 0.002  # CNY/份，A股 ETF 合理值（2 个最小报价单位）


@dataclass(frozen=True)
class LiveLeg:
    """live 单腿配置：对应回测中一个 (宇宙, window, 防抖) 配置。

    多条腿各自独立轮动、腿间零交互（资金初始划拨后不调整）。
    """
    name: str
    market: str
    symbols: tuple[str, ...]
    window: int
    cooldown: int = 0          # 与 gap_eps 互斥（同时设定报错）
    gap_eps: float = 0.0
    cash_symbols: tuple[str, ...] = ()   # 登顶时持现金的符号（现金代理，不硬编码）
    slippage: float = DEFAULT_SLIPPAGE_US


# 最终 live 组合
# CN = N10 w26 沪深300 + N16 w21 沪深300红利
# US = N13 w22 无UUP + ε32 w20 含UUP
LIVE_LEGS: tuple[LiveLeg, ...] = (
    LiveLeg(
        name="US-A", market="US",
        symbols=("US.QQQ", "US.SPY", "US.FXI", "US.GLD"),
        window=22, cooldown=13,
        slippage=DEFAULT_SLIPPAGE_US,
    ),
    LiveLeg(
        name="US-B", market="US",
        symbols=("US.UUP", "US.QQQ", "US.FXI", "US.GLD"),
        window=20, gap_eps=0.32, cash_symbols=("US.UUP",),
        slippage=DEFAULT_SLIPPAGE_US,
    ),
    LiveLeg(
        name="CN-A", market="CN",
        symbols=("SZ.159941", "SZ.159949", "SH.510300", "SH.518880"),
        window=26, cooldown=10,
        slippage=DEFAULT_SLIPPAGE_CN,
    ),
    LiveLeg(
        name="CN-B", market="CN",
        symbols=("SZ.159941", "SZ.159949", "SH.510300", "SH.510880"),
        window=21, cooldown=16,
        slippage=DEFAULT_SLIPPAGE_CN,
    ),
)

MARKET_SPECS: dict[str, dict[str, Any]] = {
    "US": {
        "timezone": US_MARKET_TIMEZONE,
        "notification_time": datetime_time(16, 10),
        "trading_symbol": "US.QQQ",
    },
    "CN": {
        "timezone": CN_MARKET_TIMEZONE,
        "notification_time": datetime_time(15, 10),
        "trading_symbol": "SZ.159941",
    },
}

DEFAULT_BACKTEST_SYMBOLS = list(LIVE_LEGS[0].symbols)
DEFAULT_UNIVERSE_SIZE = 4
DEFAULT_BENCHMARK_SYMBOL = "US.SPY"
DEFAULT_RATE = 0.001         # 佣金费率（单边）
OPTIMIZATION_WINDOWS = range(20, 31)
HISTORY_CACHE_DIR = PROJECT_ROOT / "data" / "backtest_cache" / "futu" / "source"
OUTPUT_ROOT = PROJECT_ROOT / "output" / "backtest"


@dataclass(frozen=True)
class BacktestConfig:
    symbols: list[str]
    start: str
    end: str
    config_path: Optional[str] = None
    window: int = 21
    cooldown: int = 0          # 资产->资产冷静期(日历天)；与 gap_eps 互斥
    gap_eps: float = 0.0       # 决策日分差阈值：score(新第一)-score(当前持仓)<=eps 不轮动
    cash_symbols: tuple[str, ...] = ()   # 现金符号：登顶时全部平仓持现金（不硬编码）
    capital: int = DEFAULT_CAPITAL
    rate: float = 0.001
    # 每份固定单边价格差：美股 ETF 基准 $0.01（DEFAULT_SLIPPAGE_US）；A股 ETF 基准 ¥0.002（DEFAULT_SLIPPAGE_CN）
    slippage: float = 0.01
    size: int = 1
    pricetick: float = 0.001
    annual_days: int = 250


def make_backtest_config(
    args: argparse.Namespace,
    symbols: list[str],
) -> BacktestConfig:
    cash_symbols = tuple(args.cash_symbols) if getattr(args, "cash_symbols", None) else ()
    return BacktestConfig(
        symbols=symbols,
        start=args.start,
        end=args.end,
        config_path=args.config,
        window=getattr(args, "window", 21),
        cooldown=getattr(args, "cooldown", 0),
        gap_eps=getattr(args, "eps", 0.0),
        cash_symbols=cash_symbols,
        slippage=getattr(args, "slippage", 0.01),
    )


def _normalize_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
    frame = frame.rename(columns={name: str(name).strip().title() for name in frame})
    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [name for name in required if name not in frame]
    if missing:
        raise ValueError(f"Missing OHLCV columns: {missing}")
    frame = frame[required].apply(pd.to_numeric, errors="coerce").dropna()
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def _configured_source(config: configparser.ConfigParser, symbol: str) -> str:
    market_key = f"DATA_SOURCE_{symbol.split('.', 1)[0].upper()}"
    return config.get(
        "CONFIG",
        market_key,
        fallback=config.get("CONFIG", "DATA_SOURCE", fallback=""),
    ).strip().lower()


def _futu_config(path: Optional[str], symbol: str) -> configparser.ConfigParser:
    candidates = (
        [Path(path)]
        if path
        else [PROJECT_ROOT / "config.ini", PROJECT_ROOT / "config_template.ini"]
    )
    for candidate in candidates:
        candidate = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
        if candidate.exists():
            config = configparser.ConfigParser()
            if not config.read(candidate, encoding="utf-8") or not config.has_section(
                "CONFIG"
            ):
                raise ValueError(f"配置文件不可读或缺少 CONFIG: {candidate}")
            source = _configured_source(config, symbol)
            if source != "futu":
                raise ValueError(
                    f"{symbol} 的 DATA_SOURCE 必须在配置文件中设为 futu，当前为 {source or '空'}"
                )
            config.set("CONFIG", "FUTU_PUSH_TYPE", "K_DAY")
            return config
    raise FileNotFoundError(f"Cannot find config file from: {candidates}")


def prepare_history(config: BacktestConfig) -> dict[str, pd.DataFrame]:
    """准备回测历史：优先使用共享缓存（可复现），缺失/覆盖不足时经 Futu 拉取。

    缓存优先语义保证回测结果可复现；仅当某标的缓存缺失或未覆盖
    [config.start, config.end] 时才走 get_kline_data 拉取。
    """
    from data import get_kline_data

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    start, end = pd.Timestamp(config.start), pd.Timestamp(config.end)
    symbols = list(dict.fromkeys(config.symbols + [DEFAULT_BENCHMARK_SYMBOL]))
    histories: dict[str, pd.DataFrame] = {}
    try:
        cached = load_cached_histories(symbols, start=config.start, end=config.end)
        for symbol in symbols:
            frame = cached[symbol]
            if len(frame) >= max(config.window + 10, 30):
                histories[symbol] = frame
    except (FileNotFoundError, ValueError):
        pass
    missing = [symbol for symbol in symbols if symbol not in histories]
    for symbol in missing:
        count = max(len(pd.bdate_range(start, end)) + 30, 270)
        frame = get_kline_data(
            symbol,
            _futu_config(config.config_path, symbol),
            max_count=count,
            file_cache_dir=str(HISTORY_CACHE_DIR),
        )
        if frame is None or frame.empty:
            raise RuntimeError(f"No Futu data for {symbol}")
        frame = _normalize_ohlcv(frame).loc[start:end]
        if frame.empty:
            raise RuntimeError(f"No history data for {symbol} in {config.start} to {config.end}")
        histories[symbol] = frame
    return histories


def _fill_price(side: str, limit: float, bar: pd.Series) -> Optional[float]:
    if side == "long" and limit >= bar["Low"] > 0:
        return min(limit, float(bar["Open"]))
    if side == "short" and limit <= bar["High"] and bar["High"] > 0:
        return max(limit, float(bar["Open"]))
    return None


@dataclass(frozen=True)
class SimParams:
    """向量化模拟参数（与 BacktestConfig 同构，供 run_grid 大规模搜索使用）。"""
    window: int = 21
    cooldown: int = 0          # 资产->资产冷静期(日历天)；与 gap_eps 互斥
    gap_eps: float = 0.0       # 决策日分差阈值：score(新第一)-score(当前持仓)<=eps 不轮动
    cash_symbols: tuple[str, ...] = ()   # 现金符号：登顶时全部平仓持现金
    slippage: float = DEFAULT_SLIPPAGE_US  # 每份滑点；CN 用 DEFAULT_SLIPPAGE_CN
    rate: float = DEFAULT_RATE
    pricetick: float = 0.001
    size: int = 1
    capital: float = DEFAULT_CAPITAL
    warmup_extra: int = 5      # warmup = window + warmup_extra


def load_cached_histories(
    symbols: list[str],
    cache_dir: Path = HISTORY_CACHE_DIR,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> dict[str, pd.DataFrame]:
    """从 Futu K线文件缓存加载并规范化 OHLCV 历史。

    缓存文件命名: data_{symbol.replace('.', '_')}_K_DAY_*.csv
    """
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        files = sorted(cache_dir.glob(f"data_{symbol.replace('.', '_')}_K_DAY_*.csv"))
        if not files:
            raise FileNotFoundError(f"缓存缺失: {symbol} in {cache_dir}")
        raw = pd.read_csv(files[-1], parse_dates=["time_key"], index_col="time_key")
        frame = _normalize_ohlcv(raw)
        if start:
            frame = frame.loc[pd.Timestamp(start):]
        if end:
            frame = frame.loc[: pd.Timestamp(end)]
        if frame.empty:
            raise ValueError(f"{symbol} 在 {start}~{end} 无数据")
        frames[symbol] = frame
    return frames


def align_histories(
    histories: dict[str, pd.DataFrame],
    align_to: Optional[str] = None,
    exclude: tuple[str, ...] = (),
) -> dict[str, pd.DataFrame]:
    """对齐日历。

    - align_to=None: 取除 exclude 外所有标的数据日期的交集（同市场 ETF 推荐）
    - align_to=symbol: 以该标的中日为基准，其余标的按最近收盘 ffill
      （跨市场场景，如 CN 池中加入 US.UUP）
    - exclude: 不参与对齐的标的（典型：不同交易日历的基准，如 CN 池的
      US.SPY 基准应排除，只对齐 CN 标的）
    """
    members = {s: f for s, f in histories.items() if s not in exclude}
    if align_to is None:
        common = next(iter(members.values())).index
        for frame in members.values():
            common = common.intersection(frame.index)
        return {s: frame.loc[common] for s, frame in members.items()}
    base = histories[align_to].index
    aligned = {}
    for s, frame in members.items():
        if s == align_to:
            aligned[s] = frame
        else:
            out = frame.reindex(base).ffill()
            for col in ("Open", "High", "Low"):
                out[col] = out["Close"]
            aligned[s] = out
    return aligned


def vectorized_momentum_scores(close: np.ndarray, window: int) -> np.ndarray:
    """逐日滚动 window 日线性动量分数（与 calculate_momentum_score 同口径，向量化）。

    分数 = (exp(250×日斜率) - 1) × R²。
    返回 (n_days, n_symbols)，前 window-1 行为 NaN。
    """
    n = len(close)
    out = np.full((n, close.shape[1]), np.nan)
    w = window
    mx = (w - 1) / 2.0
    varx = w * (w * w - 1) / 12.0  # Σ(x-x̄)², x=0..w-1
    L = np.log(close)
    idx = np.arange(n, dtype=float)
    for s in range(close.shape[1]):
        y = L[:, s]
        if np.isnan(y).any():
            continue
        sy = pd.Series(y).rolling(w).sum().to_numpy()
        siy = pd.Series(y * idx).rolling(w).sum().to_numpy()
        my = sy / w
        mxy = (siy - (idx - w + 1) * sy) / w  # E[x_local·y]
        my2 = pd.Series(y * y).rolling(w).mean().to_numpy()
        vary = my2 - my * my
        cov = mxy - mx * my  # 均值形式协方差
        slope = w * cov / varx
        with np.errstate(all="ignore"):
            r2 = w * cov * cov / (varx * vary)
        r2 = np.where(vary <= 0, 0.0, r2)
        out[:, s] = (np.exp(slope * 250) - 1) * r2
    return out


def verify_score_parity(
    histories: dict[str, pd.DataFrame],
    window: int = 21,
    symbols: Optional[list[str]] = None,
) -> float:
    """校验向量化分数与 calculate_momentum_score 的最大绝对误差（应 < 1e-6）。"""
    symbols = symbols or list(histories)
    closes = np.column_stack([histories[s]["Close"].to_numpy(float) for s in symbols])
    vec = vectorized_momentum_scores(closes, window)
    max_err = 0.0
    for j, s in enumerate(symbols):
        refs = np.array(
            [
                calculate_momentum_score(histories[s]["Close"].iloc[t - window + 1 : t + 1].to_numpy())
                for t in range(window - 1, len(histories[s]))
            ]
        )
        err = np.abs(refs - vec[window - 1 :, j])
        err = err[np.isfinite(refs) & np.isfinite(vec[window - 1 :, j])]
        max_err = max(max_err, err.max())
    return float(max_err)


def simulate(
    histories: dict[str, pd.DataFrame],
    symbols: list[str],
    params: SimParams = SimParams(),
    benchmark_symbol: str = DEFAULT_BENCHMARK_SYMBOL,
) -> tuple[pd.DataFrame, list[dict], dict[str, Any]]:
    """向量化模拟：全仓持有当日分数第一的标的（现金符号登顶时持现金），
    支持冷静期 N / 分差阈值 ε 防抖（互斥，不可同时设定）。

    histories: {symbol: OHLCV DataFrame}，须含 benchmark_symbol（基准可为全文
    历史、无需参与对齐，_statistics 会自行 reindex 到策略日期）。
    返回 (daily_frame, trades, statistics)。
    """
    if params.gap_eps > 0 and params.cooldown > 0:
        raise ValueError("gap_eps 与 cooldown 为互斥防抖机制，请只设置其一")
    dates = histories[symbols[0]].index
    n = len(dates)
    k = len(symbols)
    close = np.column_stack([histories[s]["Close"].to_numpy(float) for s in symbols])
    open_ = np.column_stack([histories[s]["Open"].to_numpy(float) for s in symbols])
    low = np.column_stack([histories[s]["Low"].to_numpy(float) for s in symbols])
    high = np.column_stack([histories[s]["High"].to_numpy(float) for s in symbols])
    scores = vectorized_momentum_scores(close, params.window)
    decision_start = max(params.window + params.warmup_extra, 10) - 1

    is_cash = np.array([s in params.cash_symbols for s in symbols])
    positions = np.zeros(k, dtype=np.int64)
    targets = positions.copy()
    cash = float(params.capital)
    last_price = np.zeros(k)
    orders: list[tuple[int, int, float, int]] = []  # (idx, sign, limit, vol)
    last_rotation_date = pd.Timestamp.min
    daily = np.zeros(
        n,
        dtype=[
            ("trade_count", "i4"), ("turnover", "f8"), ("commission", "f8"),
            ("slippage", "f8"), ("trading_pnl", "f8"), ("holding_pnl", "f8"),
            ("net_pnl", "f8"),
        ],
    )
    trades: list[dict] = []
    prev_close = close[0].copy()

    for t in range(n):
        start_positions = positions.copy()
        if t:
            holding_pnl = trading_pnl = turnover = slip_cost = commission = 0.0
            trade_count = 0
            filled: list[tuple[int, int, float, int]] = []
            for order in orders:
                i, sign, limit, vol = order
                fill = None
                if sign > 0 and limit >= low[t, i] > 0:
                    fill = min(limit, float(open_[t, i]))
                elif sign < 0 and limit <= high[t, i] and high[t, i] > 0:
                    fill = max(limit, float(open_[t, i]))
                if fill is None:
                    filled.append(order)
                    continue
                positions[i] += sign * vol
                cash -= sign * vol * fill * params.size
                trade_count += 1
                trading_pnl += sign * vol * (close[t, i] - fill) * params.size
                turnover += vol * fill * params.size
                slip_cost += vol * params.slippage * params.size
                trades.append(
                    {
                        "datetime": dates[t],
                        "vt_symbol": symbols[i],
                        "direction": "long" if sign > 0 else "short",
                        "volume": vol,
                        "price": fill,
                    }
                )
            orders = filled
            for i in range(k):
                holding_pnl += start_positions[i] * (close[t, i] - prev_close[i]) * params.size
            commission = turnover * params.rate
            daily[t] = (
                trade_count, turnover, commission, slip_cost,
                trading_pnl, holding_pnl,
                trading_pnl + holding_pnl - commission - slip_cost,
            )
            prev_close = close[t].copy()

        last_price = close[t]

        if t >= decision_start and not np.isnan(scores[t]).any():
            selected = int(np.nanargmax(scores[t]))
            new_targets = np.zeros(k, dtype=np.int64)
            if not is_cash[selected]:
                value = cash + np.dot(positions, last_price) * params.size
                new_targets[selected] = max(
                    int(max(value, 0) / (last_price[selected] * params.size)), 0
                )
            cur_hold = [i for i in range(k) if positions[i] > 0]
            cur_hold = cur_hold[0] if cur_hold else None
            new_hold = selected if new_targets[selected] > 0 else None
            changed = not np.array_equal(new_targets, targets)
            if changed:
                blocked = False
                if new_hold is not None and cur_hold is not None and new_hold != cur_hold:
                    if params.gap_eps > 0:
                        blocked = scores[t, selected] - scores[t, cur_hold] <= params.gap_eps
                    else:
                        blocked = (dates[t] - last_rotation_date).days <= params.cooldown
                if not blocked:
                    targets = new_targets
                    orders = []
                    for i in range(k):
                        change = targets[i] - positions[i]
                        if not change:
                            continue
                        sign = 1 if change > 0 else -1
                        limit = round(float(close[t, i]) / params.pricetick) * params.pricetick
                        orders.append((i, sign, limit, abs(int(change))))
                    last_rotation_date = dates[t]

    frame = pd.DataFrame(daily, index=dates)
    frame["balance"] = params.capital + frame["net_pnl"].cumsum()
    frame["return"] = np.log(frame["balance"] / frame["balance"].shift()).fillna(0)
    frame["highlevel"] = frame["balance"].cummax()
    frame["drawdown"] = frame["balance"] - frame["highlevel"]
    frame["ddpercent"] = frame["drawdown"] / frame["highlevel"] * 100

    config = BacktestConfig(
        symbols=symbols,
        start=str(dates[0].date()),
        end=str(dates[-1].date()),
        window=params.window,
        capital=params.capital,
        rate=params.rate,
        slippage=params.slippage,
        size=params.size,
        pricetick=params.pricetick,
    )
    frame, statistics = _statistics(frame, config, histories)
    return frame, trades, statistics


def _grid_worker(args: dict) -> dict[str, Any]:
    histories, symbols, params, benchmark_symbol, uname = (
        args["histories"], args["symbols"], args["params"], args["benchmark"], args["uname"]
    )
    frame, _, stats = simulate(histories, symbols, params, benchmark_symbol)
    return {
        "universe": uname,
        "window": params.window,
        "cooldown": params.cooldown,
        "eps": round(params.gap_eps, 4),
        "total%": round(stats["total_return"], 1),
        "maxDD%": round(stats["max_ddpercent"], 1),
        "sortino": round(stats["sortino_ratio"], 3),
        "calmar": round(stats["calmar_ratio"], 2),
        "sharpe": round(stats["sharpe_ratio"], 3),
        "rotDays": int((frame["trade_count"] > 0).sum()),
        "commission_w": round(stats["total_commission"] / 1e4, 1),
    }


def run_grid(
    histories: dict[str, pd.DataFrame],
    universes: Iterable[list[str]],
    windows: Iterable[int] = range(20, 31),
    cooldowns: Iterable[int] = (),
    epsilons: Iterable[float] = (),
    cash_symbols: tuple[str, ...] = (),
    slippage: float = DEFAULT_SLIPPAGE_US,
    benchmark_symbol: str = DEFAULT_BENCHMARK_SYMBOL,
    workers: int = 8,
) -> pd.DataFrame:
    """网格优化: universe × window × (cooldown 或 epsilon 网格)。

    cooldowns 与 epsilons 至少给一个；二者同时给出时分别生成任务。
    返回 DataFrame，可直接按 sortino/calmar/total% 排序。
    """
    tasks = []
    for uni in universes:
        uname = " ".join(s.split(".")[1] for s in uni)
        for w in windows:
            for n in cooldowns:
                tasks.append(
                    {
                        "histories": histories,
                        "symbols": list(uni),
                        "params": SimParams(window=w, cooldown=n, cash_symbols=cash_symbols, slippage=slippage),
                        "benchmark": benchmark_symbol,
                        "uname": uname,
                    }
                )
            for e in epsilons:
                tasks.append(
                    {
                        "histories": histories,
                        "symbols": list(uni),
                        "params": SimParams(window=w, gap_eps=e, cash_symbols=cash_symbols, slippage=slippage),
                        "benchmark": benchmark_symbol,
                        "uname": uname,
                    }
                )
    # fork 上下文：worker 无需重导入主模块，任意调用方均可直接使用
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(workers) as pool:
        rows = pool.map(_grid_worker, tasks)
    return pd.DataFrame(rows)


def make_universes(
    candidates: list[str],
    sizes: Iterable[int] = (4,),
    must_include: tuple[str, ...] = (),
) -> list[list[str]]:
    """生成候选宇宙组合；must_include 的标的一定在组合中。"""
    pool = [c for c in candidates if c not in must_include]
    combos = []
    for size in sizes:
        pick = size - len(must_include)
        for combo in combinations(pool, pick):
            combos.append(list(must_include) + list(combo))
    return combos


def _simulate_config(
    config: BacktestConfig,
    histories: Optional[dict[str, pd.DataFrame]] = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """以 BacktestConfig 驱动向量化 simulate（CLI 回测/优化内部助手）。

    交易日历取 config.symbols 各标的数据交集（同市场 ETF 日历一致）；
    基准单独全文传入 _statistics。跨市场场景（如 CN 池加 US.UUP）需调用方
    先 ffill 对齐。现金符号以 config.cash_symbols 为准（默认空，不硬编码）。
    """
    loaded = histories or prepare_history(config)
    symbols = list(config.symbols)
    aligned = align_histories({symbol: loaded[symbol] for symbol in symbols})
    full = {**aligned, DEFAULT_BENCHMARK_SYMBOL: loaded[DEFAULT_BENCHMARK_SYMBOL]}
    params = SimParams(
        window=config.window,
        cooldown=config.cooldown,
        gap_eps=config.gap_eps,
        cash_symbols=tuple(config.cash_symbols),
        slippage=config.slippage,
        rate=config.rate,
        pricetick=config.pricetick,
        size=config.size,
        capital=config.capital,
    )
    frame, trades, _ = simulate(full, symbols, params, DEFAULT_BENCHMARK_SYMBOL)
    return frame, trades


def _drawdown(equity: pd.Series) -> tuple[pd.Series, pd.Series]:
    drawdown = equity - equity.cummax()
    return drawdown, drawdown / equity.cummax() * 100


def _statistics(
    frame: pd.DataFrame,
    config: BacktestConfig,
    histories: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    days = len(frame)
    end_balance = float(frame["balance"].iloc[-1])
    total_return = (end_balance / config.capital - 1) * 100
    annual_return = total_return / days * config.annual_days
    return_std = float(frame["return"].std() * 100)
    daily_return = float(frame["return"].mean() * 100)
    max_ddpercent = float(frame["ddpercent"].min())
    downside = frame["balance"].pct_change().fillna(0).clip(upper=0)
    downside_deviation = math.sqrt(float(downside.pow(2).mean())) * math.sqrt(config.annual_days)
    statistics: dict[str, Any] = {
        "start_date": frame.index[0],
        "end_date": frame.index[-1],
        "total_days": days,
        "profit_days": int((frame["net_pnl"] > 0).sum()),
        "loss_days": int((frame["net_pnl"] < 0).sum()),
        "capital": config.capital,
        "end_balance": end_balance,
        "max_drawdown": float(frame["drawdown"].min()),
        "max_ddpercent": max_ddpercent,
        "total_net_pnl": float(frame["net_pnl"].sum()),
        "total_commission": float(frame["commission"].sum()),
        "total_slippage": float(frame["slippage"].sum()),
        "total_turnover": float(frame["turnover"].sum()),
        "total_trade_count": int(frame["trade_count"].sum()),
        "total_return": total_return,
        "annual_return": annual_return,
        "daily_return": daily_return,
        "return_std": return_std,
        "sharpe_ratio": daily_return / return_std * math.sqrt(config.annual_days) if return_std else 0,
        "sortino_ratio": annual_return / 100 / downside_deviation if downside_deviation else 0,
        "calmar_ratio": annual_return / abs(max_ddpercent) if max_ddpercent else 0,
        "annual_downside_deviation": downside_deviation * 100,
    }

    dates = pd.DatetimeIndex(frame.index).normalize()
    benchmark_close = histories[DEFAULT_BENCHMARK_SYMBOL]["Close"].reindex(dates).ffill()
    benchmark_equity = benchmark_close / benchmark_close.dropna().iloc[0] * config.capital
    benchmark_return = benchmark_equity.pct_change().fillna(0) * 100
    strategy_equity = pd.Series(frame["balance"].to_numpy(), index=dates)
    excess_return = strategy_equity.pct_change().fillna(0) * 100 - benchmark_return
    excess_equity = (1 + excess_return / 100).cumprod() * config.capital
    excess_drawdown, excess_ddpercent = _drawdown(excess_equity)
    frame["benchmark_symbol"] = DEFAULT_BENCHMARK_SYMBOL
    frame["benchmark_close"] = benchmark_close.to_numpy()
    frame["benchmark_balance"] = benchmark_equity.to_numpy()
    frame["benchmark_return"] = benchmark_return.to_numpy()
    frame["excess_return"] = excess_return.to_numpy()
    frame["excess_balance"] = excess_equity.to_numpy()
    frame["excess_drawdown"] = excess_drawdown.to_numpy()
    frame["excess_ddpercent"] = excess_ddpercent.to_numpy()
    span = max((dates[-1] - dates[0]).days, 1)
    annual_factor = 365 / span
    benchmark_total = (benchmark_equity.iloc[-1] / benchmark_equity.iloc[0] - 1) * 100
    benchmark_annual = ((benchmark_equity.iloc[-1] / benchmark_equity.iloc[0]) ** annual_factor - 1) * 100
    benchmark_dd = float(_drawdown(benchmark_equity)[1].min())
    benchmark_downside = (benchmark_return / 100).clip(upper=0)
    benchmark_downside_deviation = math.sqrt(float(benchmark_downside.pow(2).mean())) * math.sqrt(config.annual_days)
    statistics.update(
        {
            "benchmark_symbol": DEFAULT_BENCHMARK_SYMBOL,
            "benchmark_total_return": benchmark_total,
            "benchmark_annual_return": benchmark_annual,
            "benchmark_max_ddpercent": benchmark_dd,
            "benchmark_sortino_ratio": benchmark_annual / 100 / benchmark_downside_deviation if benchmark_downside_deviation else 0,
            "benchmark_calmar_ratio": benchmark_annual / abs(benchmark_dd) if benchmark_dd else 0,
            "excess_total_return": (strategy_equity.iloc[-1] / strategy_equity.iloc[0] - benchmark_equity.iloc[-1] / benchmark_equity.iloc[0]) * 100,
            "excess_annual_return": ((strategy_equity.iloc[-1] / strategy_equity.iloc[0]) ** annual_factor - (benchmark_equity.iloc[-1] / benchmark_equity.iloc[0]) ** annual_factor) * 100,
            "excess_max_ddpercent": float(excess_ddpercent.min()),
            "tracking_error": float(excess_return.std() * math.sqrt(config.annual_days)),
            "information_ratio": float(excess_return.mean() / excess_return.std() * math.sqrt(config.annual_days)) if excess_return.std() else 0,
        }
    )
    return frame, statistics


def _result_row(config: BacktestConfig, statistics: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": statistics[DEFAULT_OPTIMIZATION_TARGET],
        "setting": str({"regression_window": config.window}),
        "symbols": " ".join(config.symbols),
        "param_regression_window": config.window,
        **statistics,
    }


def _print_results(rows: list[dict[str, Any]], title: str, limit: int = 3) -> None:
    print(title, flush=True)
    for index, row in enumerate(rows[:limit], 1):
        print(f"{index}. {row['symbols']}", flush=True)
        print(f"   window={row['param_regression_window']}, total={row['total_return']:.2f}%, annual={row['annual_return']:.2f}%", flush=True)
        print(f"   max_dd={row['max_ddpercent']:.2f}%, Sharpe={row['sharpe_ratio']:.4f}, Sortino={row['sortino_ratio']:.4f}, Calmar={row['calmar_ratio']:.4f}", flush=True)
        print(f"   benchmark={row['benchmark_symbol']}, benchmark_total={row['benchmark_total_return']:.2f}%, excess_total={row['excess_total_return']:.2f}%\n", flush=True)


def run_momentum_backtest(config: BacktestConfig) -> dict[str, Any]:
    histories = prepare_history(config)
    frame, trades = _simulate_config(config, histories)
    frame, statistics = _statistics(frame, config, histories)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    daily_path = OUTPUT_ROOT / f"momentumrotationstrategy_daily_{stamp}.csv"
    trades_path = OUTPUT_ROOT / f"momentumrotationstrategy_trades_{stamp}.csv"
    frame.to_csv(daily_path)
    pd.DataFrame(trades).to_csv(trades_path, index=False)
    print(f"Daily result: {daily_path}\nTrades:       {trades_path}", flush=True)
    _print_results([_result_row(config, statistics)], "Backtest result:", 1)
    return statistics


def run_momentum_optimization(
    config: BacktestConfig,
    histories: Optional[dict[str, pd.DataFrame]] = None,
) -> list[dict[str, Any]]:
    histories = histories or prepare_history(config)
    started = perf_counter()
    rows = []
    for window in OPTIMIZATION_WINDOWS:
        trial = replace(config, window=window)
        frame, _ = _simulate_config(trial, histories)
        _, statistics = _statistics(frame, trial, histories)
        rows.append(_result_row(trial, statistics))
    rows.sort(key=lambda row: row["target"], reverse=True)
    path = OUTPUT_ROOT / f"momentumrotationstrategy_optimization_{datetime.now():%Y%m%d_%H%M%S_%f}.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"Optimization result: {path} (elapsed={format_elapsed(perf_counter() - started)})", flush=True)
    _print_results(rows, "Top 3 optimization results:")
    return rows


def format_elapsed(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def run_universe_optimization(args: argparse.Namespace) -> list[dict[str, Any]]:
    candidates = DEFAULT_CANDIDATE_SYMBOLS
    rows: list[dict[str, Any]] = []
    combos = list(combinations(candidates, DEFAULT_UNIVERSE_SIZE))
    started_at = perf_counter()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    progress_path = OUTPUT_ROOT / f"momentumrotationstrategy_universe_progress_{stamp}.csv"

    print(
        "Universe optimization started: "
        f"candidates={len(candidates)}, choose={DEFAULT_UNIVERSE_SIZE}, "
        f"combos={len(combos)}, target={DEFAULT_OPTIMIZATION_TARGET}, "
        f"progress={progress_path}",
        flush=True,
    )

    print(f"Preparing history data for candidate universe: {' '.join(candidates)}", flush=True)
    histories = prepare_history(make_backtest_config(args, candidates))

    for index, combo in enumerate(combos, start=1):
        symbols = list(combo)
        combo_started_at = perf_counter()
        print(f"Universe {index}/{len(combos)} started: {' '.join(symbols)}", flush=True)
        row = run_momentum_optimization(make_backtest_config(args, symbols), histories)[0]
        rows.append(row)
        pd.DataFrame(rows).sort_values("target", ascending=False).to_csv(progress_path, index=False)
        print(
            f"Universe {index}/{len(combos)} done: "
            f"target={row['target']:.6f}, elapsed={format_elapsed(perf_counter() - combo_started_at)}, "
            f"total_elapsed={format_elapsed(perf_counter() - started_at)}",
            flush=True,
        )

    path = OUTPUT_ROOT / f"momentumrotationstrategy_universe_optimization_{stamp}.csv"
    rows.sort(key=lambda row: row["target"], reverse=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    print(
        f"Universe optimization result: {path} "
        f"(elapsed={format_elapsed(perf_counter() - started_at)}, progress={progress_path})",
        flush=True,
    )
    _print_results(rows, "Top 3 universe optimization results:")
    return rows


@dataclass(frozen=True)
class LiveRuntimePaths:
    root: Path
    state_file: Path
    lock_file: Path

    @classmethod
    def from_argument(cls, raw: str, mode: str) -> "LiveRuntimePaths":
        root = Path(raw).expanduser()
        if not root.is_absolute():
            raise ValueError("--runtime-dir 必须使用绝对路径")
        root = Path(os.path.abspath(root))
        return cls(root, root / f"state-{mode}.json", root / f"{mode}.lock")

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)


class RuntimeFileLock:
    """Prevent two live processes from sharing one state directory."""

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


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def score_live_pairs(
    pairs: list[tuple[str, int]],
    closes: dict[str, list[float] | np.ndarray],
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for symbol, window in pairs:
        values = np.asarray(closes.get(symbol, ()), dtype=float)
        if len(values) < window:
            raise RuntimeError(
                f"{symbol} K线不足: 需要 {window} 根，实际 {len(values)} 根"
            )
        score = calculate_momentum_score(values[-window:])
        if not math.isfinite(score):
            raise RuntimeError(f"{symbol} 无法计算有效动量分数")
        scores[symbol] = score
    return scores


def live_leg_decision(
    leg: LiveLeg,
    scores: dict[str, float],
    previous_hold: Optional[str],
    last_rotation_date: Optional[str],
    today: date,
    min_score: float = float("-inf"),
) -> str:
    """单腿决策，镜像 simulate 的轮动语义：

    - 分数第一（含现金符号）登顶；现金符号登顶或分数低于 min_score → 持现金
    - 现金->资产 / 资产->现金 不受冷却限制；资产->资产轮动受 cooldown/ε 限制
    - 返回 INITIAL（首次）/ BUY / SELL / ROTATE / NONE
    """
    top = max(scores, key=scores.get)
    if scores[top] < min_score or top in leg.cash_symbols:
        top = None
    holding = previous_hold if previous_hold not in leg.cash_symbols else None
    if top is None:
        return "INITIAL" if holding is None and previous_hold is None else (
            "SELL" if holding is not None else "NONE"
        )
    if holding is None:
        return "INITIAL" if previous_hold is None else "BUY"
    if top == holding:
        return "NONE"
    if leg.gap_eps > 0:
        blocked = scores[top] - scores[holding] <= leg.gap_eps
    else:
        rotated_at = (
            date.fromisoformat(last_rotation_date)
            if last_rotation_date
            else today
        )
        blocked = (today - rotated_at).days <= leg.cooldown
    return "NONE" if blocked else "ROTATE"


class LiveState:
    """live 状态：每腿独立持有（持仓、上次轮动日、上次评估日）。

    腿之间零交互，状态天然按腿隔离。
    """

    @staticmethod
    def _leg_descriptor(leg: LiveLeg) -> dict[str, Any]:
        return {
            "name": leg.name,
            "market": leg.market,
            "symbols": list(leg.symbols),
            "window": leg.window,
            "cooldown": leg.cooldown,
            "gap_eps": leg.gap_eps,
            "cash_symbols": list(leg.cash_symbols),
            "slippage": leg.slippage,
        }

    def __init__(self, path: Path, legs: tuple[LiveLeg, ...]) -> None:
        self.path = path
        self.legs = legs
        expected = [self._leg_descriptor(leg) for leg in legs]
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("strategy") != LIVE_STRATEGY
                or payload.get("legs") != expected
            ):
                raise ValueError("实时状态文件与当前腿配置不匹配")
            self.leg_states = payload.get("leg_states") or {}
            self.last_snapshot = payload.get("last_snapshot")
            self.last_error = payload.get("last_error")
        else:
            self.leg_states = {
                leg.name: {
                    "selected_symbol": None,
                    "last_rotation_date": None,
                    "last_evaluation_date": None,
                }
                for leg in legs
            }
            self.last_snapshot = None
            self.last_error = None
            self.save()

    def leg_state(self, name: str) -> dict[str, Any]:
        return self.leg_states[name]

    def save(self) -> None:
        write_json_atomic(
            self.path,
            {
                "schema_version": LIVE_SCHEMA_VERSION,
                "strategy": LIVE_STRATEGY,
                "version": LIVE_VERSION,
                "legs": [self._leg_descriptor(leg) for leg in self.legs],
                "leg_states": self.leg_states,
                "updated_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "last_snapshot": self.last_snapshot,
                "last_error": self.last_error,
            },
        )

    def record_snapshot(self, event: dict[str, Any]) -> None:
        self.last_snapshot = event
        self.last_error = None
        self.save()

    def record_error(self, event: dict[str, Any]) -> None:
        self.last_error = event
        self.save()


class LiveNotifier:
    """后台线程发送通知"""

    def __init__(self, engine: Any, maxsize: int = 100) -> None:
        self.engine = engine
        self.recent: dict[str, float] = {}
        self.queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(
            maxsize=maxsize
        )
        self.thread = threading.Thread(
            target=self._run,
            name="momentum-rotation-notifier",
            daemon=True,
        )
        self.thread.start()

    def notify(self, event: dict[str, Any]) -> None:
        key = "|".join(
            str(event.get(name, ""))
            for name in ("type", "action", "market", "leg", "selected_symbol", "message")
        )
        now = time.monotonic()
        if now - self.recent.get(key, -1e12) < 300:
            return
        self.recent[key] = now
        try:
            self.queue.put_nowait(dict(event))
        except queue.Full:
            eprint(f"通知队列已满，丢弃事件: {key}")

    @staticmethod
    def format_event(event: dict[str, Any]) -> tuple[str, str]:
        action = event.get("action") or event["type"]
        subject = f"动量轮动 {action}"
        if event["type"] != "SIGNAL":
            return subject, f"动量轮动 {event['type']}\n{event.get('message', '')}"

        target = event.get("target_symbol") or "CASH"
        lines = [
            f"动量轮动 {event['action']}",
            f"市场: {event['market']} 腿: {event['leg']}",
            f"交易日: {event['evaluation_date']}",
            f"提示时间: {event['notification_time']} {event['timezone']}",
            f"目标: {target}",
            f"排名首位: {event.get('selected_symbol') or '无'}",
            "动量排名:",
        ]
        for row in event["ranking"]:
            lines.append(
                f"- {row['symbol']} window={row['window']}: "
                f"{row['score']:.2%}"
            )
        return subject, "\n".join(lines)

    def _run(self) -> None:
        while True:
            event = self.queue.get()
            try:
                if event is None:
                    return
                subject, message = self.format_event(event)
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
            except Exception as exc:
                eprint(f"通知处理失败: {exc}")
            finally:
                self.queue.task_done()

    def close(self, timeout: float = 10.0) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            eprint("通知队列未清空，退出时无法等待全部通知")
            return
        self.thread.join(timeout=timeout)


def build_live_notifier(config_path: Optional[str]) -> Optional[LiveNotifier]:
    if not config_path:
        return None
    config = configparser.ConfigParser()
    path = Path(config_path).expanduser().resolve()
    if not config.read(path, encoding="utf-8"):
        raise ValueError("配置文件不存在或不可读")
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from notification_engine import NotificationEngine

    return LiveNotifier(NotificationEngine(config))


def resolve_live_connection(
    config_path: Optional[str],
    symbols: list[str],
) -> tuple[str, int]:
    if not config_path:
        raise ValueError("live 模式必须指定 --config")
    config = _futu_config(config_path, symbols[0])
    for symbol in symbols[1:]:
        if _configured_source(config, symbol) != "futu":
            raise ValueError(f"{symbol} 的 DATA_SOURCE 必须在配置文件中设为 futu")
    host = config.get("CONFIG", "FUTU_HOST", fallback="").strip()
    port = config.getint("CONFIG", "FUTU_PORT", fallback=0)
    if not host or not 1 <= port <= 65535:
        raise ValueError("配置文件必须提供有效的 FUTU_HOST 和 FUTU_PORT")
    return host, port


def parse_futu_quote_time(
    value: Any,
    market_timezone: ZoneInfo,
) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text or text in {"N/A", "None"}:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=market_timezone)
    return parsed.astimezone(market_timezone)


def subscribe_live_klines(
    context: Any,
    symbols: list[str],
    ret_ok: Any,
    subtype: Any,
) -> None:
    ret, data = context.subscribe(
        symbols,
        [subtype.K_DAY],
        is_first_push=False,
    )
    if ret != ret_ok:
        raise RuntimeError(f"Futu 日K订阅失败: {data}")


def is_live_trading_day(
    context: Any,
    symbol: str,
    trading_date: str,
    ret_ok: Any,
) -> bool:
    ret, days = context.request_trading_days(
        start=trading_date,
        end=trading_date,
        code=symbol,
    )
    if ret != ret_ok:
        raise RuntimeError(f"Futu 交易日查询失败: {days}")
    return any(str(day.get("time", ""))[:10] == trading_date for day in days)


def fetch_live_market_data(
    context: Any,
    pairs: list[tuple[str, int]],
    ret_ok: Any,
    kl_type: Any,
    au_type: Any,
) -> tuple[dict[str, list[float]], dict[str, dict[str, Any]]]:
    symbols = [symbol for symbol, _window in pairs]
    ret, snapshots = context.get_market_snapshot(symbols)
    if ret != ret_ok or snapshots.empty:
        raise RuntimeError(f"Futu 快照失败: {snapshots}")

    snapshot_by_symbol: dict[str, dict[str, Any]] = {}
    for _, row in snapshots.iterrows():
        symbol = str(row.get("code", "")).upper()
        price = float(row.get("last_price", 0))
        if symbol and math.isfinite(price) and price > 0:
            snapshot_by_symbol[symbol] = {
                "price": price,
                "quote_time": str(row.get("update_time", "") or ""),
            }

    closes: dict[str, list[float]] = {}
    for symbol, window in pairs:
        snapshot = snapshot_by_symbol.get(symbol)
        if snapshot is None:
            raise RuntimeError(f"Futu 快照缺少 {symbol} 或价格无效")
        ret, frame = context.get_cur_kline(
            symbol,
            window,
            kl_type.K_DAY,
            au_type.QFQ,
        )
        if ret != ret_ok or frame.empty:
            raise RuntimeError(f"Futu 日K失败 {symbol}: {frame}")
        if "time_key" not in frame.columns:
            raise RuntimeError(f"Futu 日K缺少 time_key: {symbol}")
        bar_date = str(frame["time_key"].iloc[-1]).split(" ", 1)[0]
        try:
            date.fromisoformat(bar_date)
        except ValueError as exc:
            raise RuntimeError(
                f"Futu 日K日期无效 {symbol}: {bar_date}"
            ) from exc
        values = pd.to_numeric(frame["close"], errors="coerce").dropna().tolist()
        if len(values) < window:
            raise RuntimeError(
                f"{symbol} K线不足: 需要 {window} 根，实际 {len(values)} 根"
            )
        values = [float(value) for value in values[-window:]]
        values[-1] = snapshot["price"]
        closes[symbol] = values
        snapshot["bar_date"] = bar_date
    return closes, snapshot_by_symbol


def close_futu_context(context: Any) -> None:
    if context is None:
        return
    try:
        context.close()
    except Exception as exc:
        eprint(f"警告: 关闭 Futu context 失败: {exc}")


def market_union_pairs(legs: tuple[LiveLeg, ...]) -> list[tuple[str, int]]:
    """市场内全部符号的 (symbol, 该市场最大所需窗口)：一次拉取、按腿切片。"""
    windows: dict[str, int] = {}
    for leg in legs:
        for symbol in leg.symbols:
            windows[symbol] = max(windows.get(symbol, 0), leg.window)
    return list(windows.items())


def run_live(args: argparse.Namespace) -> int:
    """live（cron_restart 定时触发）：对指定市场做一次即时评估并通知。

    触发时机由 PM2 ecosystem 的 cron 表达式保证，脚本只做：
    交易日守卫（非交易日发 IDLE 不通知）、对最近收盘的即时评估、通知。
    --markets 限定本次评估的市场（cron 分市场触发的必要条件，默认全部）。
    """
    try:
        from futu import AuType, OpenQuoteContext, RET_OK, SubType
    except ImportError as exc:
        raise RuntimeError("live 模式需要 futu-api") from exc

    legs_by_market: dict[str, list[LiveLeg]] = {}
    for leg in LIVE_LEGS:
        legs_by_market.setdefault(leg.market, []).append(leg)
    if args.markets:
        requested = {
            market.strip().upper()
            for market in str(args.markets).split(",")
            if market.strip()
        }
        unknown = requested - set(MARKET_SPECS)
        if unknown:
            raise ValueError(f"未知市场: {sorted(unknown)}（可选: {sorted(MARKET_SPECS)}）")
        markets = tuple(m for m in legs_by_market if m in requested)
    else:
        markets = tuple(legs_by_market)

    runtime = LiveRuntimePaths.from_argument(args.runtime_dir, "live")
    runtime.prepare()
    state = LiveState(runtime.state_file, LIVE_LEGS)
    market_symbols = [
        symbol
        for market in markets
        for leg in legs_by_market[market]
        for symbol in leg.symbols
    ]
    host, port = resolve_live_connection(args.config, market_symbols)
    notifier = build_live_notifier(args.config)
    context = None
    subscribed_markets: set[str] = set()

    try:
        with RuntimeFileLock(runtime.lock_file):
            for market in markets:
                spec = MARKET_SPECS[market]
                market_timezone = spec["timezone"]
                now = datetime.now(market_timezone)
                today = now.date().isoformat()
                market_legs = legs_by_market[market]
                try:
                    if context is None:
                        context = OpenQuoteContext(host=host, port=port)
                        subscribed_markets.clear()
                    if not is_live_trading_day(
                        context, spec["trading_symbol"], today, RET_OK
                    ):
                        event = {
                            "type": "IDLE",
                            "strategy": LIVE_STRATEGY,
                            "market": market,
                            "message": "今日为非交易日，跳过信号计算",
                            "evaluation_date": today,
                            "emitted_at": now.isoformat(timespec="seconds"),
                        }
                        for leg in market_legs:
                            state.leg_state(leg.name)[
                                "last_evaluation_date"
                            ] = today
                        state.record_snapshot(event)
                        print(json.dumps(event, ensure_ascii=False), flush=True)
                        continue
                    pairs = market_union_pairs(market_legs)
                    if market not in subscribed_markets:
                        subscribe_live_klines(
                            context,
                            [symbol for symbol, _window in pairs],
                            RET_OK,
                            SubType,
                        )
                        subscribed_markets.add(market)

                    closes, snapshots = fetch_live_market_data(
                        context,
                        pairs,
                        RET_OK,
                        SubType,
                        AuType,
                    )
                    bar_dates = {
                        snapshot["bar_date"]
                        for snapshot in snapshots.values()
                    }
                    if len(bar_dates) != 1:
                        raise RuntimeError(
                            "ETF 最新日K日期不一致: "
                            + ", ".join(
                                f"{symbol}={snapshot['bar_date']}"
                                for symbol, snapshot in snapshots.items()
                            )
                        )
                    trading_date = next(iter(bar_dates))

                    for leg in market_legs:
                        leg_pairs = [
                            (symbol, leg.window)
                            for symbol in leg.symbols
                        ]
                        scores = score_live_pairs(leg_pairs, closes)
                        top = max(scores, key=scores.get)
                        selected_symbol = (
                            top
                            if scores[top] >= args.min_score
                            else None
                        )
                        lst = state.leg_state(leg.name)
                        action = live_leg_decision(
                            leg,
                            scores,
                            lst["selected_symbol"],
                            lst["last_rotation_date"],
                            today,
                            args.min_score,
                        )
                        target_symbol = (
                            selected_symbol
                            if selected_symbol
                            not in leg.cash_symbols
                            else None
                        )
                        windows = dict(leg_pairs)
                        ranking = [
                            {
                                "symbol": symbol,
                                "window": windows[symbol],
                                "score": score,
                                "price": snapshots[symbol]["price"],
                                "quote_time": snapshots[symbol][
                                    "quote_time"
                                ],
                                "bar_date": snapshots[symbol]["bar_date"],
                            }
                            for symbol, score in sorted(
                                scores.items(),
                                key=lambda item: item[1],
                                reverse=True,
                            )
                        ]
                        event = {
                            "type": "SIGNAL",
                            "strategy": LIVE_STRATEGY,
                            "market": market,
                            "leg": leg.name,
                            "evaluation_date": trading_date,
                            "notification_time": spec[
                                "notification_time"
                            ].strftime("%H:%M"),
                            "timezone": str(market_timezone),
                            "action": action,
                            "previous_symbol": lst["selected_symbol"],
                            "selected_symbol": selected_symbol,
                            "target_symbol": target_symbol,
                            "cash_signal": (
                                selected_symbol is None
                                or selected_symbol in leg.cash_symbols
                            ),
                            "min_score": (
                                args.min_score
                                if math.isfinite(args.min_score)
                                else None
                            ),
                            "ranking": ranking,
                            "emitted_at": now.isoformat(
                                timespec="seconds"
                            ),
                        }
                        lst["selected_symbol"] = selected_symbol
                        if action == "ROTATE":
                            lst["last_rotation_date"] = trading_date
                        lst["last_evaluation_date"] = trading_date
                        state.record_snapshot(event)
                        print(
                            json.dumps(event, ensure_ascii=False),
                            flush=True,
                        )
                        if notifier is not None:
                            notifier.notify(event)
                except Exception as exc:
                    close_futu_context(context)
                    context = None
                    subscribed_markets.clear()
                    event = {
                        "type": "ERROR",
                        "strategy": LIVE_STRATEGY,
                        "market": market,
                        "message": str(exc),
                        "emitted_at": now.isoformat(timespec="seconds"),
                    }
                    state.record_error(event)
                    eprint(json.dumps(event, ensure_ascii=False))
                    if notifier is not None:
                        notifier.notify(event)
                    raise
    finally:
        close_futu_context(context)
        if notifier is not None:
            notifier.close()
    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Momentum rotation backtest/optimize and Futu live signal evaluation"
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["backtest", "optimize", "optimize-universe", "live"],
        default=DEFAULT_MODE,
        help="live: 各腿动量信号（cron_restart 定时触发）",
    )
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--config", default=None)
    parser.add_argument("--window", type=int, default=21, help="动量窗口（回测/优化）")
    parser.add_argument("--cooldown", type=int, default=0, help="资产->资产冷静期（日历天）")
    parser.add_argument("--eps", type=float, default=0.0, help="决策日分差阈值（与 --cooldown 互斥）")
    parser.add_argument("--cash-symbols", default=None, help="现金符号（逗号分隔，如 US.UUP；默认空）")
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE_US, help="每份单边滑点（CN 用 0.002）")
    parser.add_argument("--symbols", default=None, help="回测/优化标的（逗号分隔，覆盖预设；含 US.SPY 作为基准）")
    parser.add_argument("--min-score", type=float, default=float("-inf"))
    parser.add_argument("--markets", default=None, help="live 限定市场（逗号分隔，如 CN,US；默认全部）")
    parser.add_argument("--runtime-dir")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    if math.isnan(args.min_score):
        raise ValueError("--min-score 不能是 NaN")
    if args.mode == "live":
        if not args.runtime_dir:
            raise ValueError("live 模式必须指定 --runtime-dir")
        if not args.config:
            raise ValueError("live 模式必须指定 --config")
        return run_live(args)

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = DEFAULT_BACKTEST_SYMBOLS
    if args.mode == "optimize-universe":
        run_universe_optimization(args)
    elif args.mode == "optimize":
        run_momentum_optimization(make_backtest_config(args, symbols))
    else:
        run_momentum_backtest(make_backtest_config(args, symbols))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        eprint(f"错误: {exc}")
        raise SystemExit(2)
