from __future__ import annotations

import argparse
import configparser
import importlib
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"
VNPY_ROOT = DATA_ROOT / ".vntrader"
CACHE_ROOT = DATA_ROOT / "vnpy_cache"
OUTPUT_ROOT = PROJECT_ROOT / "output" / "backtest"


@dataclass(frozen=True)
class SymbolSpec:
    code: str
    vt_symbol: str
    symbol: str
    exchange_name: str


@dataclass
class BacktestConfig:
    symbols: list[str] = field(default_factory=lambda: ["US.QQQ", "US.GLD", "US.SPY", "US.FXI"])
    start: str = "2014-09-09"
    end: str = "2025-09-09"
    capital: int = 1_000_000
    rate: float = 0.001
    slippage: float = 0.01
    size: int = 1
    pricetick: float = 0.001
    annual_days: int = 250
    risk_free: float = 0
    data_source: str = "project"
    config_path: str | None = None
    refresh_data: bool = False
    proxy: str | None = None
    no_proxy: bool = False
    show_chart: bool = False
    strategy: str = "momentum_rotation_strategy:MomentumRotationStrategy"
    strategy_setting: dict[str, Any] = field(default_factory=lambda: {
        "regression_window": 22,
        "initial_capital": 1_000_000,
        "allocation": 1.0,
        "min_score": float("-inf"),
    })


def prepare_vnpy_workspace() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    VNPY_ROOT.mkdir(parents=True, exist_ok=True)
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    strategy_dir = PROJECT_ROOT / "market_analysis"
    if str(strategy_dir) not in sys.path:
        sys.path.insert(0, str(strategy_dir))

    os.environ["HOME"] = str(DATA_ROOT)
    os.chdir(DATA_ROOT)


def configure_proxy(proxy: str | None, no_proxy: bool) -> None:
    keys = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]
    if no_proxy:
        for key in keys:
            os.environ.pop(key, None)
        return

    if proxy:
        value = proxy if "://" in proxy else f"http://{proxy}"
        for key in keys:
            os.environ[key] = value


def load_project_config(path: str | None = None) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        candidates = [candidate]
    else:
        candidates = [PROJECT_ROOT / "config.ini", PROJECT_ROOT / "config_template.ini"]

    for candidate in candidates:
        if candidate.exists():
            config.read(candidate, encoding="utf-8")
            return config

    raise FileNotFoundError(f"Cannot find config file from: {candidates}")


def import_vnpy_modules():
    from vnpy.trader.constant import Exchange, Interval
    from vnpy.trader.database import get_database
    from vnpy.trader.object import BarData
    from vnpy.trader.setting import SETTINGS
    from vnpy_portfoliostrategy.backtesting import BacktestingEngine

    SETTINGS["database.name"] = "sqlite"
    SETTINGS["database.database"] = "database.db"

    return {
        "Exchange": Exchange,
        "Interval": Interval,
        "BarData": BarData,
        "BacktestingEngine": BacktestingEngine,
        "get_database": get_database,
    }


def load_strategy_class(strategy_path: str) -> type:
    if ":" in strategy_path:
        module_name, class_name = strategy_path.split(":", 1)
    else:
        module_name, class_name = strategy_path.rsplit(".", 1)

    module = importlib.import_module(module_name)
    strategy_class = getattr(module, class_name)
    return strategy_class


def parse_strategy_setting(values: list[str]) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Invalid strategy setting: {value}. Expected KEY=VALUE.")

        key, raw = value.split("=", 1)
        settings[key] = parse_scalar(raw)

    return settings


def parse_scalar(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"none", "null"}:
        return None

    try:
        return int(raw)
    except ValueError:
        pass

    try:
        return float(raw)
    except ValueError:
        return raw


def resolve_symbol(code: str, exchange_cls: Any) -> SymbolSpec:
    normalized = code.strip().upper()

    if "." in normalized:
        prefix, raw_symbol = normalized.split(".", 1)
        if prefix == "US":
            return SymbolSpec(normalized, f"{raw_symbol}.SMART", raw_symbol, "SMART")
        if prefix == "HK":
            vnpy_symbol = raw_symbol.zfill(5)
            return SymbolSpec(normalized, f"{vnpy_symbol}.SEHK", vnpy_symbol, "SEHK")
        if prefix == "SH":
            vnpy_symbol = raw_symbol.zfill(6)
            return SymbolSpec(normalized, f"{vnpy_symbol}.SSE", vnpy_symbol, "SSE")
        if prefix == "SZ":
            vnpy_symbol = raw_symbol.zfill(6)
            return SymbolSpec(normalized, f"{vnpy_symbol}.SZSE", vnpy_symbol, "SZSE")

        if prefix not in {"US", "HK", "SH", "SZ"} and raw_symbol in exchange_cls._value2member_map_:
            return SymbolSpec(normalized, normalized, prefix, raw_symbol)

    raise ValueError(f"Unsupported symbol code: {code}")


