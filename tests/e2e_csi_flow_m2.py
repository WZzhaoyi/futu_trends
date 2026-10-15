"""M2 backtest CLI end to end: run `backtest`, then rebuild every exported result independently.

Checks:
* index NAV and ETF ledger rebuilt from raw bars and exported fills (next-bar open, costs, T+1);
* entry gates, entry clocks and cooldown on every BUY; ETF ranking recomputed with polyfit;
* an index-only run produces the same signals; a shorter snapshot reproduces earlier signals;
* missing ETF bars, missing or leaking monthly thresholds, and missing or invalid index bars are rejected.
Inputs are the synthetic snapshot from csi_flow_m2_fixture, written under OUT/inputs; no local market data needed.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/csi_flow_m2_e2e'
ENGINE = ROOT / 'market_analysis/csi_flow_timing_m2.py'
sys.path[:0] = [str(ROOT / 'market_analysis'), str(ROOT / 'tests')]
import csi_flow_m2_fixture as fixture  # noqa: E402
import csi_flow_timing as m1  # noqa: E402


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    generated = fixture.write(OUT / 'inputs')
    BARS, PANEL, THRESHOLDS = generated['bars'], generated['panel'], generated['thresholds']

    def run(name, *extra, success=True, panel=True, reason=None):
        dest = OUT / name
        inputs = ['--bars', str(BARS), '--start', fixture.START, '--thresholds', str(THRESHOLDS)] + \
            (['--etf-panel', str(PANEL)] if panel else [])
        result = subprocess.run([sys.executable, str(ENGINE), 'backtest', *inputs,
                                 '--output', str(dest), *extra], capture_output=True, text=True)
        (OUT / f'{name}.log').write_text(result.stdout + result.stderr)
        assert (result.returncode == 0) == success, result.stderr
        assert reason is None or reason in result.stderr, result.stderr
        return dest

    out = run('full')
    stats = json.loads((out / 'metrics.json').read_text())
    manifest = json.loads((out / 'manifest.json').read_text())
    assert stats['asset'] == 'index' and stats['etf_execution'] is not None
    etf_stats = stats['etf_execution']
    assert manifest['inputs'][str(BARS)] == hashlib.sha256(BARS.read_bytes()).hexdigest()
    assert manifest['threshold_publication_is_retrospective']
    curve = pd.read_csv(out / 'equity.csv', index_col=0, parse_dates=True)
    fills = pd.read_csv(out / 'trades.csv')
    sig = pd.read_csv(out / 'signals.csv', index_col=0, parse_dates=True)
    index_rows = json.loads(BARS.read_text())['bars']
    index_frame = pd.DataFrame(index_rows).set_index('time_key')
    panel = json.loads(PANEL.read_text())['panel']
    frames = {s: pd.DataFrame(rows).set_index('time_key') for s, rows in panel.items()}
    ix = curve.index
    sessions = pd.factorize(ix.normalize())[0]
    cash, units, held, j = 1., 0., None, 0
    entered, last_sell = -1, None
    nav, index_nav = [], []
    icash, iunits = 1., 0.
    fee = 1 - stats['cost_bps_per_side'] / 10000
    for i, ts in enumerate(ix):
        if j < len(fills) and pd.Timestamp(fills.iloc[j].execution_bar_end) == ts:
            fill = fills.iloc[j]
            src = ix.get_loc(pd.Timestamp(fill.signal))
            assert i >= src + stats['lag_bars']
            assert fill.execution_time == str(ts - pd.Timedelta(minutes=15))
            assert np.isclose(fill.price, frames[fill.symbol].loc[str(ts), 'open'])
            assert np.isclose(fill.index_price, index_frame.loc[str(ts), 'open'])
            if fill.side == 'BUY':
                assert units == 0 and fill.signal[11:16] in ('10:30', '14:00')
                row = sig.loc[fill.signal]
                assert row.M and (row.D_up or row.P_up)
                if last_sell is not None:
                    assert sessions[src] >= last_sell + 3
                # polyfit 与 etf_leader 的中心化点积公式相互独立。
                common = set.intersection(*(set(f.index[f.index <= fill.signal]) for f in frames.values()))
                keys = sorted(common)[-9:]
                assert keys[-1] == fill.signal and len(keys) == 9
                slopes = {s: np.polyfit(np.arange(9), np.log(f.loc[keys, 'close']), 1)[0]
                          for s, f in frames.items()}
                assert fill.symbol == max(slopes, key=slopes.get)
                entered = sessions[i]; held = fill.symbol
                units, cash = cash * fee / fill.price, 0.
                iunits, icash = icash * fee / fill.index_price, 0.
            else:
                assert fill.side == 'SELL' and held == fill.symbol and sessions[i] > entered
                cash, units = units * fill.price * fee, 0.
                icash, iunits = iunits * fill.index_price * fee, 0.
                last_sell = sessions[i]
            j += 1
        nav.append(cash + (units * frames[held].loc[str(ts), 'close'] if units else 0.))
        index_nav.append(icash + iunits * index_frame.loc[str(ts), 'close'])
    assert j == len(fills) and len(fills) > 0
    np.testing.assert_allclose(nav, curve.etf_equity, rtol=1e-11)
    assert np.isclose(nav[-1] - 1, etf_stats['total_return'])
    path = np.r_[1., nav]
    assert np.isclose((path / np.maximum.accumulate(path) - 1).min(), etf_stats['max_drawdown'])
    np.testing.assert_allclose(index_nav, curve.index_equity, rtol=1e-11)
    assert np.isclose(index_nav[-1] - 1, stats['total_return'])
    path = np.r_[1., index_nav]
    assert np.isclose((path / np.maximum.accumulate(path) - 1).min(), stats['max_drawdown'])

    index_only = run('index_only', panel=False)
    only = json.loads((index_only / 'metrics.json').read_text())
    assert only['etf_execution'] is None and np.isclose(only['total_return'], stats['total_return'])
    assert (pd.read_csv(index_only / 'trades.csv').signal.tolist() == fills.signal.tolist())

    end = '2026-03-10 10:30:00'
    obj = json.loads(BARS.read_text())
    obj['bars'] = [r for r in obj['bars'] if r['time_key'] <= end]
    prefix = OUT / 'prefix.json'; prefix.write_text(json.dumps(obj))
    short = run('prefix', '--bars', str(prefix), '--end', end)
    small = pd.read_csv(short / 'signals.csv', index_col=0, parse_dates=True)
    pd.testing.assert_frame_equal(small, sig.loc[small.index], check_exact=False,
                                  atol=1e-10, rtol=1e-10)

    missing = json.loads(PANEL.read_text()); missing['panel']['SH.510300'].pop(-100)
    bad_panel = OUT / 'missing-etf.json'; bad_panel.write_text(json.dumps(missing))
    run('missing_etf', '--etf-panel', str(bad_panel), success=False, reason='不完整或非法交易时段')
    months = OUT / 'future-threshold'; months.mkdir(exist_ok=True)
    meta = json.loads((THRESHOLDS / 'threshold_2026-02.json').read_text())
    meta['window_end'] = meta['data_audit']['last_training_bar'] = '2026-02-27'   # 训练窗含生效月
    train = [b for b in m1.load_bars(BARS) if meta['window_start'] <= b.trading_date <= meta['window_end']]
    meta['data_audit']['training_bars_sha256'] = m1.sha256_bars(train)          # 哈希一致，只剩泄露可拒绝
    (months / 'threshold_2026-02.json').write_text(json.dumps(meta))
    run('future_training', '--thresholds', str(months), '--end', '2026-02-27 15:00:00', success=False,
        reason='训练窗口、月份或策略来源非法')
    partial = OUT / 'missing-month'; partial.mkdir(exist_ok=True)
    for month in ('2026-01', '2026-02'):
        (partial / f'threshold_{month}.json').write_text((THRESHOLDS / f'threshold_{month}.json').read_text())
    run('missing_month', '--thresholds', str(partial), success=False, reason="缺少月度阈值：['2026-03']")
    invalid = json.loads(BARS.read_text()); invalid['bars'][100]['turnover'] = 0
    bad = OUT / 'invalid-index.json'; bad.write_text(json.dumps(invalid))
    run('invalid_index', '--bars', str(bad), success=False, reason='非正价格/成交额')
    invalid = json.loads(BARS.read_text()); invalid['bars'].pop(-100)   # 测试区间内，早于各阈值训练窗末
    bad.write_text(json.dumps(invalid))
    run('missing_index', '--bars', str(bad), success=False, reason='不完整或非法交易时段')
    checks = ['independent_index_NAV', 'index_only_same_signals', 'independent_ETF_cash_units_NAV', 'all_fill_prices', 'all_rankings',
              'signal_gates', 'next_bar_execution', 'T+1', 'cooldown',
              'prefix_causality', 'missing_ETF_rejection', 'future_training_rejection',
              'missing_threshold_rejection', 'invalid_index_rejection', 'missing_bar_rejection']
    evidence = dict(status='PASS', checks=checks, fills=len(fills), bars=len(curve),
                    inputs=manifest['inputs'], total_return=stats['total_return'], etf_total_return=etf_stats['total_return'])
    (OUT / 'verification.json').write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
