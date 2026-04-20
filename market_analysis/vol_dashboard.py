"""
Volatility Dashboard  v5
==========================
Three panels, two indicators, zero noise.

  Top:     SPY + VIX (context)
  Middle:  Quadrant map (Term Structure x SKEW, 20d trail)
  Bottom:  Term Structure | SKEW  (time series)
  Data Source: CBOE 指数 ^VIX/^VIX3M/^SKEW 仅 yfinance 可取

Usage:
  python vol_dashboard.py
  python vol_dashboard.py --months 12
  python vol_dashboard.py --refresh
"""

import argparse, datetime as dt, glob, os, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

import configparser
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.gridspec import GridSpec

import data as _data_module
from data import get_kline_data

TICKERS = ["US.^VIX", "US.^VIX3M", "US.^SKEW", "US.SPY"]
CACHE_DIR = Path("./data/vol_dashboard")


def _load_config(path: str | None = None):
    cfg = configparser.ConfigParser()
    candidates = [Path(path)] if path else [_ROOT / "config.ini", _ROOT / "config_template.ini"]
    for p in candidates:
        if p.exists():
            cfg.read(p, encoding="utf-8")
            return cfg
    raise FileNotFoundError(f"config not found: {candidates}")


def _clear_cache(code: str, ktype: str = "K_DAY"):
    _data_module._kline_cache.pop((code, ktype), None)
    for f in glob.glob(str(CACHE_DIR / f'data_{code.replace(".", "_")}_{ktype}_*.csv')):
        os.remove(f)


def _fetch(code: str, max_count: int, config: configparser.ConfigParser) -> pd.DataFrame:
    df = get_kline_data(code, config, max_count=max_count, file_cache_dir=str(CACHE_DIR))
    if df is None or df.empty:
        print(f"  ! {code}: no data")
        return pd.DataFrame()
    print(f"  > {code}")
    return df.rename(columns=str.capitalize).sort_index()


# ── Data ──
def build(config, start, force):
    early = (pd.Timestamp(start) - pd.Timedelta(days=60))
    max_count = len(pd.bdate_range(early, pd.Timestamp.today()))

    if force:
        for tk in TICKERS:
            _clear_cache(tk)

    data = {tk: _fetch(tk, max_count, config) for tk in TICKERS}
    vix, v3m, skw, spy = (data[tk] for tk in TICKERS)

    idx = vix.index
    df = pd.DataFrame(index=idx)
    df["VIX"]  = vix["Close"]
    df["SPY"]  = spy["Close"].reindex(idx, method="ffill")
    df["VIX3M"]= v3m["Close"].reindex(idx, method="ffill")
    df["SKEW"] = skw["Close"].reindex(idx, method="ffill")
    df["TERM"] = df["VIX"] / df["VIX3M"]
    return df.loc[start:].dropna(subset=["VIX","SPY","TERM","SKEW"])

