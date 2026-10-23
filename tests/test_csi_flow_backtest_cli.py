"""中证流通择时（M3）backtest CLI 端到端：运行 `backtest`，再从导出结果逐项独立重建。

Checks:
* 指数净值与 ETF 账本由原始行情和导出成交重建（下一根开盘、成本、按目标仓位×净值调整份额、当日买入份额不卖出）；
  ETF 成交价与净值是由原始价和复权事件直接算出的等比复权价（分红再投资），排名用面板中的前复权价；
* 每次加仓都在 M 成立的入场时点；从空仓进场是空仓后第一根入场根，ETF 用 polyfit 独立重算排名；
  持仓中加减仓不换 ETF；成交前后仓位首尾相接；
* 只给指数时信号相同；截短快照复现此前信号；
* ETF 缺根、面板缺复权事件、阈值缺失或训练窗泄露、指数缺根或非法时拒绝。
输入为 csi_flow_fixture 的合成快照，写在临时目录；不需要本地行情。设置 FUTU_TEST_ARTIFACT_DIR 时保留核对记录。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'market_analysis/csi_flow_timing_m3.py'
sys.path[:0] = [str(ROOT / 'market_analysis'), str(ROOT / 'tests')]
import csi_flow_fixture as fixture  # noqa: E402
import csi_flow_timing as m1  # noqa: E402


def verify(OUT: Path) -> dict:
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
    assert stats['asset'] == 'index' and stats['etf_execution'] is not None and stats['weights'] == [0.5, 0.25, 0.25]
    etf_stats = stats['etf_execution']
    assert manifest['inputs'][str(BARS.resolve())] == hashlib.sha256(BARS.read_bytes()).hexdigest()
    assert manifest['threshold_publication_is_retrospective']
    curve = pd.read_csv(out / 'equity.csv', index_col=0, parse_dates=True)
    fills = pd.read_csv(out / 'trades.csv')
    sig = pd.read_csv(out / 'signals.csv', index_col=0, parse_dates=True)
    index_frame = pd.DataFrame(json.loads(BARS.read_text())['bars']).set_index('time_key')
    panel = json.loads(PANEL.read_text())['panel']
    frames = {s: pd.DataFrame(rows).set_index('time_key') for s, rows in panel.items()}       # 前复权：排名
    _, raw_panel = fixture.generate()

    def ratio_adjusted(rows, events):
        """等比复权 = 原始价 × Π(A + B / 除权前一日原始收盘)，乘积取在该根之后的除权事件。"""
        raw = pd.DataFrame(rows).set_index('time_key')
        day = raw.index.str[:10]
        factor = np.ones(len(raw))
        for ex_date, a, b in events:
            prev_close = raw.close[day < ex_date].iloc[-1]
            factor *= np.where(day < ex_date, a + b / prev_close, 1.0)
        return raw[['open', 'close']].mul(factor, axis=0)
    fill_frames = {s: ratio_adjusted(raw_panel[s], fixture.ADJUSTMENT_EVENTS[s]) for s in raw_panel}

    ix = curve.index
    sessions = pd.factorize(ix.normalize())[0]
    gate = sig.loc[ix]; gate = (gate.M.astype(bool) & ix.strftime('%H:%M').isin(['10:30', '14:00'])).to_numpy()
    fee = 1 - stats['cost_bps_per_side'] / 10000
    books = {'etf': [1., 0.], 'index': [1., 0.]}          # cash, units
    fresh = {'etf': 0., 'index': 0.}
    held, held_before, position, flat_from, j, session = None, None, 0., 0, 0, -1
    nav, index_nav, target, crossed = [], [], [], set()

    def trade(book, price, after):
        cash, units = books[book]
        want = after * (cash + units * price) / price
        if want > units:
            qty = want - units; cost = qty * price / fee
            if cost > cash:
                qty, cost = cash * fee / price, cash
            cash -= cost; units += qty; fresh[book] += qty
        else:
            qty = units - want
            assert qty <= units - fresh[book] + 1e-12          # T+1：当日买入的份额不卖出
            cash += qty * price * fee; units -= qty
        books[book] = [cash, units]

    for i, ts in enumerate(ix):
        if sessions[i] != session:
            session, fresh = sessions[i], {'etf': 0., 'index': 0.}
        if j < len(fills) and pd.Timestamp(fills.iloc[j].execution_bar_end) == ts:
            fill = fills.iloc[j]
            src = ix.get_loc(pd.Timestamp(fill.signal))
            assert i >= src + stats['lag_bars'] and fill.execution_time == str(ts - pd.Timedelta(minutes=15))
            assert np.isclose(fill.position_before, position) and fill.position_after != position
            assert np.isclose(fill.price, fill_frames[fill.symbol].loc[str(ts), 'open'])
            assert np.isclose(fill.index_price, index_frame.loc[str(ts), 'open'])
            if fill.side == 'BUY':
                assert fill.position_after > position and fill.signal[11:16] in ('10:30', '14:00') and sig.loc[fill.signal].M
                assert fill.reasons == 'M'
            else:
                assert fill.position_after < position and fill.reasons
            if position == 0:
                assert fill.side == 'BUY' and src == flat_from + int(np.argmax(gate[flat_from:]))   # 空仓后第一根入场根
                # polyfit 与 etf_leader 的中心化点积公式相互独立。
                common = set.intersection(*(set(f.index[f.index <= fill.signal]) for f in frames.values()))
                keys = sorted(common)[-9:]
                assert keys[-1] == fill.signal and len(keys) == 9
                slopes = {s: np.polyfit(np.arange(9), np.log(f.loc[keys, 'close']), 1)[0] for s, f in frames.items()}
                held = max(slopes, key=slopes.get)
            assert fill.symbol == held                                      # 持仓中加减仓不换 ETF
            trade('etf', fill.price, fill.position_after)
            trade('index', fill.index_price, fill.position_after)
            position = fill.position_after
            if position == 0:
                flat_from = i                                               # 清仓成交根收盘即可再判定入场
            j += 1
        cash, units = books['etf']
        if units > 1e-12 and i and held == held_before:
            crossed |= {(held, e) for e, _, _ in fixture.ADJUSTMENT_EVENTS[held] if str(ix[i - 1])[:10] < e <= str(ts)[:10]}
        held_before = held if units > 1e-12 else None
        nav.append(cash + (units * fill_frames[held].loc[str(ts), 'close'] if units > 1e-12 else 0.))
        cash, units = books['index']
        index_nav.append(cash + units * index_frame.loc[str(ts), 'close'])
        target.append(position)
    assert j == len(fills) and len(fills) > 0
    # 合成持仓跨过现金分红与拆分的除权日
    assert {('SH.510050', '2026-03-13'), ('SH.512100', '2026-02-24'), ('SH.512100', '2026-03-25')} <= crossed, crossed
    np.testing.assert_allclose(target, curve.target, atol=1e-12)
    np.testing.assert_allclose(nav, curve.etf_equity, rtol=1e-11)
    assert np.isclose(nav[-1] - 1, etf_stats['total_return'])
    path = np.r_[1., nav]
    assert np.isclose((path / np.maximum.accumulate(path) - 1).min(), etf_stats['max_drawdown'])
    np.testing.assert_allclose(index_nav, curve.index_equity, rtol=1e-11)
    assert np.isclose(index_nav[-1] - 1, stats['total_return'])
    path = np.r_[1., index_nav]
    assert np.isclose((path / np.maximum.accumulate(path) - 1).min(), stats['max_drawdown'])
    assert set(fills.side) == {'BUY', 'SELL'} and ((fills.side == 'BUY') & (fills.position_before > 0)).any()

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
    unadjusted = json.loads(PANEL.read_text()); unadjusted.pop('adjustment_events')
    bad_panel.write_text(json.dumps(unadjusted))
    run('missing_events', '--etf-panel', str(bad_panel), success=False, reason='缺少前复权口径或复权事件')
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
    checks = ['independent_index_NAV', 'independent_ETF_cash_units_NAV', 'target_chain',
              'index_only_same_signals', 'all_fill_prices', 'all_rankings', 'same_etf_within_episode',
              'signal_gates', 'next_bar_execution', 'T+1', 'first_entry_after_flat',
              'prefix_causality', 'missing_ETF_rejection', 'missing_adjustment_events_rejection',
              'ratio_adjusted_fills', 'future_training_rejection',
              'missing_threshold_rejection', 'invalid_index_rejection', 'missing_bar_rejection']
    evidence = dict(status='PASS', checks=checks, fills=len(fills), bars=len(curve),
                    inputs=manifest['inputs'], total_return=stats['total_return'], etf_total_return=etf_stats['total_return'])
    return evidence


class BacktestCliTest(unittest.TestCase):
    def test_exported_results_rebuild_independently_and_bad_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = verify(Path(tmp))
        self.assertEqual(evidence['status'], 'PASS')
        if output := os.environ.get('FUTU_TEST_ARTIFACT_DIR'):
            path = Path(output) / 'csi-flow-backtest-cli.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    unittest.main()
