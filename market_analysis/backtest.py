from __future__ import annotations

import ast
import configparser
import os
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"
VNPY_ROOT = DATA_ROOT / ".vntrader"
CACHE_ROOT = DATA_ROOT / "vnpy_cache"
OUTPUT_ROOT = PROJECT_ROOT / "output" / "backtest"
CUSTOM_OPTIMIZATION_TARGETS = {"sortino_ratio", "calmar_ratio"}


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
    benchmark_symbol: str | None = None
    strategy_setting: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OptimizationParameter:
    name: str
    start: float
    end: float | None = None
    step: float | None = None


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
    from vnpy.trader.optimize import OptimizationSetting
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
        "OptimizationSetting": OptimizationSetting,
        "get_database": get_database,
    }


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


def history_specs(config: BacktestConfig, vnpy: dict[str, Any]) -> list[SymbolSpec]:
    symbols = list(dict.fromkeys(config.symbols + ([config.benchmark_symbol] if config.benchmark_symbol else [])))
    return resolve_specs(symbols, vnpy)


def load_benchmark_close(config: BacktestConfig, vnpy: dict[str, Any]) -> pd.Series:
    if not config.benchmark_symbol:
        return pd.Series(dtype=float)

    spec = resolve_specs([config.benchmark_symbol], vnpy)[0]
    exchange = getattr(vnpy["Exchange"], spec.exchange_name)
    database = vnpy["get_database"]()
    bars = database.load_bar_data(
        spec.symbol,
        exchange,
        vnpy["Interval"].DAILY,
        datetime.strptime(config.start, "%Y-%m-%d"),
        datetime.strptime(config.end, "%Y-%m-%d"),
    )
    if not bars:
        raise RuntimeError(f"No benchmark data for {config.benchmark_symbol}")

    index = pd.to_datetime([bar.datetime for bar in bars])
    if index.tz is not None:
        index = index.tz_localize(None)

    close = pd.Series(
        [bar.close_price for bar in bars],
        index=index.normalize(),
        name="benchmark_close",
        dtype=float,
    )
    return close.sort_index()


def calculate_drawdown(equity: pd.Series) -> tuple[pd.Series, pd.Series]:
    highlevel = equity.cummax()
    drawdown = equity - highlevel
    ddpercent = drawdown / highlevel * 100
    return drawdown, ddpercent