# ── Plot ──
def plot(df, output="vol_dashboard.png"):
    fig = plt.figure(figsize=(14, 13), facecolor="#0e1117")
    gs = GridSpec(2, 2, height_ratios=[1.8, 2.2], width_ratios=[1.1, 1],
                  hspace=0.26, wspace=0.22,
                  left=0.07, right=0.96, top=0.93, bottom=0.05)

    C = {"panel":"#161b22","text":"#c9d1d9","grid":"#21262d",
         "spy":"#58a6ff","vix":"#f0883e","term":"#d2a8ff","skew":"#79c0ff",
         "danger":"#f85149","warn":"#d29922","calm":"#3fb950","trail":"#ffa657"}

    def style(ax, title=""):
        ax.set_facecolor(C["panel"])
        ax.tick_params(colors=C["text"], labelsize=9)
        ax.grid(True, color=C["grid"], linewidth=0.5, alpha=0.5)
        for s in ax.spines.values(): s.set_color(C["grid"])
        if title:
            ax.set_title(title, color=C["text"], fontsize=11,
                         fontweight="bold", loc="left", pad=8)

    dates = df.index
    term = df["TERM"].values
    skew = df["SKEW"].values
    n = len(term)

    # ── Top (full width): SPY + VIX ──
    ax0 = fig.add_subplot(gs[0, :])
    style(ax0, "SPY  &  VIX")
    ax0.xaxis.set_major_formatter(mdates.DateFormatter("%y-%m"))
    ax0.xaxis.set_major_locator(mdates.MonthLocator())
    ax0.plot(dates, df["SPY"], color=C["spy"], linewidth=1.4)
    ax0.set_ylabel("SPY", color=C["spy"], fontsize=9)
    ax0b = ax0.twinx()
    ax0b.plot(dates, df["VIX"], color=C["vix"], linewidth=1, alpha=0.7)
    ax0b.set_ylabel("VIX", color=C["vix"], fontsize=9)
    ax0b.tick_params(colors=C["text"], labelsize=9)
    ax0b.spines["right"].set_color(C["grid"])
    m30 = df["VIX"] >= 30
    if m30.any():
        yl, yh = ax0.get_ylim()
        ax0.fill_between(dates, yl, yh, where=m30, color=C["danger"], alpha=0.08)
        ax0.set_ylim(yl, yh)

    # ── Bottom-left: Quadrant scatter ──
    ax1 = fig.add_subplot(gs[1, 0])
    style(ax1, "Quadrant Map  (recent 20 days highlighted)")

    skew_med = df["SKEW"].median()
    trail = min(20, n)

    # Quadrant shading
    t_lo, t_hi = min(term.min(), 0.7), max(term.max(), 1.15)
    s_lo, s_hi = min(skew.min(), 120), max(skew.max(), 165)
    pad_t = (t_hi - t_lo) * 0.08
    pad_s = (s_hi - s_lo) * 0.08
    t_lo -= pad_t; t_hi += pad_t; s_lo -= pad_s; s_hi += pad_s

    # Tinted quadrant backgrounds
    ax1.axhspan(skew_med, s_hi, xmin=0, xmax=0.5,
                color=C["warn"], alpha=0.04)     # top-left: hidden tail
    ax1.axhspan(skew_med, s_hi, xmin=0.5, xmax=1,
                color=C["danger"], alpha=0.04)    # top-right: full panic
    ax1.axhspan(s_lo, skew_med, xmin=0, xmax=0.5,
                color=C["calm"], alpha=0.04)      # bottom-left: true calm
    ax1.axhspan(s_lo, skew_med, xmin=0.5, xmax=1,
                color=C["calm"], alpha=0.06)      # bottom-right: fear peaking

    # Old dots
    ax1.scatter(term[:-trail], skew[:-trail], c=C["grid"], s=10, alpha=0.25, zorder=1)

    # Trail
    trail_t = term[-trail:]
    trail_s = skew[-trail:]
    alphas = np.linspace(0.15, 1.0, trail)
    sizes = np.linspace(20, 90, trail)
    for i in range(trail):
        ax1.scatter(trail_t[i], trail_s[i], c=C["trail"],
                    s=sizes[i], alpha=alphas[i], zorder=2, edgecolors="none")
    ax1.plot(trail_t, trail_s, color=C["trail"], linewidth=1, alpha=0.35, zorder=1)

    # Today
    ax1.scatter(term[-1], skew[-1], c="white", s=140, zorder=3,
                edgecolors=C["trail"], linewidth=2.5)

    # Crosshairs
    ax1.axvline(1.0, color=C["text"], linewidth=0.8, alpha=0.25, linestyle="--")
    ax1.axhline(skew_med, color=C["text"], linewidth=0.8, alpha=0.25, linestyle="--")

    # Labels
    lp = 0.03  # label padding fraction
    ax1.text(t_lo + (t_hi-t_lo)*lp, s_hi - (s_hi-s_lo)*lp,
             "QUIET SURFACE\nHIDDEN TAIL BID", color=C["warn"],
             fontsize=9, fontweight="bold", alpha=0.5, va="top")
    ax1.text(t_hi - (t_hi-t_lo)*lp, s_hi - (s_hi-s_lo)*lp,
             "FULL PANIC\nNOT BOTTOMED", color=C["danger"],
             fontsize=9, fontweight="bold", alpha=0.5, ha="right", va="top")
    ax1.text(t_lo + (t_hi-t_lo)*lp, s_lo + (s_hi-s_lo)*lp,
             "TRUE CALM", color=C["calm"],
             fontsize=9, fontweight="bold", alpha=0.5, va="bottom")
    ax1.text(t_hi - (t_hi-t_lo)*lp, s_lo + (s_hi-s_lo)*lp,
             "FEAR PEAKING\nTAIL BID FADING", color=C["calm"],
             fontsize=9, fontweight="bold", alpha=0.6, ha="right", va="bottom")

    ax1.set_xlim(t_lo, t_hi)
    ax1.set_ylim(s_lo, s_hi)
    ax1.set_xlabel("Term Structure  (VIX / VIX3M)", color=C["term"], fontsize=10)
    ax1.set_ylabel("SKEW", color=C["skew"], fontsize=10)

    # ── Bottom-right: stacked time series ──
    gs_right = gs[1, 1].subgridspec(2, 1, hspace=0.35)

    ax2 = fig.add_subplot(gs_right[0])
    style(ax2, "VIX / VIX3M")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%y-%m"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator())
    ax2.plot(dates, term, color=C["term"], linewidth=1.2)
    ax2.axhline(1.0, color=C["danger"], linewidth=0.8, linestyle="--", alpha=0.5)
    ax2.fill_between(dates, 1.0, term, where=term>1, color=C["danger"], alpha=0.10)
    ax2.fill_between(dates, 1.0, term, where=term<=1, color=C["calm"], alpha=0.06)

    ax3 = fig.add_subplot(gs_right[1])
    style(ax3, "CBOE SKEW")
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%y-%m"))
    ax3.xaxis.set_major_locator(mdates.MonthLocator())
    ax3.plot(dates, skew, color=C["skew"], linewidth=1.2)
    ax3.axhline(skew_med, color=C["text"], linewidth=0.6, linestyle="--", alpha=0.3)
    ax3.axhline(150, color=C["warn"], linewidth=0.8, linestyle="--", alpha=0.4)

    fig.suptitle("Volatility Dashboard",
                 color=C["text"], fontsize=15, fontweight="bold", y=0.97)
    plt.savefig(output, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"\n> saved: {output}")

