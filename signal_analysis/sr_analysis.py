"""
Support/Resistance Signal Study
===============================

Examples:
  python signal_analysis/sr_analysis.py SH.000001 --source futu
  python signal_analysis/sr_analysis.py SH.000001 --start 2021-07-01 --end 2026-06-30
  python signal_analysis/sr_analysis.py US.SPY --lookback 90 --pivot-window 3 --min-touches 2
  python signal_analysis/sr_analysis.py SH.000001 --params-json output/sr_timed_500_K_DAY/sr_SH_000001_evals500_K_DAY.json
  python signal_analysis/sr_analysis.py SH.000001 --bars 320 --max-levels 7
  python signal_analysis/sr_analysis.py US.QQQ US.GLD --detect-dir output/detect_20260703_K_DAY_USE_SR
"""

from __future__ import annotations

import argparse
import configparser
import glob
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from signal_analysis import SupportResistance, calculate_win_rate, default_stock_params


MARKETS = ("US", "HK", "SH", "SZ")


def _load_config(path: str | None = None):
    cfg = configparser.ConfigParser()
    candidates = [Path(path)] if path else [_ROOT / "config.ini", _ROOT / "config_template.ini"]
    for p in candidates:
        if p.exists():
            cfg.read(p, encoding="utf-8")
            return cfg
    raise FileNotFoundError(f"config not found: {candidates}")


def _is_project_ticker(code: str) -> bool:
    return "." in code and code.split(".", 1)[0] in MARKETS


def _load_cached_kline_data(code: str, ktype: str, cache_dir: Path) -> pd.DataFrame | None:
    pattern = cache_dir / f'data_{code.replace(".", "_")}_{ktype}_*.csv'
    files = glob.glob(str(pattern))
    if not files:
        return None
    latest = max(files, key=lambda f: int(f.rsplit("_", 1)[1].split(".")[0]))
    return pd.read_csv(latest, index_col=0, parse_dates=True)


def _load_params(args) -> dict[str, Any]:
    params = default_stock_params("SR")["best_params"]
    if args.params_json:
        with open(args.params_json) as f:
            data = json.load(f)
        params.update(data.get("best_params", data))

    overrides = {
        "lookback": args.lookback,
        "pivot_window": args.pivot_window,
        "min_touches": args.min_touches,
        "tolerance_atr": args.tolerance_atr,
        "breakout_buffer_atr": args.breakout_buffer_atr,
        "volume_ratio": args.volume_ratio,
    }
    params.update({k: v for k, v in overrides.items() if v is not None})
    return SupportResistance().get_params(params)


def _plot_candles(ax, df: pd.DataFrame):
    dates = mdates.date2num(df.index.to_pydatetime())
    width = 0.55
    for x, row in zip(dates, df.itertuples()):
        color = "#d62728" if row.close >= row.open else "#2ca02c"
        ax.vlines(x, row.low, row.high, color=color, linewidth=0.7, alpha=0.5)
        lower = min(row.open, row.close)
        height = abs(row.close - row.open)
        if height == 0:
            ax.hlines(row.close, x - width / 2, x + width / 2, color=color, linewidth=0.9)
        else:
            ax.add_patch(
                plt.Rectangle(
                    (x - width / 2, lower),
                    width,
                    height,
                    facecolor=color,
                    edgecolor=color,
                    linewidth=0.5,
                    alpha=0.38,
                )
            )


def _plot_level_segments(ax, df: pd.DataFrame, column: str, color: str, label: str):
    dates = mdates.date2num(df.index.to_pydatetime())
    levels = df[column].round(2)
    first_label = True
    start = None
    prev_level = None
    prev_i = None

    for i, level in enumerate(levels):
        if pd.isna(level):
            if prev_level is not None and start is not None:
                ax.hlines(prev_level, dates[start], dates[prev_i], color=color, linewidth=1.2, alpha=0.85, label=label if first_label else None)
                first_label = False
            start = None
            prev_level = None
            prev_i = None
            continue

        level = float(level)
        if prev_level is None:
            start = i
            prev_level = level
            prev_i = i
            continue

        if level != prev_level:
            ax.hlines(prev_level, dates[start], dates[prev_i], color=color, linewidth=1.2, alpha=0.85, label=label if first_label else None)
            first_label = False
            start = i
            prev_level = level
        prev_i = i

    if prev_level is not None and start is not None:
        ax.hlines(prev_level, dates[start], dates[prev_i], color=color, linewidth=1.2, alpha=0.85, label=label if first_label else None)


def _touch_count(df: pd.DataFrame, level: float, band: float) -> int:
    touched = (df["low"] <= level + band) & (df["high"] >= level - band)
    return int(touched.sum())


