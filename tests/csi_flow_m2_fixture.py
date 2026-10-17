"""M2 测试用合成输入：指数 15 分钟快照、四只 ETF 面板与逐月阈值发布，固定种子、无需本地行情。

日历：2026-01-05 起的工作日，2026-02-16 至 02-20 整周休市；预热约 28 个交易日后进入 START。
阈值逐月不同，测试区间跨 2 月、3 月，覆盖换月、长假前后的 T+1 与卖出后再入场。
"""
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np

import csi_flow_timing as m1
import csi_flow_timing_m2 as m2

SEED = 84
FIRST_DAY, LAST_DAY = date(2026, 1, 5), date(2026, 3, 31)
HOLIDAY = (date(2026, 2, 16), date(2026, 2, 20))
START = '2026-02-12 09:45:00'
THRESHOLDS = {           # month: (entry_z30, entry_z60)
    '2026-01': (0.0, 0.5),
    '2026-02': (0.25, 0.75),
    '2026-03': (0.0, 0.25),
}
PUBLISHED_AT = '2026-04-01T00:00:00+08:00'   # 与实际发布一致：各月阈值事后统一回溯发布
ETF_LEVELS = {'SH.510300': 4.0, 'SH.510500': 6.0, 'SH.510050': 3.0, 'SH.512100': 1.5}
ETF_BETAS = {'SH.510300': 0.9, 'SH.510500': 1.2, 'SH.510050': 0.7, 'SH.512100': 1.5}


def sessions():
    day, days = FIRST_DAY, []
    while day <= LAST_DAY:
        if day.weekday() < 5 and not HOLIDAY[0] <= day <= HOLIDAY[1]:
            days.append(day)
        day += timedelta(days=1)
    return days


def _bars(keys, returns, level, rng):
    rows, prev = [], level
    for key, r in zip(keys, returns):
        gap = rng.normal(0, 0.0005) if key.endswith('09:45:00') else 0.0
        op = prev * np.exp(gap); cl = op * np.exp(r)
        wick = np.abs(rng.normal(0, 0.0008, 2))
        rows.append(dict(time_key=key, open=round(op, 4), high=round(max(op, cl) * (1 + wick[0]), 4),
                         low=round(min(op, cl) * (1 - wick[1]), 4), close=round(cl, 4),
                         volume=float(rng.integers(10 ** 8, 10 ** 9)),
                         turnover=float(rng.integers(10 ** 10, 10 ** 11))))
        prev = cl
    return rows


def generate(seed=SEED):
    """返回 (指数行, {ETF: 行})。日内漂移按交易日分段，制造趋势入场与日内急跌止损。"""
    rng = np.random.default_rng(seed)
    days = sessions()
    keys = [f'{d.isoformat()} {t}:00' for d in days for t in m2.TIMES]
    drift = np.repeat(rng.choice([-0.0015, 0.0, 0.0, 0.0012, 0.002], len(days)), len(m2.TIMES))
    index_r = drift + rng.normal(0, 0.002, len(keys))
    index_rows = _bars(keys, index_r, 6000.0, rng)
    panel = {s: _bars(keys, ETF_BETAS[s] * index_r + rng.normal(0, 0.0015, len(keys)), ETF_LEVELS[s], rng)
             for s in m2.STRENGTH_SYMBOLS}
    return index_rows, panel


def _threshold(month, entry, bars):
    available = date.fromisoformat(month + '-01')
    window_end = available - timedelta(days=1)
    window_start = (window_end.replace(day=1) - timedelta(days=60)).replace(day=1)
    train = [b for b in bars if window_start.isoformat() <= b.trading_date <= window_end.isoformat()]
    return dict(publication_kind='monthly_threshold', strategy='m1', version=m2.M_THRESHOLD_SOURCE,
                month=month, available_from=available.isoformat(), published_at=PUBLISHED_AT,
                window_start=window_start.isoformat(), window_end=window_end.isoformat(),
                threshold=dict(entry_z30=entry[0], entry_z60=entry[1], exit_z30=0.0, exit_z60=0.0,
                               month=month, source=m2.M_THRESHOLD_SOURCE),
                data_audit=dict(training_bars_sha256=m1.sha256_bars(train),
                                first_training_bar=f'{window_start} 09:45', last_training_bar=f'{window_end} 15:00'))


def write(directory: Path, seed=SEED) -> dict:
    """写出 bars.json、panel.json 与 thresholds/，返回三者路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    index_rows, panel = generate(seed)
    bars = directory / 'bars.json'
    bars.write_text(json.dumps(dict(symbol=m2.SYMBOL, bar_time_convention='end', autype='NONE', bars=index_rows)))
    etf = directory / 'panel.json'
    etf.write_text(json.dumps(dict(symbols=list(m2.STRENGTH_SYMBOLS), panel=panel)))
    thresholds = directory / 'thresholds'; thresholds.mkdir(exist_ok=True)
    parsed = m1.load_bars(bars, symbol=m2.SYMBOL)
    for month, entry in THRESHOLDS.items():
        (thresholds / f'threshold_{month}.json').write_text(json.dumps(_threshold(month, entry, parsed)))
    return dict(bars=bars, panel=etf, thresholds=thresholds)
