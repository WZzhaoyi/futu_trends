"""orders YAML -> Tick -> market calendar/indicator -> event engine -> webhook -> restart.

Contracts: session-aligned periods, completed-window checks, parameter isolation,
notification entry/cooldown, missing-data handling, restart deduplication, one-time
trading actions and configuration validation. Market SDK boundaries use synthetic
feeds; notifications reach a local HTTP receiver.
"""
import configparser
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
import data
from notification_engine import NotificationEngine
from order_engine.__main__ import AsyncNotifier, collect_order_gateway_names
from order_engine.condition import ConditionOrderEngine
from order_engine.core import MainEngine, BaseGateway
from order_engine.event_engine import Event, EventEngine, EVENT_TIMER, EVENT_TICK
from order_engine.models import TickData, Exchange


class FeedGateway(BaseGateway):
    def __init__(self, *args):
        super().__init__(*args)
        self.subscriptions = set()
    def subscribe(self, request):
        self.subscriptions.add((request.symbol, request.exchange))
        return True
    def close(self):
        pass


class IndicatorOrdersE2E(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.received = []
        received = self.received
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.mains = []
        self.feeds = {}
        self.ticks = []
        data._kline_cache.clear()
        self.moment = datetime(2026, 9, 28, 16, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.config = configparser.ConfigParser()
        self.config.read_dict({'CONFIG': {
            'ORDER_CONFIG': str(self.root / 'orders.yaml'),
            'ORDER_STATE_FILE': str(self.root / 'state.json'),
            'ORDER_EVENT_LOG': str(self.root / 'events.jsonl'),
            'DATA_SOURCE': 'futu', 'FUTU_HOST': '127.0.0.1', 'FUTU_PORT': '11111',
            'FUTU_PUSH_TYPE': 'K_DAY', 'ORDER_TRADE_GATEWAY': 'QMT', 'ORDER_DATA_GATEWAY': 'FEED',
            'EMAIL_PORT': '465', 'EMAIL_SERVER': '', 'EMAIL_SENDER': '', 'EMAIL_PASWD': '',
            'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_CHAT_ID': '', 'PROXY': '',
            'WEBHOOK_TARGETS': 'local'},
            'WEBHOOK.local': {'url': f'http://127.0.0.1:{self.server.server_port}/',
                              'signing': 'none', 'content_field': 'message', 'event_id_field': 'event_id'}})
        self.fetch = patch('data.fetch_futu_data', side_effect=lambda code, *args: self.feeds[code].copy() if self.feeds.get(code) is not None else None)
        self.fetch_mock = self.fetch.start()
        import market_analysis.trading_calendar as calendar
        self.calendar_requests = []
        self.calendar_failure = False
        self.calendar_days = {
            'SH.000001': {'2026-10-01': None, '2026-10-02': None},
            'HK.800000': {'2026-12-24': 'MORNING'},
            'US.QQQ': {'2026-11-27': 'MORNING'},
        }
        owner = self
        class CalendarContext:
            def __init__(self, *args, **kwargs):
                pass
            def get_global_state(self):
                return 0, {f'market_{market}': 'MORNING' for market in ('sh', 'sz', 'hk', 'us')}
            def request_trading_days(self, start, end, code):
                owner.calendar_requests.append((start, end, code))
                if owner.calendar_failure:
                    return -1, 'calendar unavailable'
                rows = []
                for stamp in pd.bdate_range(start, end):
                    day = stamp.date().isoformat()
                    kind = owner.calendar_days.get(code, {}).get(day, 'WHOLE')
                    if kind is not None:
                        rows.append({'time': day, 'trade_date_type': kind})
                return 0, rows
            def close(self):
                pass
        self.calendar_context = CalendarContext
        self.calendar_patch = patch('futu.OpenQuoteContext', CalendarContext)
        self.calendar_patch.start()
        self.cache_patch = patch.object(calendar, 'CACHE_DIR', self.root / 'calendar')
        self.cache_patch.start()
        self.addCleanup(self.calendar_patch.stop)
        self.addCleanup(self.cache_patch.stop)
        self.addCleanup(self.fetch.stop)

    def tearDown(self):
        for main in self.mains:
            main.close()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.temp.cleanup()

    def feed(self, code, sessions):
        frames = []
        for start, end, price in sessions:
            index = pd.date_range(start, end, freq='min', inclusive='left')
            frames.append(pd.DataFrame({'open': price, 'high': price + 1,
                                       'low': price - 1, 'close': price, 'volume': 1}, index=index))
        self.feeds[code] = pd.concat(frames)

    def rule(self, ident, code, variable='tools.MA', params=None, timeframe='2h', output=None):
        exchange, symbol = code.split('.', 1)
        node = {'variable': variable, 'params': params or {'N': 1},
                'timeframe': timeframe, 'operator': '>', 'value': 0}
        if output:
            node['output'] = output
        return {'id': ident, 'enabled': True, 'symbol': symbol, 'exchange': exchange,
                'conditions': node,
                'actions': [{'type': 'notify', 'message': ident}],
                'notification': {'mode': 'on_enter', 'cooldown_seconds': 0}}

    def start(self, rules, auto_tick=True):
        (self.root / 'orders.yaml').write_text(yaml.safe_dump({'orders': rules}))
        main = MainEngine(EventEngine(interval=0.05))
        self.mains.append(main)
        main.add_gateway(FeedGateway, 'FEED')
        notifier = AsyncNotifier(NotificationEngine(self.config))
        engine = main.add_engine(ConditionOrderEngine)
        engine.load(self.config, notifier.notify)
        if auto_tick:
            self.tick(main)
        return main, engine

    def tick(self, main=None, codes=None, price=30):
        main = main or self.mains[-1]
        gateway = main._gateways['FEED']
        for symbol, exchange in gateway.subscriptions:
            if codes and (symbol, exchange) not in codes:
                continue
            tick = TickData(symbol=symbol, exchange=exchange, gateway_name='FEED',
                            datetime=self.moment, last_price=price)
            self.ticks.append({'symbol': symbol, 'exchange': exchange.value, 'time': self.moment.isoformat()})
            gateway.on_tick(tick)

    def wait(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail('event pipeline did not reach expected observable state')

    def artifact(self, name):
        destination = Path(os.environ.get('FUTU_TEST_ARTIFACT_DIR', '/tmp/futu-indicator-e2e'))
        destination.mkdir(parents=True, exist_ok=True)
        payload = {'ticks': self.ticks, 'clock': self.moment.isoformat(), 'timezone': str(self.moment.tzinfo),
                   'calendar_requests': self.calendar_requests, 'calendar_days': self.calendar_days,
                   'notifications': self.received,
                   'config': yaml.safe_load((self.root / 'orders.yaml').read_text()),
                   'state': json.loads((self.root / 'state.json').read_text()),
                   'audit': [json.loads(line) for line in (self.root / 'events.jsonl').read_text().splitlines()]
                            if (self.root / 'events.jsonl').exists() else [],
                   'feeds': {code: df.reset_index().to_json(orient='records', date_format='iso')
                             for code, df in self.feeds.items() if df is not None}}
        (destination / f'{name}.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    def test_final_quote_closes_all_hourly_windows(self):
        # Futu quote parsing and delivery cover the final full or shortened session window.
        from order_engine.gateway import FutuGateway
        from unittest.mock import Mock
        cases = [
            ('cn', 'SH.600000', '2026-09-28', 'Asia/Shanghai', [('09:30', '11:30'), ('13:00', '15:00')]),
            ('hk', 'HK.00700', '2026-09-28', 'Asia/Hong_Kong', [('09:30', '12:00'), ('13:00', '16:00')]),
            ('us_summer', 'US.AAPL', '2026-09-28', 'America/New_York', [('09:30', '16:00')]),
            ('us_winter', 'US.AAPL', '2026-11-30', 'America/New_York', [('09:30', '16:00')]),
            ('hk_half', 'HK.00700', '2026-12-24', 'Asia/Hong_Kong', [('09:30', '12:00')]),
            ('us_half', 'US.AAPL', '2026-11-27', 'America/New_York', [('09:30', '13:00')]),
        ]
        for label, code, day, timezone, sessions in cases:
            with self.subTest(market=label):
                self.feed(code, [(day + ' ' + opening, day + ' ' + closing, 10) for opening, closing in sessions])
                self.feeds[code].iloc[-1, self.feeds[code].columns.get_loc('close')] = 30
                self.feeds[code].iloc[-1, self.feeds[code].columns.get_loc('high')] = 31
                self.moment = datetime.fromisoformat(day + 'T' + sessions[-1][1]).replace(tzinfo=ZoneInfo(timezone))
                rules = [self.rule(label + '_' + str(n), code, timeframe=str(n) + 'h') for n in (1, 2, 3, 4)]
                for rule in rules:
                    rule['data_gateway'] = 'FUTU'
                    rule['conditions']['value'] = 20
                (self.root / 'orders.yaml').write_text(yaml.safe_dump({'orders': rules}))
                main = MainEngine(EventEngine(interval=.05))
                self.mains.append(main)
                gateway = main.add_gateway(FutuGateway, 'FUTU')
                gateway.quote_ctx = Mock()
                gateway.quote_ctx.subscribe.return_value = (0, '')
                notifier = AsyncNotifier(NotificationEngine(self.config))
                engine = main.add_engine(ConditionOrderEngine)
                engine.load(self.config, notifier.notify)
                business = lambda: [item for item in self.received if item['message'].startswith(label + '_')]
                quote = {'code': code, 'data_date': day, 'open_price': 10, 'high_price': 31,
                         'low_price': 9, 'prev_close_price': 10, 'volume': 100, 'last_price': 30}
                before = self.moment - pd.Timedelta(seconds=1)
                quote['data_time'] = before.strftime('%H:%M:%S') + '.999'
                gateway.process_quote(pd.DataFrame([quote]))
                time.sleep(.2)
                self.assertEqual(business(), [])
                quote['data_time'] = self.moment.strftime('%H:%M:%S') + '.000'
                # The closing quote completes the final window for every configured timeframe.
                gateway.process_quote(pd.DataFrame([quote]))
                self.ticks.append({'code': code, 'data_date': day, 'data_time': quote['data_time']})
                self.wait(lambda: len(business()) == 4)
                for item in business():
                    self.assertIn('value=30', item['message'])
                    self.assertIn(self.moment.isoformat(), item['message'])
                for _ in range(5):
                    gateway.process_quote(pd.DataFrame([quote]))
                time.sleep(.1)
                self.assertEqual(len(business()), 4)
                self.artifact('final-quote-' + label)
                main.close()
                self.mains.remove(main)

    def test_tick_boundaries_lunch_and_deferred_close(self):
        self.feed('SH.600000', [('2026-09-28 09:30', '2026-09-28 11:30', 10),
                               ('2026-09-28 13:00', '2026-09-28 14:00', 2),
                               ('2026-09-28 14:00', '2026-09-28 15:00', 30),
                               ('2026-09-29 09:30', '2026-09-29 10:30', 999)])
        rule = self.rule('boundary', 'SH.600000', timeframe='1h')
        rule['conditions']['value'] = 5
        self.moment = datetime(2026, 9, 28, 10, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
        main, engine = self.start([rule], auto_tick=False)
        # Timer activity alone cannot request data or notify.
        main.event_engine.put(Event(EVENT_TIMER))
        time.sleep(.2)
        self.assertEqual(self.fetch_mock.call_count, 0)
        self.assertEqual(self.received, [])
        self.tick()
        time.sleep(.2)
        self.assertEqual(self.fetch_mock.call_count, 0)
        self.moment = self.moment.replace(minute=30)
        self.tick()
        self.wait(lambda: len(self.received) == 1)
        count = self.fetch_mock.call_count
        for _ in range(20):
            self.tick()
        self.moment = self.moment.replace(hour=11, minute=29)
        self.tick()
        time.sleep(.2)
        self.assertEqual(self.fetch_mock.call_count, count)
        # Lunch resets the afternoon anchor: 13:30 is not an hour boundary.
        self.moment = self.moment.replace(hour=11, minute=30)
        self.tick()
        self.wait(lambda: self.fetch_mock.call_count == count + 1)
        self.moment = self.moment.replace(hour=13, minute=30)
        self.tick()
        time.sleep(.2)
        self.assertEqual(self.fetch_mock.call_count, count + 1)
        self.moment = self.moment.replace(hour=14, minute=0)
        self.tick()
        self.wait(lambda: json.loads((self.root / 'state.json').read_text())['notifications']['boundary']['matched'] is False)
        self.moment = self.moment.replace(hour=14, minute=59, second=59)
        self.tick()
        time.sleep(.2)
        self.assertEqual(len(self.received), 1)
        # No closing Tick: next morning checks yesterday's final hour, not today's forming bar.
        self.moment = self.moment.replace(day=29, hour=9, minute=30, second=0)
        self.tick()
        self.wait(lambda: len(self.received) == 2)
        self.assertIn('value=30', self.received[1]['message'])
        self.assertIn('2026-09-28T15:00', self.received[1]['message'])
        self.artifact('tick-boundaries')

    def test_mixed_timeframes_keep_latest_completed_values(self):
        self.moment = datetime(2026, 9, 28, 10, 30, tzinfo=ZoneInfo('America/New_York'))
        self.feed('US.AAPL', [('2026-09-28 09:30', '2026-09-28 12:30', 10),
                             ('2026-09-28 12:30', '2026-09-28 13:30', 30),
                             ('2026-09-28 13:30', '2026-09-28 14:30', 2),
                             ('2026-09-28 14:30', '2026-09-28 15:30', 30)])
        rule = self.rule('mixed', 'US.AAPL', timeframe='1h')
        rule['conditions'] = {'logical_operator': 'AND', 'items': [
            {'variable': 'tools.MA', 'params': {'N': 1}, 'timeframe': tf, 'operator': '>', 'value': 20}
            for tf in ('1h', '4h')]}
        self.start([rule])
        time.sleep(.2)
        self.assertEqual(self.received, [])
        self.moment = self.moment.replace(hour=13)
        self.tick()
        self.wait(lambda: len(self.received) == 1)
        count = self.fetch_mock.call_count
        self.moment = self.moment.replace(hour=14)
        self.tick()
        self.wait(lambda: json.loads((self.root / 'state.json').read_text())['notifications']['mixed']['matched'] is False)
        self.assertEqual(self.fetch_mock.call_count, count + 1)
        self.moment = self.moment.replace(hour=15)
        self.tick()
        self.wait(lambda: len(self.received) == 2)
        self.assertIn('4h', self.received[1]['message'])
        self.assertIn('2026-09-28T13:30', self.received[1]['message'])
        self.artifact('mixed-timeframes')

    def test_sessions_parameters_and_real_delivery(self):
        self.feed('SH.600000', [('2026-09-28 09:30', '2026-09-28 11:30', 10),
                               ('2026-09-28 13:00', '2026-09-28 15:00', 30)])
        self.feed('HK.00700', [('2026-09-28 09:30', '2026-09-28 11:30', 10),
                              ('2026-09-28 11:30', '2026-09-28 12:00', 20),
                              ('2026-09-28 13:00', '2026-09-28 15:00', 30),
                              ('2026-09-28 15:00', '2026-09-28 16:00', 40)])
        rules = [self.rule('cn2', 'SH.600000'), self.rule('cn3', 'SH.600000', timeframe='3h'),
                 self.rule('cn4', 'SH.600000', timeframe='4h'),
                 self.rule('hk2', 'HK.00700'), self.rule('hk_mean', 'HK.00700', params={'N': 2})]
        self.start(rules)
        self.wait(lambda: len(self.received) == 5)
        messages = {p['message'].split(' | ')[0]: p['message'] for p in self.received}
        for key in ('cn2', 'cn3', 'cn4'):
            self.assertIn('value=30', messages[key])
            self.assertIn('13:00', messages[key])
            self.assertIn('15:00', messages[key])
        self.assertIn('value=40', messages['hk2'])
        self.assertIn('value=35', messages['hk_mean'])
        self.assertEqual(len({p['event_id'] for p in self.received}), 5)
        self.artifact('sessions')

    def test_us_dst_early_close_and_unfinished_bar(self):
        self.moment = datetime(2026, 11, 27, 12, 30, tzinfo=ZoneInfo('America/New_York'))
        self.feed('US.AAPL', [('2026-11-27 04:00', '2026-11-27 09:30', 999),
                             ('2026-11-27 09:30', '2026-11-27 11:30', 10),
                             ('2026-11-27 11:30', '2026-11-27 13:00', 30),
                             ('2026-11-27 13:00', '2026-11-27 16:00', 999)])
        self.start([self.rule('two', 'US.AAPL'), self.rule('four', 'US.AAPL', timeframe='4h')])
        self.wait(lambda: len(self.received) == 1)
        self.assertIn('two |', self.received[0]['message'])
        self.assertIn('value=10', self.received[0]['message'])
        self.moment = self.moment.replace(hour=13, minute=0)
        self.tick()
        self.wait(lambda: len(self.received) == 2)
        message = next(p['message'] for p in self.received if p['message'].startswith('four |'))
        self.assertIn('value=30', message)
        self.assertIn('13:00', message)
        self.assertIn('-05:00', message)
        self.artifact('us-early-close')

    def test_enter_missing_data_and_restart(self):
        self.feed('HK.00700', [('2026-09-28 09:30', '2026-09-28 15:00', 10),
                              ('2026-09-28 15:00', '2026-09-28 16:00', 2),
                              ('2026-09-29 09:30', '2026-09-29 11:30', 10)])
        frames = self.feeds['HK.00700'].copy()
        self.moment = datetime(2026, 9, 28, 11, 30, tzinfo=ZoneInfo('Asia/Hong_Kong'))
        rule = self.rule('enter', 'HK.00700')
        rule['conditions']['value'] = 5
        main, engine = self.start([rule])
        messages = lambda: [item for item in self.received if item['message'].startswith('enter |')]
        self.wait(lambda: len(messages()) == 1)
        self.feeds['HK.00700'] = None
        self.moment = self.moment.replace(hour=12)
        self.tick()
        self.wait(lambda: any('K线数据不可用' in item['message'] for item in self.received))
        self.feeds['HK.00700'] = frames
        self.moment = self.moment.replace(hour=15)
        self.tick()
        time.sleep(.2)
        self.assertEqual(len(messages()), 1)
        main.close()
        self.mains.remove(main)
        self.start([rule])
        time.sleep(.2)
        self.assertEqual(len(messages()), 1)
        self.moment = self.moment.replace(hour=16)
        self.tick()
        self.wait(lambda: json.loads((self.root / 'state.json').read_text())['notifications']['enter']['matched'] is False)
        self.moment = self.moment.replace(day=29, hour=11)
        self.tick()
        self.wait(lambda: len(messages()) == 2)
        self.assertNotEqual(messages()[0]['event_id'], messages()[1]['event_id'])
        self.artifact('restart')

    def test_all_indicator_outputs_and_signal_override(self):
        days = pd.bdate_range(end='2026-09-28', periods=80)
        for code, pattern in [('SH.600001', 'constant'), ('SH.600002', 'rising'),
                              ('SH.600003', 'alternating')]:
            sessions = []
            for number, day in enumerate(days):
                date = day.strftime('%Y-%m-%d')
                am, pm = ((10, 10) if pattern == 'constant' else
                          (number * 2 + 10, number * 2 + 11) if pattern == 'rising' else (11, 10))
                sessions.extend([(date + ' 09:30', date + ' 11:30', am),
                                 (date + ' 13:00', date + ' 15:00', pm)])
            self.feed(code, sessions)
        rules = [self.rule('ema', 'SH.600001', variable='tools.EMA', params={'N': 20}),
                 self.rule('rsi', 'SH.600002', variable='tools.RSI', params={'N': 14})]
        for output in ('K', 'D', 'J'):
            rules.append(self.rule('kdj_' + output, 'SH.600001', variable='tools.KDJ',
                                   params={'N': 9, 'M1': 3, 'M2': 3}, output=output))
        for output in ('DIF', 'DEA', 'HIST'):
            rule = self.rule('macd_' + output, 'SH.600001', variable='tools.MACD',
                             params={'fast_period': 5, 'slow_period': 10, 'signal_period': 3}, output=output)
            rule['conditions']['operator'] = '>='
            rules.append(rule)
        signal = self.rule('top_down', 'SH.600003', variable='signal.top_down',
                           params={'oversold': 90})
        from params_db import ParamsDB
        params_path = self.root / 'params.json'
        params_path.write_text(json.dumps({'SH.600003': {
            'best_params': {'rsi_period': 5, 'oversold': 30, 'overbought': 95},
            'meta_info': {'atr_period': 60, 'target_multiplier': 1.5}, 'performance': {}}}))
        db_uri = 'sqlite:///' + str(self.root / 'rsi.db')
        ParamsDB(db_uri, init_db=True).import_params(str(params_path))
        self.config.set('CONFIG', 'RSI_PARAMS_DB', db_uri)
        signal['conditions'].update(operator='contains', value='📈')
        rules.append(signal)
        self.start(rules)
        self.wait(lambda: len(self.received) == 9)
        messages = {p['message'].split(' | ')[0]: p['message'] for p in self.received}
        self.assertIn('value=10', messages['ema'])
        self.assertIn('value=100', messages['rsi'])
        for output in ('K', 'D', 'J'):
            self.assertIn('value=50', messages['kdj_' + output])
        for output in ('DIF', 'DEA', 'HIST'):
            self.assertIn('value=0', messages['macd_' + output])
        self.assertIn("'rsi_period': 5", messages['top_down'])
        self.assertIn('value=rsi', messages['top_down'])
        self.artifact('indicator-outputs')

    def test_once_and_enter_cooldown(self):
        self.feed('HK.00700', [('2026-09-28 09:30', '2026-09-28 11:30', 10),
                              ('2026-09-28 11:30', '2026-09-28 12:00', 2),
                              ('2026-09-28 13:00', '2026-09-28 15:00', 10)])
        self.moment = datetime(2026, 9, 28, 11, 30, tzinfo=ZoneInfo('Asia/Hong_Kong'))
        once = self.rule('once', 'HK.00700')
        once['notification'].update(mode='once')
        enter = self.rule('cooldown', 'HK.00700')
        enter['notification'].update(cooldown_seconds=30)
        for rule in (once, enter):
            rule['conditions']['value'] = 5
        main, _ = self.start([once, enter])
        self.wait(lambda: len(self.received) == 2)
        self.moment = self.moment.replace(hour=12)
        self.tick()
        self.wait(lambda: json.loads((self.root / 'state.json').read_text())['notifications']['cooldown']['matched'] is False)
        self.moment = self.moment.replace(hour=15)
        self.tick()
        self.wait(lambda: json.loads((self.root / 'state.json').read_text())['notifications']['cooldown']['matched'] is True)
        main.close()
        self.mains.remove(main)
        self.start([once, enter])
        time.sleep(.2)
        self.assertEqual(sum(p['message'].startswith('once |') for p in self.received), 1)
        self.assertEqual(sum(p['message'].startswith('cooldown |') for p in self.received), 1)
        self.artifact('modes')

    def test_us_summer_windows_and_hk_half_day(self):
        self.moment = datetime(2026, 9, 28, 16, 1, tzinfo=ZoneInfo('America/New_York'))
        self.feed('US.AAPL', [('2026-09-28 09:30', '2026-09-28 13:30', 10),
                             ('2026-09-28 13:30', '2026-09-28 15:30', 20),
                             ('2026-09-28 15:30', '2026-09-28 16:00', 30)])
        self.start([self.rule('us' + str(n), 'US.AAPL', timeframe=str(n) + 'h') for n in (2, 3, 4)])
        self.wait(lambda: len(self.received) == 3)
        for payload in self.received:
            self.assertIn('value=30', payload['message'])
            self.assertIn('-04:00', payload['message'])
        message4 = next(p['message'] for p in self.received if p['message'].startswith('us4 |'))
        self.assertIn('13:30', message4)
        self.assertIn('16:00', message4)
        self.artifact('us-summer')
        self.moment = datetime(2026, 12, 24, 12, 1, tzinfo=ZoneInfo('Asia/Hong_Kong'))
        self.feed('HK.00700', [('2026-12-24 09:30', '2026-12-24 12:00', 10),
                              ('2026-12-24 13:00', '2026-12-24 16:00', 999)])
        self.start([self.rule('hk_half', 'HK.00700', timeframe='4h')])
        self.wait(lambda: any(p['message'].startswith('hk_half |') for p in self.received))
        message = next(p['message'] for p in self.received if p['message'].startswith('hk_half |'))
        self.assertIn('value=10', message)
        self.assertIn('12:00', message)
        self.artifact('hk-half-day')

    def test_minute_pagination_through_sdk_boundary(self):
        self.feed('SH.600000', [('2026-09-28 09:30', '2026-09-28 11:30', 10),
                               ('2026-09-28 13:00', '2026-09-28 15:00', 30)])
        raw = self.feeds['SH.600000'].reset_index().rename(columns={'index': 'time_key'})
        self.fetch.stop()
        from unittest.mock import Mock
        context = Mock()
        context.request_history_kline.side_effect = [(0, raw.iloc[:120], b'page2'),
                                                     (0, raw.iloc[120:], None)]
        def sdk_context(*args, **kwargs):
            instance = self.calendar_context()
            instance.request_history_kline = context.request_history_kline
            instance.close = context.close
            return instance
        with patch('data.opend_alive', return_value=True), patch('data.ft.OpenQuoteContext', side_effect=sdk_context):
            main, _ = self.start([self.rule('pages', 'SH.600000')])
            self.wait(lambda: len(self.received) == 1)
            main.close()
            self.mains.remove(main)
        self.assertIn('value=30', self.received[0]['message'])
        self.assertIn('13:00', self.received[0]['message'])
        requests = context.request_history_kline.call_args_list
        self.assertIsNone(requests[0].kwargs['page_req_key'])
        self.assertEqual(requests[1].kwargs['page_req_key'], b'page2')
        self.assertGreaterEqual(context.close.call_count, 2)
        self.artifact('minute-pagination')

    def test_native_daily_weekly_boundaries(self):
        self.moment = datetime(2026, 9, 28, 12, 30, tzinfo=ZoneInfo('America/New_York'))
        index = pd.DatetimeIndex(['2026-09-25 04:00Z', '2026-09-28 04:00Z', '2026-10-02 04:00Z'])
        self.feeds['US.AAPL'] = pd.DataFrame({'open': [10, 20, 30], 'close': [10, 20, 30],
                                            'high': [11, 21, 31], 'low': [9, 19, 29], 'volume': [100, 200, 300]}, index=index)
        rules = [self.rule(tf, 'US.AAPL', timeframe=tf) for tf in ('1d', '1w')]
        self.start(rules)
        time.sleep(.2)
        self.assertEqual(self.received, [])
        self.assertEqual(self.fetch_mock.call_count, 0)
        self.moment = self.moment.replace(hour=16, minute=0)
        self.tick()
        self.wait(lambda: len(self.received) == 1)
        self.assertIn('1d |', self.received[0]['message'])
        self.assertIn('value=20', self.received[0]['message'])
        self.moment = self.moment.replace(day=2, month=10)
        self.tick()
        self.wait(lambda: len(self.received) == 2)
        self.assertIn('1w |', self.received[1]['message'])
        self.assertIn('value=30', self.received[1]['message'])
        self.artifact('native-bars')

    def test_repeated_notification_never_repeats_trade_after_restart(self):
        from order_engine.gateway import SimGateway
        self.feed('SH.600000', [('2026-09-28 09:30', '2026-09-28 11:30', 10),
                               ('2026-09-28 13:00', '2026-09-28 15:00', 30),
                               ('2026-09-29 09:30', '2026-09-29 11:30', 10),
                               ('2026-09-29 13:00', '2026-09-29 15:00', 30)])
        order = self.rule('indicator-order', 'SH.600000')
        order.update(data_gateway='FAKE', order_gateway='SIM')
        order['conditions']['value'] = 20
        order['actions'].append({'type': 'limit', 'action': 'buy', 'quantity': 100, 'price': 30})
        (self.root / 'orders.yaml').write_text(yaml.safe_dump({'orders': [order]}))
        def boot():
            main = MainEngine(EventEngine(interval=.05))
            self.mains.append(main)
            gateway = main.add_gateway(FeedGateway, 'FAKE')
            main.add_gateway(SimGateway, 'SIM')
            main.connect({}, 'SIM')
            notifier = AsyncNotifier(NotificationEngine(self.config))
            engine = main.add_engine(ConditionOrderEngine)
            engine.load(self.config, notifier.notify)
            return main, gateway
        def push(gateway):
            gateway.on_tick(TickData(symbol='600000', exchange=Exchange.SSE, gateway_name='FAKE',
                                    datetime=self.moment, last_price=30))
            self.ticks.append({'symbol': '600000', 'time': self.moment.isoformat()})
        def trades():
            path = self.root / 'events.jsonl'
            return [item for item in (json.loads(line) for line in path.read_text().splitlines())
                    if item['kind'] == 'trade'] if path.exists() else []
        messages = lambda: [item for item in self.received if item['message'].startswith('indicator-order |')]
        self.moment = datetime(2026, 9, 28, 15, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
        main, gateway = boot()
        push(gateway)
        self.wait(lambda: len(trades()) == 1 and len(messages()) == 1)
        self.assertEqual((trades()[0]['symbol'], trades()[0]['volume'], trades()[0]['price']), ('600000', 100, 30))
        main.close()
        self.mains.remove(main)
        main, gateway = boot()
        push(gateway)
        time.sleep(.2)
        self.assertEqual((len(trades()), len(messages())), (1, 1))
        self.moment = self.moment.replace(day=29, hour=11, minute=30)
        push(gateway)
        self.wait(lambda: json.loads((self.root / 'state.json').read_text())['notifications']['indicator-order']['matched'] is False)
        self.moment = self.moment.replace(hour=15, minute=0)
        push(gateway)
        self.wait(lambda: len(messages()) == 2)
        for _ in range(5):
            push(gateway)
        time.sleep(.2)
        self.assertEqual(len(trades()), 1)
        self.artifact('indicator-order')

    def test_tick_notifications_once_default_and_on_enter(self):
        once = self.rule('price-once', 'HK.00700')
        once.pop('notification')
        enter = self.rule('price-enter', 'HK.00700')
        for rule in (once, enter):
            rule['conditions'] = {'variable': 'last_price', 'operator': '>', 'value': 20}
        main, engine = self.start([once, enter])
        self.wait(lambda: len(self.received) == 2)
        self.tick(price=10)
        self.wait(lambda: json.loads((self.root / 'state.json').read_text())['notifications']['price-enter']['matched'] is False)
        self.tick(price=30)
        self.wait(lambda: len(self.received) == 3)
        self.assertEqual(sum(item['message'].startswith('price-once |') for item in self.received), 1)
        main.close()
        self.mains.remove(main)
        self.start([once, enter])
        time.sleep(.2)
        self.assertEqual(len(self.received), 3)
        self.assertEqual(self.fetch_mock.call_count, 0)
        self.artifact('tick-notification-modes')

    def test_calendar_holiday_and_short_week(self):
        self.moment = datetime(2026, 9, 30, 16, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.feed('SH.600000', [('2026-09-30 09:30', '2026-09-30 11:30', 10),
                               ('2026-09-30 13:00', '2026-09-30 15:00', 30),
                               ('2026-10-01 09:30', '2026-10-01 15:00', 999)])
        self.feeds['SH.600001'] = pd.DataFrame({'open': [10, 30], 'close': [10, 30],
                                              'high': [11, 31], 'low': [9, 29], 'volume': [100, 200]},
                                             index=pd.to_datetime(['2026-09-21', '2026-09-28']))
        self.start([self.rule('holiday', 'SH.600000'), self.rule('short_week', 'SH.600001', timeframe='1w')])
        self.wait(lambda: len(self.received) == 2)
        for payload in self.received:
            self.assertIn('value=30', payload['message'])
            self.assertIn('2026-09-30T15:00', payload['message'])
        self.assertTrue(all(code == 'SH.000001' for _, _, code in self.calendar_requests))
        self.artifact('calendar-holiday')

    def test_calendar_unavailable_defers_notification(self):
        self.moment = datetime(2026, 9, 28, 12, 1, tzinfo=ZoneInfo('Asia/Hong_Kong'))
        self.calendar_failure = True
        self.feed('HK.00700', [('2026-09-28 09:30', '2026-09-28 12:00', 10)])
        self.start([self.rule('calendar-recovery', 'HK.00700')])
        self.wait(lambda: any('calendar unavailable' in item['message'] for item in self.received))
        self.assertFalse(any(item['message'].startswith('calendar-recovery |') for item in self.received))
        self.calendar_failure = False
        self.tick()
        self.wait(lambda: any(item['message'].startswith('calendar-recovery |') for item in self.received))
        self.artifact('calendar-recovery')

    def test_invalid_config_and_notification_only_gateway_discovery(self):
        base = self.rule('invalid', 'HK.00700')
        for update in ({'params': {'TYPO': 3}}, {'timeframe': '7h'}, {'params': {'N': 0}},
                       {'output': 'J'}, {'value': float('nan')}):
            rule = json.loads(json.dumps(base))
            rule['conditions'].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.start([rule])
        invalid = json.loads(json.dumps(base))
        invalid['notification']['mode'] = 'invalid'
        with self.assertRaisesRegex(ValueError, 'once/on_enter'):
            self.start([invalid])
        invalid = json.loads(json.dumps(base))
        invalid['unsupported_option'] = True
        with self.assertRaisesRegex(ValueError, '未支持字段'):
            self.start([invalid])
        (self.root / 'orders.yaml').write_text(yaml.safe_dump({'orders': [base]}))
        self.assertEqual(collect_order_gateway_names(self.config, 'FUTU', 'QMT'), {'FUTU'})
        (self.root / 'orders.yaml').write_text(yaml.safe_dump({'unsupported_section': []}))
        main = MainEngine(EventEngine())
        self.mains.append(main)
        engine = main.add_engine(ConditionOrderEngine)
        with self.assertRaisesRegex(ValueError, '只支持orders'):
            engine.load(self.config)


if __name__ == '__main__':
    unittest.main()