def _cluster_level_series(df: pd.DataFrame, series: pd.Series, tolerance: float, min_touches: int):
    points = [(idx, float(value)) for idx, value in series.dropna().items()]
    if not points:
        return []

    points = sorted(points, key=lambda item: item[1])
    clusters = []
    for idx, price in points:
        if not clusters or abs(price - clusters[-1]["level"]) > tolerance:
            clusters.append({"level": price, "points": [(idx, price)]})
            continue

        cluster = clusters[-1]
        cluster["points"].append((idx, price))
        cluster["level"] = float(np.mean([p for _, p in cluster["points"]]))

    result = []
    for cluster in clusters:
        if len(cluster["points"]) < min_touches:
            continue
        indexes = [idx for idx, _ in cluster["points"]]
        prices = [price for _, price in cluster["points"]]
        level = float(np.mean(prices))
        result.append(
            {
                "level": level,
                "presence": len(prices),
                "price_touches": _touch_count(df, level, tolerance),
                "first": min(indexes),
                "last": max(indexes),
                "price_std": float(np.std(prices)),
            }
        )
    return result


def _plot_human_levels(ax, df: pd.DataFrame, levels, color: str, label: str, band: float, max_levels: int):
    if not levels:
        return

    levels = sorted(levels, key=lambda item: (item["presence"], item["last"]), reverse=True)[:max_levels]
    x_end = mdates.date2num(df.index[-1].to_pydatetime())
    first_label = True

    for level in sorted(levels, key=lambda item: item["level"]):
        x_start = mdates.date2num(level["first"].to_pydatetime())
        y = level["level"]
        presence = level["presence"]
        strength = min(1.0, presence / max(1, levels[0]["presence"]))
        alpha = 0.35 + 0.35 * strength
        linewidth = 1.0 + 1.6 * strength

        ax.fill_between(
            [x_start, x_end],
            [y - band, y - band],
            [y + band, y + band],
            color=color,
            alpha=0.06 + 0.05 * strength,
            linewidth=0,
        )
        ax.hlines(y, x_start, x_end, color=color, linewidth=linewidth, alpha=alpha, label=label if first_label else None)
        first_label = False
        price_text = f"{y:.0f}" if y >= 100 else f"{y:.2f}"
        ax.text(
            df.index[-1],
            y,
            f" {price_text} ({presence}, T{level['price_touches']})",
            color=color,
            fontsize=8,
            va="center",
            alpha=0.85,
        )


def _plot_signals(ax, df: pd.DataFrame):
    styles = {
        "breakout resistance": ("^", "#d62728", "breakout resistance", "support_win"),
        "support hold": ("^", "#ff7f0e", "support hold", "support_win"),
        "breakdown support": ("v", "#2ca02c", "breakdown support", "resistance_win"),
        "resistance reject": ("v", "#1f77b4", "resistance reject", "resistance_win"),
    }
    first_win = True
    first_loss = True
    for signal, (marker, color, label, win_col) in styles.items():
        points = df[df["sr_signal"] == signal]
        if points.empty:
            continue
        wins = points[points[win_col] == 1]
        losses = points[points[win_col] != 1]
        if not wins.empty:
            y_win = wins["low"] * 0.995 if marker == "^" else wins["high"] * 1.005
            ax.scatter(wins.index, y_win, marker=marker, s=58, color=color, edgecolors="#111111", linewidths=0.85, label=label, zorder=6)
            ax.scatter([], [], marker="o", s=38, facecolors="none", edgecolors="#111111", linewidths=0.85, label="win" if first_win else None)
            first_win = False
        if not losses.empty:
            y_loss = losses["low"] * 0.995 if marker == "^" else losses["high"] * 1.005
            ax.scatter(losses.index, y_loss, marker=marker, s=44, color=color, alpha=0.42, edgecolors="#d9d9d9", linewidths=0.65, label=label if wins.empty else None, zorder=5)
            ax.scatter([], [], marker="o", s=38, facecolors="none", edgecolors="#d9d9d9", linewidths=0.65, label="loss" if first_loss else None)
            first_loss = False


def _finish_plot(fig, ax, title: str):
    ax.set_title(title)
    ax.set_ylabel("Price")
    ax.grid(True, linewidth=0.4, alpha=0.25)
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    ax.legend(loc="best", fontsize=8, ncols=2)
    fig.tight_layout()


