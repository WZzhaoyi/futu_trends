"""Sheet-delivery E2E with local HTTP providers and optional live verification."""
from __future__ import annotations

import argparse
import configparser
import json
import multiprocessing
from pathlib import Path
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'market_analysis')]
import notification_engine.engine as owner
from notification_engine import NotificationEngine
from market_analysis import csi_flow_timing as m1
from market_analysis import momentum_rotation_strategy as rotation
from market_analysis import etf_premium_rate as premium


class Provider(BaseHTTPRequestHandler):
    cells = {}
    writes = []
    reject_feishu = False
    reject_google = False

    def log_message(self, *args):
        pass

    def handle_request(self):
        path = unquote(urlsplit(self.path).path)
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
        google = '/v4/' in path
        if google and self.reject_google:
            payload, status = {'error': {'code': 403, 'message': 'rejected'}}, 403
        elif not google and self.reject_feishu:
            payload, status = {'code': 999, 'msg': 'rejected'}, 403
        elif path.endswith('/query'):
            payload, status = {'code': 0, 'data': {'sheets': [{'sheet_id': 'tab', 'title': '26-10'}, {'sheet_id': 'sun', 'title': '26-11'}]}}, 200
        elif google and '/values/' not in path:
            payload, status = {'sheets': [{'properties': {'title': '26-10'}}, {'properties': {'title': '26-11'}}]}, 200
        else:
            key = ('google' if google else 'feishu', path.rsplit('/values/', 1)[-1] if self.command == 'GET' or google else body['valueRange']['range'])
            if self.command == 'GET':
                time.sleep(0.04)  # expose unprotected read-modify-write races
                values = [[self.cells.get(key, '线上原文\n保持原样')]]
                payload = {'values': values} if google else {'code': 0, 'data': {'valueRange': {'values': values}}}
            else:
                values = body['values'] if google else body['valueRange']['values']
                self.cells[key] = values[0][0]
                self.writes.append({'provider': key[0], 'cell': key[1], 'content': values[0][0]})
                payload = {'updatedCells': 1} if google else {'code': 0, 'data': {}}
            status = 200
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_PUT = do_POST = handle_request


def local_engine(endpoint):
    import httplib2
    import requests
    from googleapiclient.discovery import build_from_document
    from googleapiclient.discovery_cache import get_static_doc
    config = configparser.ConfigParser()
    config.read(ROOT / 'config_template.ini')
    config['CONFIG'].update(GOOGLE_SHEET_ID='e2e', GOOGLE_API_JSON=__file__, PROXY='', FEISHU_SPREADSHEET_TOKEN='e2e', FEISHU_TENANT_ACCESS_TOKEN='fake')
    engine = NotificationEngine(config)
    request = requests.Session().request
    engine.SESSION.request = lambda method, url, **kw: request(method, endpoint + urlsplit(url).path, **kw)
    doc = json.loads(get_static_doc('sheets', 'v4'))
    doc['rootUrl'] = endpoint + '/'
    def build(*args, **kwargs):
        return build_from_document(json.dumps(doc), http=httplib2.Http(timeout=10))
    # Substitute only provider endpoint/authentication; real Google request encoding.
    return engine, patch.object(owner, 'build', build), patch.object(owner.Credentials, 'from_service_account_file', return_value=object()), patch.object(owner, 'AuthorizedHttp', side_effect=lambda creds, http: http)


def writer(endpoint, index):
    engine, sdk, auth, http = local_engine(endpoint)
    with sdk, auth, http:
        for send in (engine.send_google_sheet_message, engine.send_feishu_sheet_message):
            assert send(f'并发信号{index}', sheet_date=date(2026, 10, 13))
            assert send(f'并发信号{index}', sheet_date=date(2026, 10, 13))