def add_benchmark_metrics(daily_df: pd.DataFrame, config: BacktestConfig, vnpy: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, float]]:
    if not config.benchmark_symbol:
        return daily_df, {}

    df = daily_df.copy()
    dates = pd.to_datetime(df.index).normalize()
    benchmark_close = load_benchmark_close(config, vnpy).reindex(dates).ffill()
    if benchmark_close.isna().all():
        raise RuntimeError(f"No aligned benchmark data for {config.benchmark_symbol}")

    first_close = benchmark_close.dropna().iloc[0]
    benchmark_equity = benchmark_close / first_close * config.capital
    benchmark_return = benchmark_equity.pct_change().fillna(0) * 100

    strategy_equity = pd.Series(df["balance"].astype(float).to_numpy(), index=dates, name="strategy_balance")
    strategy_return = strategy_equity.pct_change().fillna(0) * 100
    excess_return = strategy_return - benchmark_return
    excess_equity = (1 + excess_return / 100).cumprod() * config.capital
    excess_drawdown, excess_ddpercent = calculate_drawdown(excess_equity)

    df["benchmark_symbol"] = config.benchmark_symbol
    df["benchmark_close"] = benchmark_close.to_numpy()
    df["benchmark_balance"] = benchmark_equity.to_numpy()
    df["benchmark_return"] = benchmark_return.to_numpy()
    df["excess_return"] = excess_return.to_numpy()
    df["excess_balance"] = excess_equity.to_numpy()
    df["excess_drawdown"] = excess_drawdown.to_numpy()
    df["excess_ddpercent"] = excess_ddpercent.to_numpy()

    total_days = max((dates[-1] - dates[0]).days, 1)
    annual_factor = 365 / total_days
    benchmark_total_return = (benchmark_equity.iloc[-1] / benchmark_equity.iloc[0] - 1) * 100
    benchmark_annual_return = ((benchmark_equity.iloc[-1] / benchmark_equity.iloc[0]) ** annual_factor - 1) * 100
    benchmark_max_ddpercent = calculate_drawdown(benchmark_equity)[1].min()
    benchmark_downside = (benchmark_return / 100 - (config.risk_free / 100) / config.annual_days).clip(upper=0)
    benchmark_downside_deviation = (benchmark_downside.pow(2).mean() ** 0.5) * (config.annual_days ** 0.5)
    excess_total_return = (strategy_equity.iloc[-1] / strategy_equity.iloc[0] - benchmark_equity.iloc[-1] / benchmark_equity.iloc[0]) * 100
    tracking_error = excess_return.std() * (config.annual_days ** 0.5)
    information_ratio = (
        excess_return.mean() / excess_return.std() * (config.annual_days ** 0.5)
        if excess_return.std()
        else 0
    )

    statistics = {
        "benchmark_symbol": config.benchmark_symbol,
        "benchmark_total_return": benchmark_total_return,
        "benchmark_annual_return": benchmark_annual_return,
        "benchmark_max_ddpercent": benchmark_max_ddpercent,
        "benchmark_sortino_ratio": (benchmark_annual_return / 100 - config.risk_free / 100) / benchmark_downside_deviation if benchmark_downside_deviation else 0,
        "benchmark_calmar_ratio": benchmark_annual_return / abs(benchmark_max_ddpercent) if benchmark_max_ddpercent else 0,
        "excess_total_return": excess_total_return,
        "excess_annual_return": ((strategy_equity.iloc[-1] / strategy_equity.iloc[0]) ** annual_factor - (benchmark_equity.iloc[-1] / benchmark_equity.iloc[0]) ** annual_factor) * 100,
        "excess_max_ddpercent": excess_ddpercent.min(),
        "tracking_error": tracking_error,
        "information_ratio": information_ratio,
    }
    return df, statistics


def benchmark_statistics(config: BacktestConfig, vnpy: dict[str, Any]) -> dict[str, float]:
    if not config.benchmark_symbol:
        return {}

    close = load_benchmark_close(config, vnpy)
    if close.empty:
        return {}

    equity = close / close.iloc[0] * config.capital
    dates = pd.to_datetime(equity.index).normalize()
    total_days = max((dates[-1] - dates[0]).days, 1)
    annual_factor = 365 / total_days
    total_return = (equity.iloc[-1] / equity.iloc[0] - 1) * 100
    annual_return = ((equity.iloc[-1] / equity.iloc[0]) ** annual_factor - 1) * 100
    max_ddpercent = calculate_drawdown(equity)[1].min()
    daily_return = equity.pct_change().fillna(0)
    target_daily_return = (config.risk_free / 100) / config.annual_days
    downside = (daily_return - target_daily_return).clip(upper=0)
    downside_deviation = (downside.pow(2).mean() ** 0.5) * (config.annual_days ** 0.5)
    return {
        "benchmark_symbol": config.benchmark_symbol,
        "benchmark_total_return": total_return,
        "benchmark_annual_return": annual_return,
        "benchmark_max_ddpercent": max_ddpercent,
        "benchmark_sortino_ratio": (annual_return / 100 - config.risk_free / 100) / downside_deviation if downside_deviation else 0,
        "benchmark_calmar_ratio": annual_return / abs(max_ddpercent) if max_ddpercent else 0,
    }


def add_benchmark_statistics(statistics: dict[str, Any], benchmark: dict[str, float]) -> dict[str, Any]:
    if not benchmark:
        return statistics

    enriched = dict(statistics)
    enriched.update(benchmark)
    total_return = enriched.get("total_return")
    annual_return = enriched.get("annual_return")
    if total_return is not None:
        enriched["excess_total_return"] = total_return - benchmark["benchmark_total_return"]
    if annual_return is not None:
        enriched["excess_annual_return"] = annual_return - benchmark["benchmark_annual_return"]
    return enriched