def plot_algorithm_levels(df: pd.DataFrame, output: Path, title: str):
    fig, ax = plt.subplots(figsize=(18, 9), dpi=150)
    _plot_candles(ax, df)
    ax.plot(df.index, df["close"], color="#111111", linewidth=1.0, alpha=0.72, label="close")
    _plot_level_segments(ax, df, "support_level", "#2ca02c", "support level")
    _plot_level_segments(ax, df, "resistance_level", "#d62728", "resistance level")
    _plot_signals(ax, df)
    _finish_plot(fig, ax, title)
    fig.savefig(output)
    plt.close(fig)


def plot_human_levels(df: pd.DataFrame, params: dict[str, Any], output: Path, title: str, max_levels: int):
    median_atr = float(df["atr"].dropna().median())
    tolerance = median_atr * float(params.get("tolerance_atr", 0.35))
    band = tolerance * 0.35
    min_touches = max(2, int(params.get("min_touches", 2)))
    support_levels = _cluster_level_series(df, df["support_level"], tolerance, min_touches)
    resistance_levels = _cluster_level_series(df, df["resistance_level"], tolerance, min_touches)

    fig, ax = plt.subplots(figsize=(18, 9), dpi=150)
    _plot_candles(ax, df)
    ax.plot(df.index, df["close"], color="#111111", linewidth=1.0, alpha=0.72, label="close")
    _plot_human_levels(ax, df, support_levels, "#2ca02c", "support zone", band, max_levels)
    _plot_human_levels(ax, df, resistance_levels, "#d62728", "resistance zone", band, max_levels)
    _plot_signals(ax, df)
    _finish_plot(fig, ax, title)
    fig.savefig(output)
    plt.close(fig)


