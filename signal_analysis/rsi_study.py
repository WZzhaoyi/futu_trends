"""
RSI Oversold/Overbought Edge Study
====================================
  - Forward returns by RSI quintile (RSI 2/3/4)
  - 200MA trend filter
  - Oversold density (triggers per rolling window)
  - Optional data source override and cache-only replay.

Usage:
  python rsi_study.py US.SPY
  python rsi_study.py US.QQQ --rsi 2 3 4
  python rsi_study.py US.SPY --density
  python rsi_study.py US.AAPL --start 2010-01-01
  python rsi_study.py US.SPY --refresh
  python rsi_study.py US.SPY --hold 2 3 5 10
  python rsi_study.py ^KS11 --start 2007-01-01 --density
  python rsi_study.py ^KS11 --proxy 127.0.0.1:10802

Latest cached summary:
  Command:
    python rsi_study.py <ticker> --cache-only --density --hold 1 2 3 5 10

  RSI(4) <= 20, all-trend average forward returns:
    US.SPY    1d +0.36% PF 1.77 | 5d +0.78% PF 1.90 | 10d +0.88% PF 1.64
    ^KS11     1d +0.01% PF 1.02 | 5d +0.41% PF 1.35 | 10d +0.56% PF 1.39
    HK.800000 1d +0.09% PF 1.14 | 5d +0.58% PF 1.50 | 10d +0.82% PF 1.52
    SH.000902 1d +0.04% PF 1.05 | 5d +0.47% PF 1.34 | 10d +0.42% PF 1.23

  Notable patterns:
    - US.SPY behaves like classic short-term mean reversion; 3-5 day holds and
      high oversold density are strongest.
    - ^KS11 and HK.800000 prefer trend context: oversold above 200MA works
      better than blind dip buying.
    - SH.000902 and ^KS11 show strong RSI > 80 momentum continuation,
      especially over 5-10 trading days.
    - HK.800000 below 200MA with high oversold density is weak; persistent
      selling is not automatically a bargain.
"""

import argparse, glob, os, sys, textwrap
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

import configparser
import pandas as pd


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


def _load_cached_kline_data(code: str, cache_dir: Path) -> pd.DataFrame | None:
    pattern = cache_dir / f'data_{code.replace(".", "_")}_K_DAY_*.csv'
    files = glob.glob(str(pattern))
    if not files:
        return None
    latest = max(files, key=lambda f: int(f.rsplit("_", 1)[1].split(".")[0]))
    return pd.read_csv(latest, index_col=0, parse_dates=True)