def calculate_risk_ratios(daily_df: pd.DataFrame, statistics: dict[str, Any], config: BacktestConfig) -> dict[str, float]:
    balance = pd.Series(daily_df["balance"].astype(float).to_numpy(), index=pd.to_datetime(daily_df.index))
    daily_return = balance.pct_change().fillna(0)
    target_daily_return = (config.risk_free / 100) / config.annual_days
    downside = (daily_return - target_daily_return).clip(upper=0)
    downside_deviation = (downside.pow(2).mean() ** 0.5) * (config.annual_days ** 0.5)

    annual_excess_return = statistics.get("annual_return", 0) / 100 - config.risk_free / 100
    sortino_ratio = annual_excess_return / downside_deviation if downside_deviation else 0

    max_ddpercent = abs(statistics.get("max_ddpercent", 0))
    calmar_ratio = statistics.get("annual_return", 0) / max_ddpercent if max_ddpercent else 0

    return {
        "sortino_ratio": sortino_ratio,
        "calmar_ratio": calmar_ratio,
        "annual_downside_deviation": downside_deviation * 100,
    }


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


def export_optimization_results(results: list[tuple], path: Path) -> None:
    rows: list[dict[str, Any]] = []
    for setting_text, target_value, statistics in results:
        row: dict[str, Any] = {
            "setting": setting_text,
            "target": target_value,
        }

        setting = parse_setting_text(setting_text)

        for key, value in setting.items():
            row[f"param_{key}"] = value

        row.update(statistics)
        rows.append(row)

    pd.DataFrame(rows).to_csv(path, index=False)


def parse_setting_text(setting_text: str) -> dict[str, Any]:
    try:
        safe_setting_text = re.sub(r"(?<![\w.])-inf(?![\w.])", "-1e309", setting_text)
        safe_setting_text = re.sub(r"(?<![\w.])inf(?![\w.])", "1e309", safe_setting_text)
        setting = ast.literal_eval(safe_setting_text)
    except (SyntaxError, ValueError):
        return {}

    return setting if isinstance(setting, dict) else {}


def build_optimization_setting(
    optimization_cls: type,
    strategy_setting: dict[str, Any],
    parameters: list[OptimizationParameter],
    target: str,
) -> Any:
    optimization_setting = optimization_cls()
    optimized_names = {parameter.name for parameter in parameters}

    for key, value in strategy_setting.items():
        if key not in optimized_names:
            optimization_setting.add_parameter(key, value)

    for parameter in parameters:
        ok, message = optimization_setting.add_parameter(
            parameter.name,
            parameter.start,
            parameter.end,
            parameter.step,
        )
        if not ok:
            raise ValueError(f"Invalid optimization parameter {parameter.name}: {message}")

    optimization_setting.set_target(target)
    return optimization_setting


def configure_engine(engine: Any, vnpy: dict[str, Any], vt_symbols: list[str], config: BacktestConfig) -> None:
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


def resolve_specs(symbols: list[str], vnpy: dict[str, Any]) -> list[SymbolSpec]:
    exchange_cls = vnpy["Exchange"]
    return [resolve_symbol(symbol, exchange_cls) for symbol in symbols]


def prepare_history_data(config: BacktestConfig) -> None:
    prepare_vnpy_workspace()
    vnpy = import_vnpy_modules()
    specs = history_specs(config, vnpy)
    ensure_history_data(config, specs, vnpy)


def prepare_engine(strategy_class: type, config: BacktestConfig, vnpy: dict[str, Any]) -> Any:
    specs = history_specs(config, vnpy)
    vt_symbols = [spec.vt_symbol for spec in specs]

    ensure_history_data(config, specs, vnpy)
    strategy_specs = resolve_specs(config.symbols, vnpy)
    vt_symbols = [spec.vt_symbol for spec in strategy_specs]

    engine = vnpy["BacktestingEngine"]()
    configure_engine(engine, vnpy, vt_symbols, config)

    strategy_setting = dict(config.strategy_setting)
    strategy_setting["initial_capital"] = config.capital
    engine.add_strategy(strategy_class, strategy_setting)
    return engine


