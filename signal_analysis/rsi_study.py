"""
RSI Oversold/Overbought Edge Study
====================================
  - Next-day returns by RSI quintile (RSI 2/3/4)
  - 200MA trend filter
  - Oversold density (triggers per rolling window)
  - US.SPY / HK.00700 / SH.600000 / SZ.000001。

Usage:
  python rsi_study.py US.SPY
  python rsi_study.py US.QQQ --rsi 2 3 4
  python rsi_study.py US.SPY --density
  python rsi_study.py US.AAPL --start 2010-01-01
  python rsi_study.py US.SPY --refresh

Example:
==============================================================================
  US.SPY  RSI(4)  Next-Day Returns    2022-01-31 to 2026-04-16   N=1056
==============================================================================
  Trend          RSI Range        #Trades   %Win    Avg%     PF
  ──────────────────────────────────────────────────────────────
 +All            RSI <= 20             80  62.5%  +0.47%  2.42
  All            20 < RSI <= 40       196  49.5%  +0.03%  1.07
  All            40 < RSI <= 60       272  56.6%  +0.02%  1.04
  All            60 < RSI <= 80       354  51.4%  -0.01%  0.98
  All            RSI > 80             154  57.8%  +0.08%  1.36
  ──────────────────────────────────────────────────────────────
 +Above 200MA    RSI <= 20             41  68.3%  +0.44%  3.89
  Above 200MA    20 < RSI <= 40       122  50.8%  -0.02%  0.95
  Above 200MA    40 < RSI <= 60       193  60.6%  +0.08%  1.26
  Above 200MA    60 < RSI <= 80       286  52.1%  +0.00%  1.00
  Above 200MA    RSI > 80             144  59.0%  +0.08%  1.38
  ──────────────────────────────────────────────────────────────
 +Below 200MA    RSI <= 20             39  56.4%  +0.50%  1.96
 +Below 200MA    20 < RSI <= 40        74  47.3%  +0.12%  1.20
 -Below 200MA    40 < RSI <= 60        79  46.8%  -0.14%  0.81
  Below 200MA    60 < RSI <= 80        68  48.5%  -0.03%  0.94
  Below 200MA    RSI > 80              10  40.0%  +0.07%  1.22

==============================================================================
  SH.000902  RSI(4)  Next-Day Returns    2019-12-26 to 2026-04-17   N=1527
==============================================================================
  Trend          RSI Range        #Trades   %Win    Avg%     PF
  ──────────────────────────────────────────────────────────────
 +All            RSI <= 20            136  62.5%  +0.21%  1.38
 -All            20 < RSI <= 40       336  49.7%  -0.09%  0.83
  All            40 < RSI <= 60       456  52.9%  +0.01%  1.03
  All            60 < RSI <= 80       437  48.5%  +0.03%  1.08
 +All            RSI > 80             162  54.3%  +0.19%  1.51
  ──────────────────────────────────────────────────────────────
 -Above 200MA    RSI <= 20             35  65.7%  -0.21%  0.79
  Above 200MA    20 < RSI <= 40       152  55.3%  +0.05%  1.10
  Above 200MA    40 < RSI <= 60       292  56.5%  +0.03%  1.06
  Above 200MA    60 < RSI <= 80       305  48.5%  +0.02%  1.04
 +Above 200MA    RSI > 80             129  55.0%  +0.20%  1.57
  ──────────────────────────────────────────────────────────────
 +Below 200MA    RSI <= 20            101  61.4%  +0.35%  1.90
 -Below 200MA    20 < RSI <= 40       184  45.1%  -0.20%  0.65
  Below 200MA    40 < RSI <= 60       164  46.3%  -0.01%  0.98
  Below 200MA    60 < RSI <= 80       132  48.5%  +0.05%  1.16
 +Below 200MA    RSI > 80              33  51.5%  +0.15%  1.33

==============================================================================
  HK.800000  RSI(4)  Next-Day Returns    2019-12-24 to 2026-04-17   N=1550
==============================================================================
  Trend          RSI Range        #Trades   %Win    Avg%     PF
  ──────────────────────────────────────────────────────────────
  All            RSI <= 20            161  53.4%  +0.04%  1.05
  All            20 < RSI <= 40       378  51.6%  +0.01%  1.02
  All            40 < RSI <= 60       474  48.9%  -0.00%  0.99
  All            60 < RSI <= 80       381  48.8%  -0.02%  0.96
  All            RSI > 80             156  49.4%  +0.07%  1.15
  ──────────────────────────────────────────────────────────────
 +Above 200MA    RSI <= 20             34  58.8%  +0.12%  1.18
  Above 200MA    20 < RSI <= 40       161  51.6%  +0.03%  1.06
 -Above 200MA    40 < RSI <= 60       240  47.9%  -0.12%  0.76
  Above 200MA    60 < RSI <= 80       213  51.2%  +0.03%  1.07
 +Above 200MA    RSI > 80             125  54.4%  +0.19%  1.43
  ──────────────────────────────────────────────────────────────
  Below 200MA    RSI <= 20            127  52.0%  +0.01%  1.02
  Below 200MA    20 < RSI <= 40       217  51.6%  +0.00%  1.00
 +Below 200MA    40 < RSI <= 60       234  50.0%  +0.12%  1.20
 -Below 200MA    60 < RSI <= 80       168  45.8%  -0.09%  0.86
 -Below 200MA    RSI > 80              31  29.0%  -0.41%  0.43  
"""

