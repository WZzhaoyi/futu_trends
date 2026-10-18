"""Compact signal-only sheet records; use the trigger timestamp's literal date."""

from datetime import datetime
import logging


def queued_sheet_event(event):
    return {'emitted_at': datetime.now().isoformat(timespec='seconds'), **event}


def send_sheet_signal(engine, strategy, event):
    if event.get('type') != 'SIGNAL':
        return
    action = event.get('action')
    allowed = {'BUY', 'SELL', 'ROTATE', 'INITIAL'} if strategy == 'rotation' else {'BUY', 'SELL'}
    if action not in allowed:
        return
    stamp = datetime.fromisoformat(event['emitted_at'])
    if strategy == 'rotation':
        target = event.get('target_symbol') or 'CASH'
        previous = event.get('previous_symbol') or 'CASH'
        if not event.get('market') or not event.get('leg'):
            return
        summary = f"轮动 {event['leg']} {action} {previous}→{target}"
    elif strategy == 'm3':
        if not event.get('symbol'):
            return
        reason = str(event.get('reason', '')).replace('（', '(').replace('）', ')')
        summary = (f"{event.get('tag', 'M3')} {event['symbol']} {'进场' if action == 'BUY' else '离场'} "
                   f"{event.get('position_text', '')} {reason}")
        ranking = (event.get('strength') or {}).get('ranking') or []
        if action == 'BUY' and ranking and ranking[0].get('symbol'):
            summary += f" 首选{ranking[0]['symbol']}"
    else:
        if not event.get('symbol'):
            return
        premium = event.get("iopv_premium")
        premium_text = "实时溢价不可用" if premium is None else f"实时溢价{premium:.2%}"
        summary = (f"ETF溢价 {event['symbol']} {action} {premium_text} "
                   f"T-1溢价{event['t1_premium']:.2%}")
    message = f"{stamp:%H:%M:%S} {' '.join(summary.split())}"
    for send in (engine.send_google_sheet_message, engine.send_feishu_sheet_message):
        try:
            send(message, sheet_date=stamp.date())
        except Exception as exc:
            logging.getLogger(__name__).error("策略表格通知失败: %s", type(exc).__name__)