def load_project_daily(spec: SymbolSpec, config: BacktestConfig) -> pd.DataFrame:
    project_config = load_project_config(config.config_path)
    project_config.set("CONFIG", "FUTU_PUSH_TYPE", "K_DAY")

    if config.proxy:
        project_config.set("CONFIG", "PROXY", config.proxy)
    if config.no_proxy:
        project_config.set("CONFIG", "PROXY", "")

    cache_dir = CACHE_ROOT / "project"
    cache_dir.mkdir(parents=True, exist_ok=True)

    start = pd.Timestamp(config.start)
    end = pd.Timestamp(config.end)
    max_count = max(len(pd.bdate_range(start=start, end=pd.Timestamp.today())) + 30, 270)

    from data import get_kline_data

    df = get_kline_data(spec.code, project_config, max_count=max_count, file_cache_dir=str(cache_dir))
    if df is None or df.empty:
        raise RuntimeError(f"No project data for {spec.code}")

    df = normalize_ohlcv(df)
    df = df[(df.index >= start) & (df.index <= end)]
    if df.empty:
        raise RuntimeError(f"No project data for {spec.code} in {config.start} to {config.end}")

    return df


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    normalized.index = pd.to_datetime(normalized.index)
    if normalized.index.tz is not None:
        normalized.index = normalized.index.tz_localize(None)

    rename_map = {column: str(column).strip().lower() for column in normalized.columns}
    normalized = normalized.rename(columns=rename_map)
    required = ["open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in normalized.columns]
    if missing:
        raise ValueError(f"Missing OHLCV columns: {missing}")

    return normalized[required].rename(columns={
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }).dropna()


def dataframe_to_bars(df: pd.DataFrame, spec: SymbolSpec, exchange: Any, interval: Any, bar_cls: Any) -> list[Any]:
    bars: list[Any] = []
    for index, row in df.iterrows():
        ts = pd.Timestamp(index)
        if ts.tzinfo is not None:
            ts = ts.tz_convert(None)

        bars.append(
            bar_cls(
                symbol=spec.symbol,
                exchange=exchange,
                datetime=ts.to_pydatetime(),
                interval=interval,
                volume=float(row["Volume"]),
                turnover=0,
                open_interest=0,
                open_price=float(row["Open"]),
                high_price=float(row["High"]),
                low_price=float(row["Low"]),
                close_price=float(row["Close"]),
                gateway_name="YF",
            )
        )

    return bars


def ensure_history_data(config: BacktestConfig, specs: list[SymbolSpec], vnpy: dict[str, Any]) -> None:
    if config.data_source == "database":
        return
    if config.data_source != "project":
        raise ValueError(f"Unsupported data source: {config.data_source}")

    configure_proxy(config.proxy, config.no_proxy)

    exchange_cls = vnpy["Exchange"]
    interval = vnpy["Interval"].DAILY
    bar_cls = vnpy["BarData"]
    database = vnpy["get_database"]()

    for spec in specs:
        exchange = getattr(exchange_cls, spec.exchange_name)
        df = load_project_daily(spec, config)
        bars = dataframe_to_bars(df, spec, exchange, interval, bar_cls)
        if bars:
            database.save_bar_data(bars)
            print(f"Imported {len(bars):>5} bars: {spec.code} -> {spec.vt_symbol}")


def export_trades(engine: Any, path: Path) -> None:
    rows: list[dict[str, Any]] = []
    for trade in engine.trades.values():
        rows.append({
            "datetime": trade.datetime,
            "vt_symbol": trade.vt_symbol,
            "direction": trade.direction.value,
            "offset": trade.offset.value,
            "price": trade.price,
            "volume": trade.volume,
        })

    if rows:
        pd.DataFrame(rows).to_csv(path, index=False)


def run_backtest(config: BacktestConfig) -> dict[str, Any]:
    prepare_vnpy_workspace()
    strategy_class = load_strategy_class(config.strategy)
    return run_strategy_backtest(strategy_class, config)


def run_strategy_backtest(strategy_class: type, config: BacktestConfig) -> dict[str, Any]:
    prepare_vnpy_workspace()
    vnpy = import_vnpy_modules()
    exchange_cls = vnpy["Exchange"]
    specs = [resolve_symbol(symbol, exchange_cls) for symbol in config.symbols]
    vt_symbols = [spec.vt_symbol for spec in specs]

    ensure_history_data(config, specs, vnpy)

    engine = vnpy["BacktestingEngine"]()
    interval = vnpy["Interval"].DAILY
    start = datetime.strptime(config.start, "%Y-%m-%d")
    end = datetime.strptime(config.end, "%Y-%m-%d")

    engine.set_parameters(
        vt_symbols=vt_symbols,
        interval=interval,
        start=start,
        end=end,
        rates={symbol: config.rate for symbol in vt_symbols},
        slippages={symbol: config.slippage for symbol in vt_symbols},
        sizes={symbol: config.size for symbol in vt_symbols},
        priceticks={symbol: config.pricetick for symbol in vt_symbols},
        capital=config.capital,
        risk_free=config.risk_free,
        annual_days=config.annual_days,
    )

    strategy_setting = dict(config.strategy_setting)
    strategy_setting["initial_capital"] = config.capital

    engine.add_strategy(strategy_class, strategy_setting)
    engine.load_data()
    engine.run_backtesting()

    daily_df = engine.calculate_result()
    if daily_df is None or daily_df.empty:
        raise RuntimeError("Backtest produced no daily result. Check imported history data and strategy trades.")

    statistics = engine.calculate_statistics(daily_df)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    strategy_name = re.sub(r"[^0-9A-Za-z_]+", "_", strategy_class.__name__).lower()
    daily_path = OUTPUT_ROOT / f"{strategy_name}_daily_{stamp}.csv"
    trades_path = OUTPUT_ROOT / f"{strategy_name}_trades_{stamp}.csv"
    daily_df.to_csv(daily_path)
    export_trades(engine, trades_path)

    print(f"Daily result: {daily_path}")
    if trades_path.exists():
        print(f"Trades:       {trades_path}")

    if config.show_chart:
        engine.show_chart(daily_df)

    return statistics


def parse_args() -> BacktestConfig:
    parser = argparse.ArgumentParser(description="vn.py daily portfolio backtest")
    parser.add_argument("--symbols", nargs="+", default=["US.QQQ", "US.GLD", "US.SPY", "US.FXI"])
    parser.add_argument("--start", default="2014-09-09")
    parser.add_argument("--end", default="2025-09-09")
    parser.add_argument("--capital", type=int, default=1_000_000)
    parser.add_argument("--rate", type=float, default=0.001)
    parser.add_argument("--slippage", type=float, default=0.01)
    parser.add_argument("--size", type=int, default=1)
    parser.add_argument("--pricetick", type=float, default=0.001)
    parser.add_argument("--annual-days", type=int, default=250)
    parser.add_argument("--source", choices=["project", "database"], default="project")
    parser.add_argument("--config", default=None, help="Project config path. Defaults to config.ini then config_template.ini")
    parser.add_argument("--refresh-data", action="store_true")
    parser.add_argument("--proxy", default=None, help="Override project config HTTP proxy, for example 127.0.0.1:10802")
    parser.add_argument("--no-proxy", action="store_true", help="Ignore HTTP_PROXY/HTTPS_PROXY for this run")
    parser.add_argument(
        "--strategy",
        default="momentum_rotation_strategy:MomentumRotationStrategy",
        help="Strategy class path, for example momentum_rotation_strategy:MomentumRotationStrategy",
    )
    parser.add_argument(
        "--strategy-setting",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional strategy setting. Can be repeated.",
    )
    parser.add_argument("--regression-window", type=int, default=22)
    parser.add_argument("--allocation", type=float, default=1.0)
    parser.add_argument("--min-score", type=float, default=float("-inf"))
    parser.add_argument("--show-chart", action="store_true")
    args = parser.parse_args()

    strategy_setting = {
        "regression_window": args.regression_window,
        "initial_capital": args.capital,
        "allocation": args.allocation,
        "min_score": args.min_score,
    }
    strategy_setting.update(parse_strategy_setting(args.strategy_setting))

    return BacktestConfig(
        symbols=args.symbols,
        start=args.start,
        end=args.end,
        capital=args.capital,
        rate=args.rate,
        slippage=args.slippage,
        size=args.size,
        pricetick=args.pricetick,
        annual_days=args.annual_days,
        data_source=args.source,
        config_path=args.config,
        refresh_data=args.refresh_data,
        proxy=args.proxy,
        no_proxy=args.no_proxy,
        show_chart=args.show_chart,
        strategy=args.strategy,
        strategy_setting=strategy_setting,
    )


if __name__ == "__main__":
    run_backtest(parse_args())
