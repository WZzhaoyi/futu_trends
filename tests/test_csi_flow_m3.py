"""M3 测试：不连接 Futu，用合成快照（csi_flow_fixture）回放并逐根推送。

检查：
* m1 预设与独立的 M1 规则（csi_flow_timing.decide_at_close 逐根推进）逐笔一致；m3 的目标仓位是三层的加权和；
* 快照覆盖加回满仓、止损、逐层离场、三层同时离场与 T+1 延后；
* 三个预设的在线信号与假定成交都与回放逐笔一致；中途重启后不变；
* 权重非法、状态文件权重或策略不符、初始持仓参数不全时拒绝；强度排名不可用时不发进场；
* 通知文本：仓位档位随权重、原因符号、ETF 行、T+1 行与表格一行简版；在线引擎在 T+1 延后时标注不可当日卖出。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'market_analysis'), str(ROOT / 'tests')]
import csi_flow_fixture as fixture  # noqa: E402
import csi_flow_timing as m1  # noqa: E402
import csi_flow_timing_m3 as m3  # noqa: E402
import sheet_notifications  # noqa: E402

START = fixture.START
INPUTS = ROWS = KEYS = SCHEDULE = PANEL = None
_tmp = None


def setUpModule():
    global _tmp, INPUTS, ROWS, KEYS, SCHEDULE, PANEL
    _tmp = tempfile.TemporaryDirectory()
    INPUTS = fixture.write(Path(_tmp.name))
    ROWS = json.loads(INPUTS['bars'].read_text())['bars']
    KEYS = [r['time_key'] for r in ROWS]
    SCHEDULE = m3.schedule_for(INPUTS['thresholds'])
    PANEL = m3.load_etf_panel(INPUTS['panel'])


def tearDownModule():
    _tmp.cleanup()


def replay(weights):
    return m3.replay(INPUTS['bars'], START, thresholds=INPUTS['thresholds'], weights=weights, etf_panel=PANEL)


def m1_actions(bars, features, schedule, start):
    """M1 规则的独立推进：收盘用 decide_at_close 判定，下一根开盘成交；当日买入后的卖出延到次一交易日首根。"""
    actions, state, pending, deferred, entry_day = [], 0, None, None, None
    for i, bar in enumerate(bars):
        if bar.key < start:
            continue
        ready = deferred is not None and bar.trading_date > deferred[1]
        side, source = ('SELL', deferred[0]) if ready and state == 1 else pending or (None, None)
        if ready:
            deferred = None
        if side:
            actions.append((side, source, bar.key))
            state, pending = int(side == 'BUY'), None
            entry_day = bar.trading_date if state else None
        if features[i] is None or i == len(bars) - 1:
            continue
        sellable = bool(state and bar.trading_date > entry_day)
        _, _, action, defer = m1.decide_at_close(state, sellable, bar.clock, features[i], schedule[bar.trading_date[:7]])
        if action:
            pending = (action, bar.key)
        elif defer and deferred is None:
            deferred = (bar.key, bar.trading_date)
    return actions


class ScheduleProvider:
    def __init__(self, schedule, missing=()):
        self.schedule, self.missing = schedule, set(missing)

    def for_date(self, value):
        month = value[:7]
        if month in self.missing or month not in self.schedule:
            raise ValueError(f'缺少 {month} 阈值')
        return self.schedule[month]


def run_engine(preset, start_index, stop_index, state_path, events, panel=None):
    """以 rows[start_index] 为正在形成的 K 线预热，再逐根推送到 rows[stop_index-1]；ETF 与指数同节奏推送。"""
    weights, tag = m3.resolve_weights(preset)
    panel = PANEL if panel is None else panel
    engine = m3.LiveEngine(ScheduleProvider(SCHEDULE), m3.LiveState(state_path, weights), events.append, tag=tag)
    first = ROWS[start_index]['time_key']
    by_key = {s: {r['time_key']: r for r in panel[s]} for s in m3.STRENGTH_SYMBOLS}
    engine.bootstrap_strength({s: [r for r in panel[s] if r['time_key'] <= first][-m3.HISTORY_BARS:]
                               for s in m3.STRENGTH_SYMBOLS})
    engine.bootstrap(ROWS[max(0, start_index - m3.HISTORY_BARS + 1):start_index + 1])
    for row in ROWS[start_index + 1:stop_index]:
        for s in m3.STRENGTH_SYMBOLS:
            if row['time_key'] in by_key[s]:
                engine.on_strength_bar(s, engine.bar_from_row(by_key[s][row['time_key']]))
        engine.on_bar(engine.bar_from_row(row))
    return engine


def live_fills(events):
    return [(e['execution_bar_end'], e['position_after'], e['etf']) for e in events
            if e['type'] == 'EXECUTION_ASSUMED' and e['position_before'] != e['position_after']]


def live_signals(events):
    return [(e['bar_key'], e['action'], e['position_after'], e['reasons']) for e in events
            if e['type'] == 'SIGNAL' and e['action'] != 'NONE']


def expected(trades):
    fills = [(t.execution_bar_end, t.position_after, t.symbol) for t in trades.itertuples()]
    signals = [(t.signal, t.side, t.position_after, t.reasons.split('·')) for t in trades.itertuples()]
    return fills, signals


class PresetReplayTest(unittest.TestCase):
    def test_m1_preset_matches_m1_rules(self):
        bars = m1.load_bars(INPUTS['bars'], symbol=m3.SYMBOL)
        _, got, _, _ = replay(m3.PRESETS['m1'])
        want = m1_actions(bars, m1.build_features(bars), SCHEDULE, START)
        self.assertTrue(len(want) >= 4)
        self.assertEqual([(t.side, t.signal[:16], t.execution_bar_end[:16]) for t in got.itertuples()],
                         [(side, signal[:16], execution[:16]) for side, signal, execution in want])

    def test_m3_target_is_weighted_sum_of_layers(self):
        layers = [replay(w)[2].target for w in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
        target = replay(m3.PRESETS['m3'])[2].target
        self.assertTrue(((target - (.5 * layers[0] + .25 * layers[1] + .25 * layers[2])).abs() < 1e-12).all())

    def test_fixture_covers_readd_stop_staircase_full_exit_and_t1(self):
        _, t, _, _ = replay(m3.PRESETS['m3'])
        self.assertTrue(((t.side == 'BUY') & (t.position_before > 0)).any())                   # 加回满仓
        self.assertTrue(t.reasons.str.contains('S').any())                                       # 止损
        self.assertTrue(((t.side == 'SELL') & (t.position_after > 0)).any())                   # 逐层离场
        self.assertTrue(((t.side == 'SELL') & (t.position_before == 1) & (t.position_after == 0)).any())
        self.assertTrue((t.execution_bar_end.str[:10] > t.signal.str[:10]).any())                # T+1 延后


class LiveReplayEquivalenceTest(unittest.TestCase):
    def test_each_preset_matches_its_replay(self):
        for preset in ('m3', 'm1', 'm2'):
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as tmp:
                events = []
                run_engine(preset, KEYS.index(START), len(ROWS), Path(tmp) / 'state.json', events)
                fills, signals = expected(replay(m3.PRESETS[preset])[1])
                self.assertEqual(live_fills(events), fills)
                self.assertEqual(live_signals(events), signals)
                self.assertFalse([e for e in events if e['type'] == 'ERROR'])
                if preset == 'm3':                                              # 当日买入后离场：通知标注 T+1
                    self.assertTrue([e for e in events if e['type'] == 'SIGNAL' and e['action'] == 'SELL'
                                     and not e['t1_sellable']])

    def test_restart_mid_run_reproduces_the_uninterrupted_fills(self):
        fills, _ = expected(replay(m3.PRESETS['m3'])[1])
        for middle in (KEYS.index(START) + 120, KEYS.index(START) + 333):
            with self.subTest(middle=middle), tempfile.TemporaryDirectory() as tmp:
                state, first, second = Path(tmp) / 'state.json', [], []
                run_engine('m3', KEYS.index(START), middle, state, first)
                run_engine('m3', middle - 1, len(ROWS), state, second)
                self.assertEqual(live_fills(first) + live_fills(second), fills)
                self.assertFalse([e for e in second if e['type'] == 'ERROR'])


class FailClosedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name) / 'state.json'

    def tearDown(self):
        self.tmp.cleanup()

    def test_invalid_weights_are_rejected(self):
        for kwargs in (dict(weights='0.5,0.5,0.5'), dict(weights='-0.5,1,0.5'), dict(weights='1,0'),
                       dict(preset='m1', weights='1,0,0'), dict(preset='m4')):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                m3.resolve_weights(**kwargs)
        self.assertEqual(m3.resolve_weights('m1'), ((1.0, 0.0, 0.0), 'M1'))
        self.assertEqual(m3.resolve_weights(weights='0.5,0.25,0.25'), ((0.5, 0.25, 0.25), 'M3(0.5/0.25/0.25)'))

    def test_state_with_other_weights_or_strategy_is_rejected(self):
        m3.LiveState(self.path, m3.PRESETS['m1'])
        with self.assertRaisesRegex(ValueError, '权重'):
            m3.LiveState(self.path, m3.PRESETS['m3'])
        self.path.write_text(json.dumps({'strategy': 'm1', 'version': m1.VERSION}))
        with self.assertRaises(ValueError):
            m3.LiveState(self.path, m3.PRESETS['m3'])

    def test_initial_layers_require_matching_count_entry_date_and_etf(self):
        with self.assertRaises(ValueError):
            m3.LiveState(self.path, m3.PRESETS['m3'], [1, 1])
        with self.assertRaises(ValueError):
            m3.LiveState(self.path, m3.PRESETS['m3'], [1, 1, 1], '2026-02-12', None)
        state = m3.LiveState(self.path, m3.PRESETS['m3'], [0, 0, 1], '2026-02-12', 'SH.510500')
        self.assertEqual([ls['position'] for ls in state.layers.values()], [0, 0, 1])

    def test_entry_without_strength_ranking_is_not_published(self):
        empty = {s: PANEL[s][:m3.STRENGTH_N] for s in m3.STRENGTH_SYMBOLS}       # 只有最早的几根，排名必然失败
        first = KEYS.index(replay(m3.PRESETS['m3'])[1].signal.iloc[0])
        events = []
        engine = run_engine('m3', KEYS.index(START), first + 3, self.path, events, empty)
        self.assertFalse([e for e in events if e['type'] == 'EXECUTION_ASSUMED'])
        self.assertTrue([e for e in events if e['type'] == 'ERROR' and '强度排名不可用' in e['message']])
        self.assertFalse(engine.state.pending)


class FormatTest(unittest.TestCase):
    def test_position_follows_weights(self):
        cases = {0: '0', 1: '1', .25: '¼', .5: '½', .75: '¾', .375: '3/8', 1 / 3: '1/3', .3: '30%'}
        for value, text in cases.items():
            self.assertEqual(m3.format_position(value), text)

    def test_reason_lists_methods_in_fixed_order(self):
        self.assertEqual(m3.format_reason(['H', 'X', 'M1']), '动量转弱、价格破位、趋势结束（M1·X·H）')
        self.assertEqual(m3.format_reason(['S']), '止损（S）')

    def test_entry_and_exit_messages(self):
        base = dict(type='SIGNAL', tag='M3', bar_key='2026-05-21 10:30:00')
        _, text = m3.format_event({**base, 'action': 'BUY', 'position_before': 0, 'position_after': 1,
                                   'reason': '动量启动（M）', 'strength': {'ranking': [
                                       {'rank': 1, 'symbol': 'SH.510500', 'score': 0.012}]}})
        self.assertEqual(text, '[M3] SH.000902 进场\n信号K线: 2026-05-21 10:30\n仓位: 0 → 1\n'
                               '原因: 动量启动（M）\n强度排名: SH.510500(1.200%)')
        _, text = m3.format_event({**base, 'action': 'BUY', 'position_before': .25, 'position_after': 1,
                                   'reason': '动量启动（M）', 'strength': None, 'held_etf': 'SH.510500'})
        self.assertTrue(text.endswith('仓位: ¼ → 1\n原因: 动量启动（M）\n持有ETF: SH.510500'))
        subject, text = m3.format_event({**base, 'action': 'SELL', 'position_before': 1, 'position_after': .5,
                                         'reason': '动量转弱（M1）', 'held_etf': 'SH.510500', 't1_sellable': False})
        self.assertEqual(subject, 'M3 SH.000902 离场')
        self.assertTrue(text.endswith('原因: 动量转弱（M1）\n持有ETF: SH.510500；T+1：次一交易日开盘卖出'))

    def test_sheet_line(self):
        sent = []

        class Engine:
            def send_google_sheet_message(self, message, sheet_date):
                sent.append(message)

            def send_feishu_sheet_message(self, message, sheet_date):
                pass

        event = dict(type='SIGNAL', action='SELL', symbol='SH.000902', tag='M1', position_text='1→0',
                     reason='动量转弱（M1）', emitted_at='2026-05-22T14:00:04')
        sheet_notifications.send_sheet_signal(Engine(), 'm3', event)
        self.assertEqual(sent, ['14:00:04 M1 SH.000902 离场 1→0 动量转弱(M1)'])


if __name__ == '__main__':
    unittest.main()