# ── Terminal ──
def summary(df):
    L = df.iloc[-1]; d = df.index[-1].strftime("%Y-%m-%d")
    t, s, s_med = L["TERM"], L["SKEW"], df["SKEW"].median()
    hi_t, hi_s = t >= 1.0, s >= s_med
    if   hi_t and hi_s:     q = "FULL PANIC — not bottomed"
    elif hi_t and not hi_s: q = "FEAR PEAKING — tail bid fading"
    elif not hi_t and hi_s: q = "QUIET SURFACE — hidden tail bid"
    else:                   q = "TRUE CALM"
    t_dir = s_dir = "?"
    if len(df) > 5:
        t_dir = "^" if t > df["TERM"].iloc[-6]+.02 else ("v" if t < df["TERM"].iloc[-6]-.02 else "=")
        s_dir = "^" if s > df["SKEW"].iloc[-6]+2 else ("v" if s < df["SKEW"].iloc[-6]-2 else "=")
    print(f"\n{'='*52}")
    print(f"  {d}   SPY {L['SPY']:.2f}   VIX {L['VIX']:.2f}")
    print(f"  Term  {t:.3f} {t_dir}  {'BACKWARDATION' if hi_t else 'contango'}")
    print(f"  SKEW  {s:.0f}  {s_dir}  {'above' if hi_s else 'below'} median ({s_med:.0f})")
    print(f"  >> {q}")
    print(f"{'='*52}\n")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--months",type=int,default=6,
                   help="lookback months (ignored if --from is set)")
    p.add_argument("--from", dest="date_from", type=str, default=None,
                   help="start date, e.g. 2026-03-15")
    p.add_argument("--to", dest="date_to", type=str, default=None,
                   help="end date, e.g. 2026-04-15")
    p.add_argument("--refresh",action="store_true")
    p.add_argument("--output",default="vol_dashboard.png")
    p.add_argument("--config", default=None, help="config 路径，默认 config.ini 回退 config_template.ini")
    a = p.parse_args()

    if a.date_from:
        start = a.date_from
    else:
        start = (dt.date.today()-dt.timedelta(days=a.months*30)).isoformat()

    config = _load_config(a.config)
    config.set("CONFIG", "FUTU_PUSH_TYPE", "K_DAY")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    df = build(config, start, a.refresh)

    if a.date_to:
        df = df.loc[:a.date_to]

    if df.empty: print("No data."); sys.exit(1)
    summary(df); plot(df, a.output)

if __name__ == "__main__": main()
