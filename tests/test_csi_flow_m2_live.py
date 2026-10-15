"""M2 在线引擎测试：不连接 Futu，用合成快照（csi_flow_m2_fixture）逐根推送。

检查：
* 在线假定成交与回测 replay 逐笔一致（信号根、执行根、方向、ETF），含跨长假持仓、同日止损延至次日的 T+1 卖出、
  冷却期内的买入信号与跨月换阈值；
* 中途重启后成交不变，停机期间的 K 线只报告、不补发信号；
* 当月阈值缺失时拒绝判定，强度排名不可用时不发买入；
* 拒绝其他策略或版本的状态文件，初始持仓参数不全时拒绝启动。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'market_analysis'), str(ROOT / 'tests')]
import csi_flow_m2_fixture as fixture  # noqa: E402
import csi_flow_timing_m2 as m2  # noqa: E402

START = fixture.START
INPUTS = ROWS = KEYS = SCHEDULE = PANEL = EXPECTED = None
_tmp = None


def setUpModule():
    global _tmp, INPUTS, ROWS, KEYS, SCHEDULE, PANEL, EXPECTED
    _tmp = tempfile.TemporaryDirectory()
    INPUTS = fixture.write(Path(_tmp.name))
    ROWS = json.loads(INPUTS['bars'].read_text())['bars']
    KEYS = [r['time_key'] for r in ROWS]
    SCHEDULE = m2.schedule_for(INPUTS['thresholds'])
    PANEL = m2.load_etf_panel(INPUTS['panel'])
    _, trades, _, _ = m2.replay(INPUTS['bars'], START, thresholds=INPUTS['thresholds'], etf_panel=PANEL)
    EXPECTED = [(t.side, t.signal, t.execution_bar_end, t.symbol) for t in trades.itertuples()]


def tearDownModule():
    _tmp.cleanup()


class ScheduleProvider:
    def __init__(self, schedule, missing=()):
        self.schedule, self.missing = schedule, set(missing)

    def for_date(self, value):
        month = value[:7]
        if month in self.missing or month not in self.schedule:
            raise ValueError(f'缺少 {month} 阈值')
        return self.schedule[month]


def run_engine(rows, start_index, stop_index, state_path, provider, panel, events):
    """以 rows[start_index] 为正在形成的 K 线预热，再逐根推送到 rows[stop_index-1]。

    ETF 与指数同节奏推送：预热只给到同一时点，之后每根指数 K 线到达前先推送同一时刻的 ETF K 线。
    """
    engine = m2.LiveEngine(provider, m2.LiveState(state_path), events.append)
    first = rows[start_index]['time_key']
    by_key = {s: {} for s in m2.STRENGTH_SYMBOLS}
    for s in m2.STRENGTH_SYMBOLS:
        for r in panel[s]:
            by_key[s][r['time_key']] = r
    engine.bootstrap_strength({s: [r for r in panel[s] if r['time_key'] <= first][-m2.HISTORY_BARS:]
                               for s in m2.STRENGTH_SYMBOLS})
    engine.bootstrap(rows[max(0, start_index - m2.HISTORY_BARS + 1):start_index + 1])
    for row in rows[start_index + 1:stop_index]:
        for s in m2.STRENGTH_SYMBOLS:
            if row['time_key'] in by_key[s]:
                engine.on_strength_bar(s, engine.bar_from_row(by_key[s][row['time_key']]))
        engine.on_bar(engine.bar_from_row(row))
    return engine


def fills(events):
    return [(e['side'], e['signal_bar'], e['execution_bar_end'], e['etf']) for e in events
            if e['type'] == 'EXECUTION_ASSUMED']


class LiveReplayEquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.keys, cls.schedule, cls.panel, cls.expected = ROWS, KEYS, SCHEDULE, PANEL, EXPECTED
        cls.start = cls.keys.index(START)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.events = []
        cls.engine = run_engine(cls.rows, cls.start, len(cls.rows), Path(cls.tmp.name) / 'state.json',
                                ScheduleProvider(cls.schedule), cls.panel, cls.events)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_fills_match_replay_including_holiday_t1_and_month_switch(self):
        got = fills(self.events)
        self.assertEqual(got, self.expected)
        pairs = list(zip(got[::2], got[1::2]))
        self.assertGreaterEqual(len(pairs), 4)
        self.assertEqual({b[1][:7] for b, _ in pairs}, {'2026-02', '2026-03'})
        self.assertTrue(any(b[2] < '2026-02-16' and s[2] > '2026-02-20' for b, s in pairs))   # 跨长假持仓
        deferred = [s for b, s in pairs if s[1][:10] == b[2][:10]]           # 入场当日的卖出信号
        self.assertTrue(deferred)
        self.assertTrue(all(s[2][:10] > s[1][:10] for s in deferred))      # T+1：延至下一交易日开盘

    def test_signal_events_are_emitted_once_per_fill(self):
        signals = [(e['action'], e['bar_key']) for e in self.events if e['type'] == 'SIGNAL' and e['action'] != 'NONE']
        self.assertEqual(signals, [(f[0], f[1]) for f in self.expected])
        buys = [e for e in self.events if e['type'] == 'SIGNAL' and e['action'] == 'BUY']
        self.assertTrue(all(e['strength']['ranking'][0]['symbol'] in m2.STRENGTH_SYMBOLS for e in buys))

    def test_restart_mid_run_reproduces_the_uninterrupted_fills(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state.json'; events = []
            middle = self.keys.index('2026-03-19 13:15:00')               # 持仓中、待T+1卖出之前重启
            run_engine(self.rows, self.start, middle, state, ScheduleProvider(self.schedule), self.panel, events)
            restarted = []
            run_engine(self.rows, middle - 1, len(self.rows), state, ScheduleProvider(self.schedule), self.panel, restarted)
            self.assertEqual(fills(events) + fills(restarted), self.expected)
            self.assertFalse([e for e in restarted if e['type'] == 'ERROR'])

    def test_missed_bars_during_downtime_are_reported_not_backfilled(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state.json'; events = []
            run_engine(self.rows, self.start, self.start + 40, state, ScheduleProvider(self.schedule), self.panel, events)
            later = []
            run_engine(self.rows, self.start + 60, self.start + 61, state, ScheduleProvider(self.schedule), self.panel, later)
            errors = [e['message'] for e in later if e['type'] == 'ERROR']
            self.assertTrue(errors and '未判定' in errors[0])
            self.assertFalse([e for e in later if e['type'] == 'SIGNAL'])


class FailClosedTest(unittest.TestCase):
    def setUp(self):
        self.rows, self.schedule, self.panel = ROWS, SCHEDULE, PANEL
        self.start = KEYS.index(START)
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_current_month_threshold_raises(self):
        with self.assertRaises(ValueError):
            run_engine(self.rows, self.start, self.start + 3, Path(self.tmp.name) / 's.json',
                       ScheduleProvider(self.schedule, missing={'2026-02'}), self.panel, [])

    def test_buy_without_strength_ranking_is_not_published(self):
        empty = {s: self.panel[s][:m2.STRENGTH_N] for s in m2.STRENGTH_SYMBOLS}   # 只有最早的几根，排名必然失败
        events = []
        first_buy = KEYS.index(next(f[1] for f in EXPECTED if f[0] == 'BUY'))
        engine = run_engine(self.rows, self.start, first_buy + 3, Path(self.tmp.name) / 's.json',
                            ScheduleProvider(self.schedule), empty, events)
        self.assertFalse([e for e in events if e['type'] == 'EXECUTION_ASSUMED'])
        self.assertTrue([e for e in events if e['type'] == 'ERROR' and '强度排名不可用' in e['message']])
        self.assertIsNone(engine.state.pending)

    def test_state_of_another_strategy_is_rejected(self):
        path = Path(self.tmp.name) / 'state.json'
        path.write_text(json.dumps({'strategy': 'm1', 'version': 'M1-LF-held-downside-exact-grid-strength-t1-defer-v2'}))
        with self.assertRaises(ValueError):
            m2.LiveState(path)

    def test_initial_long_requires_entry_date_and_etf(self):
        with self.assertRaises(ValueError):
            m2.LiveState(Path(self.tmp.name) / 'long.json', 'long', '2026-05-20', None)


class FormatTest(unittest.TestCase):
    def test_buy_and_sell_messages(self):
        base = dict(type='SIGNAL', bar_key='2026-05-21 10:30:00', position_before=0, reason='M成立且D或P至少一票',
                    threshold={'month': '2026-05'}, signals=dict(z30=1.2, z60=1.6, D=1.1, P=0.8, strong=True))
        subject, text = m2.format_event({**base, 'action': 'BUY', 'strength': {'ranking': [
            {'rank': 1, 'symbol': 'SH.510500', 'score': 0.012}]}})
        self.assertEqual(subject, 'M2 SH.000902 BUY')
        self.assertIn('SH.510500', text); self.assertIn('下一根开盘执行', text)
        _, text = m2.format_event({**base, 'action': 'SELL', 'held_etf': 'SH.510500', 't1_sellable': False})
        self.assertIn('T+1', text); self.assertIn('SH.510500', text)


if __name__ == '__main__':
    unittest.main()