def events():
    stamp = '2026-10-13T23:59:59-04:00'
    return [
        (m1.LiveEventNotifier, {'type': 'SIGNAL', 'action': 'BUY', 'symbol': 'SH.000906', 'bar_key': '2026-10-13 14:45:00', 'feature': {}, 'threshold': {}, 'strength': {'ranking': [{'symbol': 'SH.510500', 'score': .03}]}, 'emitted_at': stamp}),
        (rotation.LiveNotifier, {'type': 'SIGNAL', 'action': 'ROTATE', 'market': 'US', 'leg': 'US-A', 'previous_symbol': 'US.SPY', 'target_symbol': 'US.QQQ', 'selected_symbol': 'US.QQQ', 'evaluation_date': '2026-10-13', 'timezone': 'America/New_York', 'ranking': [{'symbol': 'US.QQQ', 'window': 3, 'score': .12}], 'emitted_at': stamp}),
        (premium.LiveNotifier, {'type': 'SIGNAL', 'action': 'BUY', 'symbol': 'SZ.159941', 'price': 1.2345, 'unit_nav': 1.25, 'nav_date': '2026-10-12', 'nav_age_days': 1, 't1_premium': -.0124, 'iopv_premium': .0234, 'buy_threshold': -.01, 'sell_threshold': .01, 'target_position': 1., 'emitted_at': stamp}),
    ]