# ── RSI calculation ──
def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ── Core study ──
def run_study(df, ticker, rsi_periods, hold_periods, show_density):
    raw_close = df["Close"]
    raw_ma200 = raw_close.rolling(200).mean()

    for hold_days in hold_periods:
        forward_ret = raw_close.shift(-hold_days) / raw_close - 1

        # Trim to valid range
        valid = raw_ma200.notna() & forward_ret.notna()
        close = raw_close[valid]
        ma200 = raw_ma200[valid]
        forward_ret = forward_ret[valid]

        total_days = len(close)
        date_range = f"{close.index[0].strftime('%Y-%m-%d')} to {close.index[-1].strftime('%Y-%m-%d')}"
        return_label = "Next-Day Returns" if hold_days == 1 else f"{hold_days}-Day Forward Returns"

        for period in rsi_periods:
            r = rsi(df["Close"], period).reindex(close.index)

            print(f"\n{'='*78}")
            print(f"  {ticker}  RSI({period})  {return_label}    {date_range}   N={total_days}")
            print(f"{'='*78}")

            # ── RSI quintile breakdown ──
            bins = [
                (f"RSI <= 20",           r <= 20),
                (f"20 < RSI <= 40",      (r > 20) & (r <= 40)),
                (f"40 < RSI <= 60",      (r > 40) & (r <= 60)),
                (f"60 < RSI <= 80",      (r > 60) & (r <= 80)),
                (f"RSI > 80",            r > 80),
            ]

            trend_filters = [
                ("All",           pd.Series(True, index=close.index)),
                ("Above 200MA",   close > ma200),
                ("Below 200MA",   close <= ma200),
            ]

            header = f"  {'Trend':<14} {'RSI Range':<16} {'#Trades':>7} {'%Win':>6} {'Avg%':>7} {'PF':>6}"
            print(header)
            print(f"  {'─'*62}")

            for trend_label, trend_mask in trend_filters:
                for bin_label, bin_mask in bins:
                    mask = trend_mask & bin_mask
                    n = mask.sum()
                    if n < 5:
                        continue
                    rets = forward_ret[mask]
                    winners = (rets > 0).sum()
                    win_pct = winners / n * 100
                    avg_ret = rets.mean() * 100
                    gross_gain = rets[rets > 0].sum()
                    gross_loss = abs(rets[rets < 0].sum())
                    pf = gross_gain / gross_loss if gross_loss > 0 else float('inf')

                    if avg_ret > 0.1:
                        marker = "+"
                    elif avg_ret < -0.05:
                        marker = "-"
                    else:
                        marker = " "

                    print(f" {marker}{trend_label:<14} {bin_label:<16} {n:>7} {win_pct:>5.1f}% {avg_ret:>+6.2f}% {pf:>5.2f}")
                if trend_label != "Below 200MA":
                    print(f"  {'─'*62}")

        # ── Oversold density analysis ──
        if show_density:
            print(f"\n{'='*78}")
            print(f"  {ticker}  {return_label}  Oversold Density Analysis")
            print(f"{'='*78}")

            for period in rsi_periods:
                r = rsi(df["Close"], period).reindex(close.index)
                oversold = (r <= 20).astype(int)

                for window in [10, 20, 30]:
                    density = oversold.rolling(window).sum()
                    density = density.reindex(close.index)

                    print(f"\n  RSI({period}) <= 20 triggers in past {window} trading days:")
                    print(f"  {'Density':<12} {'#Days':>7} {'%Win':>6} {'Avg%':>7} {'PF':>6}  Interpretation")
                    print(f"  {'─'*72}")

                    density_bins = [
                        ("0 (none)",     density == 0,     "no recent oversold"),
                        ("1",            density == 1,     "isolated dip"),
                        ("2",            density == 2,     "moderate selling"),
                        ("3+",           density >= 3,     "persistent selling / correction"),
                    ]

                    for dlabel, dmask, interp in density_bins:
                        mask_today_os = (r <= 20) & dmask & forward_ret.notna()
                        n = mask_today_os.sum()
                        if n < 3:
                            print(f"  {dlabel:<12} {n:>7}   (insufficient data)")
                            continue
                        rets = forward_ret[mask_today_os]
                        winners = (rets > 0).sum()
                        win_pct = winners / n * 100
                        avg_ret = rets.mean() * 100
                        gross_gain = rets[rets > 0].sum()
                        gross_loss = abs(rets[rets < 0].sum())
                        pf = gross_gain / gross_loss if gross_loss > 0 else float('inf')

                        print(f"  {dlabel:<12} {n:>7} {win_pct:>5.1f}% {avg_ret:>+6.2f}% {pf:>5.2f}  {interp}")

                    high_density = (density >= 3) & (close <= ma200)
                    n_hd = high_density.sum()
                    if n_hd >= 5:
                        rets_hd = forward_ret[high_density]
                        avg_hd = rets_hd.mean() * 100
                        win_hd = (rets_hd > 0).mean() * 100
                        print(f"  >> density>=3 AND below 200MA: n={n_hd}, "
                              f"win={win_hd:.1f}%, avg={avg_hd:+.2f}%")

    print()