def generate_from_detect(detect_dir: Path, output_dir: Path, codes: list[str], bars: int, max_levels: int, ktype: str = "K_DAY"):
    """从 detect.py 的输出目录批量生成典型分析图，复用已训练的信号 CSV 与最优参数，不重新取数训练。"""
    summary_files = sorted(detect_dir.glob(f"analysis_params_*_{ktype}.json"))
    if not summary_files:
        raise FileNotFoundError(f"analysis params json not found in {detect_dir}")
    with open(summary_files[-1]) as f:
        summary = json.load(f)

    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for code in codes:
        safe = code.replace(".", "_")
        matches = sorted(detect_dir.glob(f"signals_{safe}_*_{ktype}.csv"))
        if not matches:
            raise FileNotFoundError(f"signals csv not found for {code} in {detect_dir}")
        csv_path = matches[-1]

        df = pd.read_csv(csv_path)
        date_col = "Date" if "Date" in df.columns else "time_key" if "time_key" in df.columns else df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col).sort_index()
        df_chart = df.tail(bars).copy()
        params = summary[code]["best_params"]
        perf = summary[code]["performance"]

        code_dir = output_dir / safe
        code_dir.mkdir(parents=True, exist_ok=True)
        prefix = code_dir / f"sr_{safe}_{ktype}"
        levels_png = Path(f"{prefix}_levels.png")
        human_png = Path(f"{prefix}_human.png")
        plot_algorithm_levels(df_chart, levels_png, f"{code} SR Algorithm Levels - {detect_dir.name}")
        plot_human_levels(df_chart, params, human_png, f"{code} SR Human-Style Zones - {detect_dir.name}", max_levels)

        def _side_summary(result: dict[str, Any]) -> dict[str, Any]:
            return {
                "support_win_rate": result.get("support_win_rate", 0),
                "support_signals_count": result.get("support_signals_count", 0),
                "resistance_win_rate": result.get("resistance_win_rate", 0),
                "resistance_signals_count": result.get("resistance_signals_count", 0),
                "signal_breakdown": result.get("signal_breakdown", {}),
            }

        item = {
            "code": code,
            "detect_dir": str(detect_dir),
            "source_csv": str(csv_path),
            "params": params,
            "raw": _side_summary(perf),
            "checked": _side_summary(perf.get("checked", {})),
            "chart_rows": len(df_chart),
            "chart_start": str(df_chart.index.min().date()),
            "chart_end": str(df_chart.index.max().date()),
            "files": {
                "algorithm_plot": str(levels_png),
                "human_plot": str(human_png),
                "summary": f"{prefix}.json",
            },
        }
        with open(f"{prefix}.json", "w") as f:
            json.dump(item, f, ensure_ascii=False, indent=2)
        rows.append(item)

    with open(output_dir / "typical_summary.json", "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return rows


def _print_summary(name: str, result: dict[str, Any]):
    print(f"\n{name}")
    print("side        win_rate  signals  z_score")
    print(f"support     {result['support_win_rate']:.1%}  {result['support_signals_count']:>7}  {result['support_z_score']:.2f}")
    print(f"resistance  {result['resistance_win_rate']:.1%}  {result['resistance_signals_count']:>7}  {result['resistance_z_score']:.2f}")
    breakdown = result.get("signal_breakdown")
    if breakdown:
        print("signal                 win_rate  signals  z_score")
        for signal in ["breakout resistance", "support hold", "breakdown support", "resistance reject"]:
            item = breakdown.get(signal, {})
            print(f"{signal:<22} {item.get('win_rate', 0):>7.1%}  {item.get('signals_count', 0):>7}  {item.get('z_score', 0):.2f}")


def _print_signal_breakdown_comparison(raw: dict[str, Any], checked: dict[str, Any]):
    raw_breakdown = raw.get("signal_breakdown", {})
    checked_breakdown = checked.get("signal_breakdown", {})
    if not raw_breakdown and not checked_breakdown:
        return

    print("\nsignal breakdown: raw vs checked")
    print("signal                    raw win/count    checked win/count")
    for signal in ["breakout resistance", "support hold", "breakdown support", "resistance reject"]:
        raw_item = raw_breakdown.get(signal, {})
        checked_item = checked_breakdown.get(signal, {})
        raw_text = f"{raw_item.get('win_rate', 0):.1%}/{raw_item.get('signals_count', 0)}"
        checked_text = f"{checked_item.get('win_rate', 0):.1%}/{checked_item.get('signals_count', 0)}"
        print(f"{signal:<22} {raw_text:>14}    {checked_text:>17}")


def main():
    p = argparse.ArgumentParser(
        description="Support/Resistance signal study and charting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          python signal_analysis/sr_analysis.py SH.000001 --source futu
          python signal_analysis/sr_analysis.py SH.000001 --params-json output/sr_timed_500_K_DAY/sr_SH_000001_evals500_K_DAY.json
          python signal_analysis/sr_analysis.py US.SPY --lookback 90 --pivot-window 3 --bars 260
          python signal_analysis/sr_analysis.py US.QQQ US.GLD --detect-dir output/detect_20260703_K_DAY_USE_SR
        """),
    )
    p.add_argument("ticker", nargs="+", help="带市场前缀的代码，如 SH.000001 / SZ.399006 / US.SPY；--detect-dir 模式下可传多个")
    p.add_argument("--detect-dir", default=None, help="detect.py 输出目录；给定时直接用其中的信号 CSV 与最优参数批量出图，不取数训练")
    p.add_argument("--start", default="2021-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--ktype", default="K_DAY")
    p.add_argument("--source", choices=["futu", "yfinance", "akshare", "longbridge", "ibkr"], default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--cache-only", action="store_true")
    p.add_argument("--params-json", default=None)
    p.add_argument("--lookback", type=int, default=None)
    p.add_argument("--pivot-window", type=int, default=None)
    p.add_argument("--min-touches", type=int, default=None)
    p.add_argument("--tolerance-atr", type=float, default=None)
    p.add_argument("--breakout-buffer-atr", type=float, default=None)
    p.add_argument("--volume-ratio", type=float, default=None)
    p.add_argument("--look-ahead", type=int, default=10)
    p.add_argument("--target-multiplier", type=float, default=1.85)
    p.add_argument("--atr-period", type=int, default=57)
    p.add_argument("--bars", type=int, default=320, help="图表展示最近 N 根 K 线")
    p.add_argument("--max-levels", type=int, default=7)
    p.add_argument("--output-dir", default=None)
    args = p.parse_args()

    if args.detect_dir:
        detect_dir = Path(args.detect_dir)
        codes = [t.upper() for t in args.ticker]
        out_dir = Path(args.output_dir) if args.output_dir else Path("output") / f"sr_typical_{detect_dir.name}"
        rows = generate_from_detect(detect_dir, out_dir, codes, args.bars, args.max_levels, args.ktype)
        for item in rows:
            checked = item["checked"]
            print(
                f"{item['code']} "
                f"support={checked['support_win_rate']:.3f}/{checked['support_signals_count']} "
                f"resistance={checked['resistance_win_rate']:.3f}/{checked['resistance_signals_count']} "
                f"human={item['files']['human_plot']}"
            )
        return

    if len(args.ticker) != 1:
        p.error("单标的研究模式只接受一个 ticker；批量出图请配合 --detect-dir")
    code = args.ticker[0].upper()
    use_project_data = _is_project_ticker(code)
    config = _load_config(args.config)
    if args.source:
        if use_project_data:
            config.set("CONFIG", f"DATA_SOURCE_{code.split('.', 1)[0]}", args.source)
        else:
            config.set("CONFIG", "DATA_SOURCE", args.source)
    if not use_project_data and not args.source:
        config.set("CONFIG", "DATA_SOURCE", "yfinance")
    config.set("CONFIG", "FUTU_PUSH_TYPE", args.ktype)

    cache_dir = Path("./data/sr_analysis")
    cache_dir.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(args.start)
    warmup_start = start - pd.Timedelta(days=360)
    end = pd.Timestamp(args.end) if args.end else pd.Timestamp.today()
    max_count = len(pd.bdate_range(warmup_start, end)) + 30

    print(f"  > {code} {args.ktype} start={args.start} end={args.end or 'latest'}")
    df = None
    if args.cache_only:
        df = _load_cached_kline_data(code, args.ktype, cache_dir)
        if df is not None:
            df = df.tail(max_count).copy()
    else:
        import data as _data_module
        from data import get_kline_data

        if args.refresh:
            _data_module._kline_cache.pop((code, args.ktype), None)
            for f in glob.glob(str(cache_dir / f'data_{code.replace(".", "_")}_{args.ktype}_*.csv')):
                os.remove(f)
        df = get_kline_data(code, config, max_count=max_count, file_cache_dir=str(cache_dir))

    if df is None or df.empty:
        raise SystemExit(f"Insufficient data for {code}")
    df = df.sort_index()
    df = df[(df.index >= warmup_start) & (df.index <= end)]
    if len(df) < 250:
        raise SystemExit(f"Insufficient data for {code}: {len(df)} rows")

    params = _load_params(args)
    indicator = SupportResistance()
    raw = indicator.calculate(df, params, mode="train")
    checked = indicator.calculate(df, params, mode="check", atr_period=args.atr_period, target_multiplier=args.target_multiplier)

    raw_eval = raw[raw.index >= start].copy()
    checked_eval = checked[checked.index >= start].copy()

    raw_perf = calculate_win_rate(raw_eval, look_ahead=args.look_ahead, target_multiplier=args.target_multiplier, atr_period=args.atr_period)
    checked_perf = calculate_win_rate(checked_eval, look_ahead=args.look_ahead, target_multiplier=args.target_multiplier, atr_period=args.atr_period)
    consume_results = {}
    for ratio in [0.3, 0.5, 0.7]:
        filtered = indicator.calculate(df, params, mode="check", atr_period=args.atr_period, target_multiplier=args.target_multiplier, consume_ratio=ratio)
        filtered_eval = filtered[filtered.index >= start].copy()
        consume_results[f"checked_{ratio}"] = calculate_win_rate(filtered_eval, look_ahead=args.look_ahead, target_multiplier=args.target_multiplier, atr_period=args.atr_period)

    raw_detail = raw_perf.pop("detailed_df")
    checked_perf.pop("detailed_df")
    for result in consume_results.values():
        result.pop("detailed_df")
    df_chart = raw_detail[raw_detail.index >= start].tail(args.bars).copy()

    out_dir = Path(args.output_dir) if args.output_dir else Path("output") / f"sr_analysis_{code.replace('.', '_')}_{args.ktype}"
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / f"sr_{code.replace('.', '_')}_{args.ktype}"
    raw_detail.to_csv(f"{prefix}.csv")
    plot_algorithm_levels(df_chart, Path(f"{prefix}_levels.png"), f"{code} SR Algorithm Levels")
    plot_human_levels(df_chart, params, Path(f"{prefix}_human.png"), f"{code} SR Human-Style Zones", args.max_levels)

    summary = {
        "code": code,
        "params": params,
        "data_info": {
            "rows": len(df),
            "start": str(df.index.min()),
            "end": str(df.index.max()),
            "study_start": str(start),
            "last_close": float(df["close"].iloc[-1]),
        },
        "eval": {
            "look_ahead": args.look_ahead,
            "target_multiplier": args.target_multiplier,
            "atr_period": args.atr_period,
        },
        "performance": {
            "raw": raw_perf,
            "checked": checked_perf,
            **consume_results,
        },
        "files": {
            "csv": f"{prefix}.csv",
            "algorithm_plot": f"{prefix}_levels.png",
            "human_plot": f"{prefix}_human.png",
            "summary": f"{prefix}.json",
        },
    }
    with open(f"{prefix}.json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    _print_summary("raw", raw_perf)
    _print_summary("checked", checked_perf)
    _print_signal_breakdown_comparison(raw_perf, checked_perf)
    for name, result in consume_results.items():
        _print_summary(name, result)
    print("\nFiles:")
    for path in summary["files"].values():
        print(f"  {path}")


if __name__ == "__main__":
    main()