def evaluate_strategy_statistics(strategy_class: type, config: BacktestConfig, vnpy: dict[str, Any]) -> dict[str, Any]:
    engine = prepare_engine(strategy_class, config, vnpy)
    engine.load_data()
    engine.run_backtesting()

    daily_df = engine.calculate_result()
    if daily_df is None or daily_df.empty:
        raise RuntimeError("Backtest produced no daily result. Check imported history data and strategy trades.")

    statistics = engine.calculate_statistics(daily_df, output=False)
    statistics.update(calculate_risk_ratios(daily_df, statistics, config))
    _, benchmark_stats = add_benchmark_metrics(daily_df, config, vnpy)
    statistics.update(benchmark_stats)
    return statistics


def enrich_optimization_results(
    strategy_class: type,
    config: BacktestConfig,
    vnpy: dict[str, Any],
    results: list[tuple],
    target: str,
) -> list[tuple]:
    enriched_results: list[tuple] = []
    for setting_text, target_value, statistics in results:
        setting = parse_setting_text(setting_text)
        if setting:
            result_config = replace(config, strategy_setting=setting)
            statistics = evaluate_strategy_statistics(strategy_class, result_config, vnpy)

        if target in statistics:
            target_value = statistics[target]

        enriched_results.append((setting_text, target_value, statistics))

    enriched_results.sort(reverse=True, key=lambda result: result[1])
    return enriched_results


def run_strategy_backtest(strategy_class: type, config: BacktestConfig) -> dict[str, Any]:
    prepare_vnpy_workspace()
    vnpy = import_vnpy_modules()
    engine = prepare_engine(strategy_class, config, vnpy)
    engine.load_data()
    engine.run_backtesting()

    daily_df = engine.calculate_result()
    if daily_df is None or daily_df.empty:
        raise RuntimeError("Backtest produced no daily result. Check imported history data and strategy trades.")

    statistics = engine.calculate_statistics(daily_df)
    statistics.update(calculate_risk_ratios(daily_df, statistics, config))
    daily_df, benchmark_stats = add_benchmark_metrics(daily_df, config, vnpy)
    statistics.update(benchmark_stats)

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


def run_strategy_optimization(
    strategy_class: type,
    config: BacktestConfig,
    parameters: list[OptimizationParameter],
    target: str = "sharpe_ratio",
    method: str = "bf",
    max_workers: int | None = None,
    ga_ngen_size: int = 30,
) -> list[tuple]:
    if not parameters:
        raise ValueError(
            "No optimization parameters configured. "
            "Pass --optimization-parameter KEY=START:END:STEP or define them in the strategy script."
        )
    if target in CUSTOM_OPTIMIZATION_TARGETS and method != "bf":
        raise ValueError(f"{target} optimization is only supported with brute force method.")

    prepare_vnpy_workspace()
    vnpy = import_vnpy_modules()
    engine = prepare_engine(strategy_class, config, vnpy)
    engine_target = "sharpe_ratio" if target in CUSTOM_OPTIMIZATION_TARGETS else target

    strategy_setting = dict(config.strategy_setting)
    strategy_setting["initial_capital"] = config.capital
    optimization_setting = build_optimization_setting(
        vnpy["OptimizationSetting"],
        strategy_setting,
        parameters,
        engine_target,
    )

    if method == "bf":
        results = engine.run_bf_optimization(optimization_setting, max_workers=max_workers)
    elif method == "ga":
        results = engine.run_ga_optimization(
            optimization_setting,
            max_workers=max_workers,
            ngen_size=ga_ngen_size,
        )
    else:
        raise ValueError(f"Unsupported optimization method: {method}")

    if not results:
        raise RuntimeError("Optimization produced no results.")

    results = enrich_optimization_results(strategy_class, config, vnpy, results, target)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    strategy_name = re.sub(r"[^0-9A-Za-z_]+", "_", strategy_class.__name__).lower()
    optimization_path = OUTPUT_ROOT / f"{strategy_name}_optimization_{stamp}.csv"
    export_optimization_results(results, optimization_path)
    print(f"Optimization result: {optimization_path}")
    return results