def offline(output):
    from tests.offline_guard import offline as network_guard
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f'http://127.0.0.1:{server.server_port}'
    try:
        with network_guard() as blocked:
            ctx = multiprocessing.get_context('spawn')
            processes = [ctx.Process(target=writer, args=(endpoint, i)) for i in range(4)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(45)
                if process.is_alive():
                    process.terminate()
                    raise AssertionError('sheet writer timed out')
                assert process.exitcode == 0
            assert len(Provider.cells) == 2
            for content in Provider.cells.values():
                assert content.startswith('线上原文\n保持原样\n\n')
                for i in range(4):
                    assert content.count(f'并发信号{i}') == 1
            engine, sdk, auth, http = local_engine(endpoint)
            for channel in ('send_webhook', 'send_telegram_message', 'send_email'):
                setattr(engine, channel, lambda *args: None)
            with sdk, auth, http:
                for notifier_type, event in events():
                    for _ in range(2):
                        notifier = notifier_type(engine)
                        notifier.notify(event)
                        notifier.notify({**event, 'action': 'NONE'})
                        notifier.notify({'type': 'ERROR', 'message': 'test error', 'symbol': event.get('symbol', '')})
                        notifier.close(30)
                assert not engine.send_google_sheet_message('   ')
                assert not engine.send_feishu_sheet_message('')
                assert engine.send_google_sheet_message('周日月初', sheet_date=date(2026, 11, 1))
                assert engine.send_feishu_sheet_message('周日月初', sheet_date=date(2026, 11, 1))
                notifier = premium.LiveNotifier(engine)
                notifier.notify({**events()[2][1], 'action': 'SELL', 'iopv_premium': None, 'emitted_at': '2026-10-13T23:59:57-04:00'})
                notifier.close(30)
                for action, leg, previous, target, seconds in (
                    ('INITIAL', 'US-B', None, 'US.QQQ', '55'),
                    ('BUY', 'CN-A', None, 'SH.510300', '54'),
                    ('SELL', 'CN-B', 'SZ.159941', None, '53'),
                ):
                    notifier = rotation.LiveNotifier(engine)
                    notifier.notify({**events()[1][1], 'action': action, 'leg': leg,
                                     'previous_symbol': previous, 'target_symbol': target,
                                     'emitted_at': f'2026-10-13T23:59:{seconds}-04:00'})
                    notifier.close(30)
                notifier = m1.LiveEventNotifier(engine, notify_lifecycle=True)
                notifier.notify({**events()[0][1], 'action': 'SELL', 'emitted_at': '2026-10-13T23:59:52-04:00'})
                notifier.notify({'type': 'READY', 'symbol': 'SH.000906', 'position': 0,
                                 'emitted_at': '2026-10-13T23:59:51-04:00'})
                notifier.close(30)
                Provider.reject_google = True
                notifier = premium.LiveNotifier(engine)
                notifier.notify({**events()[2][1], 'action': 'SELL', 'emitted_at': '2026-10-13T23:59:56-04:00'})
                notifier.close(30)
                Provider.reject_google = False
                Provider.reject_feishu = True
                notifier = premium.LiveNotifier(engine)
                notifier.notify({**events()[2][1], 'action': 'SELL', 'emitted_at': '2026-10-13T23:59:58-04:00'})
                notifier.close(30)
                Provider.reject_feishu = False
            for provider, cell in [('google', '26-10!D5'), ('feishu', 'tab!D5:D5')]:
                content = Provider.cells[(provider, cell)]
                for label in ('M1', '轮动', 'ETF溢价'):
                    lines = [line for line in content.splitlines() if label in line and line.startswith('23:59:59 ')]
                    assert len(lines) == 1, (provider, label, lines)
                    assert lines[0].startswith('23:59:59 ')
                assert '23:59:59 轮动 US-A ROTATE US.SPY→US.QQQ' in content
                assert 'US/US-A' not in content
                assert '轮动 US-B INITIAL CASH→US.QQQ' in content
                assert '轮动 CN-A BUY CASH→SH.510300' in content
                assert '轮动 CN-B SELL SZ.159941→CASH' in content
                assert '23:59:52 M1 SH.000906 SELL' in content
                assert 'READY' not in content
                assert ('23:59:56 ETF溢价' in content) == (provider == 'feishu')
                assert ('23:59:58 ETF溢价' in content) == (provider == 'google')
                assert '23:59:59 ETF溢价 SZ.159941 BUY 实时溢价2.34% T-1溢价-1.24%' in content
                assert '23:59:57 ETF溢价 SZ.159941 SELL 实时溢价不可用 T-1溢价-1.24%' in content
                assert '仓位' not in content and '1.2345' not in content and '1.25' not in content
                assert 'NONE' not in content and 'test error' not in content
                assert content.startswith('线上原文\n保持原样')
            assert Provider.cells[('google', '26-11!B3')] == '线上原文\n保持原样\n\n周日月初'
            assert Provider.cells[('feishu', 'sun!B3:B3')] == '线上原文\n保持原样\n\n周日月初'
            assert blocked == []
        output.write_text(json.dumps({'mode': 'local HTTP E2E', 'writes': Provider.writes, 'final_cells': [{'provider': provider, 'cell': cell, 'content': content} for (provider, cell), content in Provider.cells.items()], 'blocked_connections': blocked}, ensure_ascii=False, indent=2))
    finally:
        server.shutdown()
        server.server_close()


def live(config_path, output):
    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')
    engine = NotificationEngine(config)
    day = date.today()
    position = owner._calendar_sheet_position(engine.google_sheet_cell_origin, day)
    google_cell = f"{position['sheet_name']}!{position['target_col']}{position['target_row']}"
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    creds = owner.Credentials.from_service_account_file(engine.google_api_json, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    client = httplib2.Http(timeout=30)
    if engine.PROXIES.get('http'):
        proxy = urlsplit(engine.PROXIES['http'])
        client = httplib2.Http(timeout=30, proxy_info=httplib2.ProxyInfo(httplib2.socks.PROXY_TYPE_HTTP, proxy.hostname, proxy.port))
    service = build('sheets', 'v4', http=AuthorizedHttp(creds, client))
    def google_read():
        values = service.spreadsheets().values().get(spreadsheetId=engine.google_sheet_id, range=google_cell).execute().get('values', [])
        return str(values[0][0]) if values and values[0] else ''
    fp = owner._calendar_sheet_position(engine.feishu_sheet_cell_origin, day)
    sheet_id = next(s['sheet_id'] for s in engine._get_feishu_sheets() if s['title'] == fp['sheet_name'])
    feishu_cell = f"{sheet_id}!{fp['target_col']}{fp['target_row']}:{fp['target_col']}{fp['target_row']}"
    output.touch(mode=0o600, exist_ok=True)
    output.chmod(0o600)
    before = {'google': google_read(), 'feishu': engine._read_feishu_value(feishu_cell)}
    marker = f'{day} 表格接入验证（E2E，非交易信号）'
    artifact = {'mode': 'live append-only', 'date': str(day), 'google_cell': google_cell, 'feishu_cell': feishu_cell.split('!', 1)[1], 'marker': marker, 'before': before}
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))
    for send in (engine.send_google_sheet_message, engine.send_feishu_sheet_message):
        assert send(marker, sheet_date=day)
        assert send(marker, sheet_date=day)
    after = {'google': google_read(), 'feishu': engine._read_feishu_value(feishu_cell)}
    artifact['after'] = after
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))
    for provider in before:
        assert after[provider].startswith(before[provider]), f'{provider}: original content changed'
        assert after[provider].split('\n\n').count(marker) == 1, f'{provider}: duplicate marker'
    artifact['verified'] = True
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-config', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    (live(args.live_config, args.output) if args.live_config else offline(args.output))
    print(f'PASS: {args.output}')