import argparse, glob, os, sys, textwrap
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

import configparser
import pandas as pd
import data as _data_module
from data import get_kline_data


MARKETS = ("US", "HK", "SH", "SZ")


def _load_config(path: str | None = None):
    cfg = configparser.ConfigParser()
    candidates = [Path(path)] if path else [_ROOT / "config.ini", _ROOT / "config_template.ini"]
    for p in candidates:
        if p.exists():
            cfg.read(p, encoding="utf-8")
            return cfg
    raise FileNotFoundError(f"config not found: {candidates}")


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
def run_study(df, ticker, rsi_periods, show_density):
    close = df["Close"]
    ma200 = close.rolling(200).mean()
    next_ret = close.shift(-1) / close - 1  # next-day return

    # Trim to valid range
    valid = ma200.notna() & next_ret.notna()
    close = close[valid]
    ma200 = ma200[valid]
    next_ret = next_ret[valid]

    total_days = len(close)
    date_range = f"{close.index[0].strftime('%Y-%m-%d')} to {close.index[-1].strftime('%Y-%m-%d')}"

    for period in rsi_periods:
        r = rsi(df["Close"], period).reindex(close.index)

        print(f"\n{'='*78}")
        print(f"  {ticker}  RSI({period})  Next-Day Returns    {date_range}   N={total_days}")
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
                rets = next_ret[mask]
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
        print(f"  {ticker}  Oversold Density Analysis")
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
                    mask_today_os = (r <= 20) & dmask & next_ret.notna()
                    n = mask_today_os.sum()
                    if n < 3:
                        print(f"  {dlabel:<12} {n:>7}   (insufficient data)")
                        continue
                    rets = next_ret[mask_today_os]
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
                    rets_hd = next_ret[high_density]
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
        """))
    p.add_argument("ticker", help="带市场前缀的代码，如 US.SPY / HK.00700 / SH.600000")
    p.add_argument("--rsi", nargs="+", type=int, default=[4])
    p.add_argument("--start", default="2007-01-01")
    p.add_argument("--density", action="store_true")
    p.add_argument("--refresh", action="store_true", help="清空缓存后重新拉取")
    p.add_argument("--config", default=None, help="config 路径，默认 config.ini 回退 config_template.ini")
    a = p.parse_args()

    code = a.ticker.upper()
    if "." not in code or code.split(".", 1)[0] not in MARKETS:
        p.error(f"ticker 必须带市场前缀 ({'/'.join(MARKETS)}.XXX)")

    config = _load_config(a.config)
    config.set("CONFIG", "FUTU_PUSH_TYPE", "K_DAY")  # 本研究固定日线

    cache_dir = Path("./data/rsi_study")
    cache_dir.mkdir(parents=True, exist_ok=True)

    if a.refresh:
        _data_module._kline_cache.pop((code, "K_DAY"), None)
        for f in glob.glob(str(cache_dir / f'data_{code.replace(".", "_")}_K_DAY_*.csv')):
            os.remove(f)

    warmup_start = pd.Timestamp(a.start) - pd.Timedelta(days=300)
    max_count = len(pd.bdate_range(warmup_start, pd.Timestamp.today()))

    print(f"  > {code}{' (refresh)' if a.refresh else ''}")
    df = get_kline_data(code, config, max_count=max_count, file_cache_dir=str(cache_dir))
    if df is None or df.empty or len(df) < 250:
        print(f"Insufficient data for {code}.")
        sys.exit(1)

    df = df.rename(columns=str.capitalize).sort_index()
    run_study(df, code, a.rsi, a.density)


if __name__ == "__main__":
    main()