# ── Main ──
def main():
    p = argparse.ArgumentParser(
        description="RSI Oversold/Overbought Edge Study",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          python rsi_study.py US.SPY
          python rsi_study.py US.QQQ --rsi 2 3 4
          python rsi_study.py US.SPY --density
          python rsi_study.py US.SPY --start 2007-01-01 --density
          python rsi_study.py US.SPY --hold 2 3 5 10
          python rsi_study.py HK.800000 --source futu
          python rsi_study.py US.SPY --cache-only
          python rsi_study.py ^KS11 --start 2007-01-01 --density
          python rsi_study.py ^KS11 --proxy 127.0.0.1:10802
        """))
    p.add_argument("ticker", help="带市场前缀的代码，如 US.SPY / HK.00700 / SH.600000；或 Yahoo 原生代码，如 ^KS11")
    p.add_argument("--rsi", nargs="+", type=int, default=[4])
    p.add_argument("--hold", nargs="+", type=int, default=[1], help="持有交易日数，如 --hold 1 2 3 5 10")
    p.add_argument("--start", default="2007-01-01")
    p.add_argument("--density", action="store_true")
    p.add_argument("--refresh", action="store_true", help="清空缓存后重新拉取")
    p.add_argument("--cache-only", action="store_true", help="只读取本地缓存，不触发远程取数")
    p.add_argument("--config", default=None, help="config 路径，默认 config.ini 回退 config_template.ini")
    p.add_argument("--source", choices=["futu", "yfinance", "akshare", "longbridge", "ibkr"], default=None, help="临时覆盖数据源")
    p.add_argument("--proxy", default=None, help="yfinance HTTP 代理，如 127.0.0.1:10802")
    a = p.parse_args()
    if any(days <= 0 for days in a.hold):
        p.error("--hold 必须是正整数")

    code = a.ticker.upper()
    use_project_data = _is_project_ticker(code)

    config = _load_config(a.config)
    if a.proxy:
        config.set("CONFIG", "PROXY", a.proxy)
    if a.source:
        if use_project_data:
            market = code.split(".", 1)[0]
            config.set("CONFIG", f"DATA_SOURCE_{market}", a.source)
        else:
            config.set("CONFIG", "DATA_SOURCE", a.source)
    if not use_project_data and not a.source:
        config.set("CONFIG", "DATA_SOURCE", "yfinance")
    config.set("CONFIG", "FUTU_PUSH_TYPE", "K_DAY")  # 本研究固定日线

    cache_dir = Path("./data/rsi_study")
    cache_dir.mkdir(parents=True, exist_ok=True)

    warmup_start = pd.Timestamp(a.start) - pd.Timedelta(days=300)
    max_count = len(pd.bdate_range(warmup_start, pd.Timestamp.today()))

    source_hint = f" via {a.source}" if a.source else " via yfinance" if not use_project_data else ""
    cache_hint = " (cache-only)" if a.cache_only else ""
    print(f"  > {code}{source_hint}{' (refresh)' if a.refresh else ''}{cache_hint}")

    if a.cache_only:
        df = _load_cached_kline_data(code, cache_dir)
        if df is not None and not df.empty:
            df = df.tail(max_count).copy()
    else:
        import data as _data_module
        from data import get_kline_data

        if a.refresh:
            _data_module._kline_cache.pop((code, "K_DAY"), None)
            for f in glob.glob(str(cache_dir / f'data_{code.replace(".", "_")}_K_DAY_*.csv')):
                os.remove(f)

        df = get_kline_data(code, config, max_count=max_count, file_cache_dir=str(cache_dir))

    if df is not None and not df.empty:
        df = df[df.index >= warmup_start]
    if df is None or df.empty or len(df) < 250:
        print(f"Insufficient data for {code}.")
        sys.exit(1)

    df = df.rename(columns=str.capitalize).sort_index()
    run_study(df, code, a.rsi, a.hold, a.density)


if __name__ == "__main__":
    main()
