import configparser
import hashlib
import json
import numbers
import math
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields as dataclass_fields
import yaml
from copy import copy
from datetime import datetime, timedelta

from order_engine.event_engine import Event, EVENT_LOG, EVENT_ORDER, EVENT_TICK, EVENT_TRADE
from order_engine.core import MainEngine, BaseEngine
from order_engine.models import (
    TickData, OrderRequest, SubscribeRequest, CancelRequest, LogData, OrderData, TradeData,
    Exchange, Direction, Offset, OrderType, Status,
)
from order_engine.indicators import calculate, validate_indicator, timeframe_of, period_ends
from market_analysis.trading_calendar import market_of, trading_sessions
from trends import is_balance, is_breakout, is_continue, is_reverse, is_top_down

EXCHANGE_MAP = {
    "SH": Exchange.SSE,
    "SZ": Exchange.SZSE,
    "HK": Exchange.SEHK,
    "US": Exchange.SMART,
}

DIRECTION_MAP = {
    "buy": Direction.LONG,
    "sell": Direction.SHORT,
}

ORDER_TYPE_MAP = {
    "limit": OrderType.LIMIT,
    "market": OrderType.MARKET,
}

ORDER_CONFIG_FIELDS = {
    "id", "enabled", "description", "symbol", "exchange",
    "data_gateway", "order_gateway", "conditions", "actions", "cancel", "notification",
}

CANCEL_CONFIG_FIELDS = {"enabled", "conditions"}
GROUP_CONDITION_FIELDS = {"logical_operator", "operator", "items", "conditions"}
LEAF_CONDITION_FIELDS = {"variable", "operator", "value", "params", "timeframe", "output"}
EVENT_INDICATOR = "eIndicator"
NOTIFY_ACTION_FIELDS = {"type", "message"}
ORDER_ACTION_FIELDS = {"type", "action", "quantity", "price", "order_gateway"}
TERMINAL_ORDER_STATUSES = {Status.ALLTRADED, Status.CANCELLED, Status.REJECTED}
SIGNAL_FUNCTIONS = {
    "breakout": is_breakout,
    "continue": is_continue,
    "reverse": is_reverse,
    "top_down": is_top_down,
    "balance": is_balance,
}
DATETIME_VALUE_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}$')
# 富途行情无真实涨跌停数据（仅price_spread最小价差），禁止作为条件变量
UNRELIABLE_TICK_VARIABLES = {'limit_up', 'limit_down'}
TICK_CONDITION_VARIABLES = {f.name for f in dataclass_fields(TickData)} - UNRELIABLE_TICK_VARIABLES
NUMERIC_TICK_VARIABLES = {
    f.name for f in dataclass_fields(TickData) if f.type in (float, 'float', int, 'int')
} - UNRELIABLE_TICK_VARIABLES
# 对齐IBKR条件单语义：连续值（价格/成交量/时间）只有方向比较，无相等概念；
# 字符串值（信号消息等）只有相等/包含语义
DIRECTIONAL_OPERATORS = {'>', '<', '>=', '<='}
EQUALITY_OPERATORS = {'==', '!=', 'contains'}
# 撤单重试策略：提交失败或订单未进终态时最多重试次数与间隔
CANCEL_MAX_ATTEMPTS = 3
CANCEL_RETRY_INTERVAL = 10


class ConditionOrderEngine(BaseEngine):
    """条件单引擎"""

    def __init__(self, main_engine: MainEngine, event_engine, engine_name='ConditionOrder'):
        super().__init__(main_engine, event_engine, engine_name)
        self.active_orders = {}
        self.managed_orders = {}
        self.order_snapshots = {}
        self.order_history_path = None
        self.state_file_path = None
        self.triggered_state = {}
        self.terminal_orders = {}
        self.data_gateway_name = "FUTU"
        self.order_gateway_name = "QMT"
        self.config = None
        self.notification_state = {}
        self._last_ticks = {}
        self._session_cache = {}
        self.indicator_cache = {}
        self._indicator_pending = set()
        self._closed = False
        self._signal_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="signal")

    def write_log(self, msg: str, source: str = "") -> None:
        log = LogData(msg=msg, gateway_name=source)
        event = Event(EVENT_LOG, log)
        self.event_engine.put(event)

    def load(self, config: configparser.ConfigParser, notify_calc: Callable[[str], None] = None):
        """加载配置"""
        self.yaml_config_path = config.get("CONFIG", "ORDER_CONFIG", fallback=None)
        if not self.yaml_config_path:
            raise ValueError("ORDER_CONFIG未配置")
        self.order_history_path = config.get("CONFIG", "ORDER_EVENT_LOG", fallback="order_events.jsonl").strip()
        self.state_file_path = config.get("CONFIG", "ORDER_STATE_FILE", fallback="order_state.json").strip()
        self.data_gateway_name = config.get("CONFIG", "ORDER_DATA_GATEWAY", fallback="FUTU").strip().upper()
        self.order_gateway_name = config.get("CONFIG", "ORDER_TRADE_GATEWAY", fallback="QMT").strip().upper()
        self.config = config
        for path in (self.order_history_path, self.state_file_path):
            if path:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        if os.path.exists(self.yaml_config_path):
            self.event_engine.register(EVENT_TICK, self.process_tick)
            self.event_engine.register(EVENT_ORDER, self.process_order_event)
            self.event_engine.register(EVENT_TRADE, self.process_trade_event)
            if callable(notify_calc):
                # 注册LOG事件回调
                def process_log(event: Event):
                    log: LogData = event.data
                    notify_calc(log.msg)
                self.event_engine.register(EVENT_LOG, process_log)

            # 加载条件单
            with open(self.yaml_config_path, 'r', encoding='utf-8') as f:
                yaml_config = yaml.safe_load(f) or {}
            if not isinstance(yaml_config, dict) or set(yaml_config) - {'orders'}:
                raise ValueError('ORDER_CONFIG顶层只支持orders')
            orders = yaml_config.get('orders', [])
            if not isinstance(orders, list):
                raise ValueError("ORDER_CONFIG中的orders必须是列表")

            state = self._load_state()
            self.triggered_state = state.get("triggered", {})
            self.notification_state = state.get("notifications", {})
            # 终态订单归档只在当日有意义（QMT只返回当日委托），非当日条目清理
            today = datetime.now().strftime("%Y-%m-%d")
            self.terminal_orders = {
                vt: info for vt, info in state.get("terminal_orders", {}).items()
                if isinstance(info, dict) and info.get("date") == today
            }
            seen_order_ids = set()
            for order_config in orders:
                self._validate_order_config(order_config)
                order_id = order_config['id']
                if order_id in seen_order_ids:
                    raise ValueError(f"条件单id重复: {order_id}")
                seen_order_ids.add(order_id)
                if not order_config.get('enabled', False):
                    continue

                # 按配置指纹恢复通知与一次性交易状态，防止重启后重复执行。
                triggered = self.triggered_state.get(order_id)
                config_hash = self._order_config_hash(order_config)
                repeating = order_config.get('notification', {}).get('mode', 'once') == 'on_enter'
                if triggered and triggered.get('config_hash') == config_hash and not repeating:
                    if any(action['type'] != 'notify' for action in order_config['actions']):
                        self.write_log(
                            f"条件单 {order_id} 已于 {triggered.get('triggered_at')} 触发过，跳过装载"
                            f"（修改该条件单配置或删除状态文件条目可重新激活）"
                        )
                    continue
                if triggered and triggered.get('config_hash') != config_hash:
                    del self.triggered_state[order_id]
                    self._save_state()
                if any(action['type'] == 'notify' for action in order_config['actions']):
                    previous = self.notification_state.get(order_id, {})
                    if previous.get('config_hash') != config_hash:
                        self.notification_state[order_id] = {'config_hash': config_hash, 'matched': False,
                                                             'last_sent': None, 'fired': False, 'sequence': 0}
                    if not repeating and self.notification_state[order_id]['fired']:
                        continue
                self.active_orders[order_id] = order_config
            self._save_state()

            # 校验网关引用，未注册的网关会导致订阅/下单静默失败
            self._validate_gateway_references()

            # 订阅条件单行情源，默认FUTU；单个条件单可用data_gateway覆盖
            symbols_to_subscribe = set()
            for order in self.active_orders.values():
                req = SubscribeRequest(symbol=order['symbol'], exchange=EXCHANGE_MAP[order['exchange']])
                data_gateway = str(order.get("data_gateway", self.data_gateway_name)).upper()
                symbols_to_subscribe.add((req.symbol, req.exchange, data_gateway))
            for order in self._orders_with_cancel():
                req = SubscribeRequest(symbol=order['symbol'], exchange=EXCHANGE_MAP[order['exchange']])
                data_gateway = str(order.get("data_gateway", self.data_gateway_name)).upper()
                symbols_to_subscribe.add((req.symbol, req.exchange, data_gateway))

            for symbol, exchange, data_gateway in symbols_to_subscribe:
                req = SubscribeRequest(symbol=symbol, exchange=exchange)
                if not self.main_engine.subscribe(req, data_gateway):
                    raise ValueError(
                        f"行情订阅失败: {symbol}.{exchange.value} @ {data_gateway}，"
                        f"条件单无法生效（检查网关连接、订阅额度，或该网关不提供行情）"
                    )

            self.event_engine.register(EVENT_INDICATOR, self.process_indicator)

        else:
            self.write_log(f"yaml配置不存在: {self.yaml_config_path}")
            raise Exception(f"yaml配置不存在: {self.yaml_config_path}")

    def process_tick(self, event: Event):
        tick: TickData = event.data
        # 集合竞价前/坏数据的tick价格为0，不参与任何条件评估，防止止损类条件误触发
        if tick.last_price is None or tick.last_price <= 0:
            return
        tick = copy(tick)
        if tick.datetime.tzinfo is None:
            market = 'US' if tick.exchange == Exchange.SMART else 'HK' if tick.exchange == Exchange.SEHK else 'CN'
            tick.datetime = tick.datetime.replace(tzinfo=market_of(market).timezone)
        previous = self._last_ticks.get((tick.symbol, tick.exchange, tick.gateway_name.upper()))
        if previous and tick.datetime < previous.datetime:
            return
        self._last_ticks[(tick.symbol, tick.exchange, tick.gateway_name.upper())] = copy(tick)
        for order_id, order in list(self.active_orders.items()):
            if not self._tick_matches(order, tick):
                continue
            matched = self._check_condition(order, tick)
            if matched is None:
                continue
            if not self._process_notification(order, tick, matched):
                continue
            if matched:
                # Notification repetition never rearms trading actions, including after restart.
                if any(action['type'] != 'notify' for action in order['actions']) and order_id not in self.triggered_state:
                    self.write_log(f"条件单 '{order.get('description', order_id)}' 触发")
                    if not self._mark_triggered(order):
                        continue
                    self._execute_actions(order)
                if order.get('notification', {}).get('mode', 'once') == 'once':
                    del self.active_orders[order_id]
        self._process_cancel_conditions(tick)

    def _check_condition(self, order: dict, tick: TickData):
        return self._evaluate_condition_node(order, order['conditions'], tick)

    def _evaluate_condition_node(self, order: dict, node: dict, tick: TickData) -> bool:
        if not isinstance(node, dict):
            self.write_log(f"条件单 {order['id']} 条件节点必须是对象")
            return False

        items = node.get('items', node.get('conditions'))
        if items is not None:
            if not isinstance(items, list) or not items:
                self.write_log(f"条件单 {order['id']} 条件组items必须是非空列表")
                return False

            op = str(node.get('logical_operator', node.get('operator', 'AND'))).upper()
            if op not in ('AND', 'OR'):
                self.write_log(f"条件单 {order['id']} 不支持的逻辑操作符: {op}")
                return False

            results = [self._evaluate_condition_node(order, item, tick) for item in items]
            if any(result is None for result in results):
                return None
            return all(results) if op == 'AND' else any(results)

        return self._evaluate_leaf_condition(order, node, tick)

    def _evaluate_leaf_condition(self, order: dict, cond: dict, tick: TickData) -> bool:
        variable = cond.get('variable')
        operator = cond.get('operator')

        if not variable:
            self.write_log(f"条件单 {order['id']} 条件缺少variable")
            return False
        if operator not in ('>', '<', '>=', '<=', '==', '!=', 'contains'):
            self.write_log(f"条件单 {order['id']} {variable} 不支持的比较符: {operator}")
            return False
        if 'value' not in cond:
            self.write_log(f"条件单 {order['id']} {variable} 缺少value")
            return False

        var_value = self._resolve_variable_value(order, tick, variable, cond)
        if var_value is None:
            return None

        target_value = cond['value']
        try:
            var_value, target_value = self._normalize_compare_values(var_value, target_value)
            if operator == '>':   return var_value > target_value
            if operator == '<':   return var_value < target_value
            if operator == '>=':  return var_value >= target_value
            if operator == '<=':  return var_value <= target_value
            if operator == '==':  return var_value == target_value
            if operator == '!=':  return var_value != target_value
            if operator == 'contains': return str(target_value) in str(var_value)
        except Exception as e:
            self.write_log(f"条件单 {order['id']} {variable} 比较异常: {e}")
            return False

        return False

    def _execute_actions(self, order):
        """执行动作。条件单为一次性触发，动作失败只通知、不重试。"""
        for action in order['actions']:
            action_type = str(action.get('type', '')).lower()
            if action_type == 'notify':
                continue

            try:
                req = OrderRequest(
                    symbol=order['symbol'],
                    exchange=EXCHANGE_MAP[order['exchange']],
                    direction=DIRECTION_MAP[str(action['action']).lower()],
                    type=ORDER_TYPE_MAP[action_type],
                    volume=float(action['quantity']),
                    price=float(action.get('price', 0)),
                    offset=Offset.OPEN
                )
            except Exception as e:
                self.write_log(f"条件单 {order['id']} 动作配置异常，跳过该动作（不重试）: {e}")
                continue

            order_gateway = str(
                action.get("order_gateway", order.get("order_gateway", self.order_gateway_name))
            ).upper()
            vt_orderid = self.main_engine.send_order(req, order_gateway)
            if vt_orderid:
                self._register_managed_order(order, vt_orderid, order_gateway)
                self.write_log(f"条件单 {order['id']} 已提交订单: {vt_orderid}")
            else:
                self.write_log(
                    f"条件单 {order['id']} 下单失败（一次性触发，不重试，请人工处理）: "
                    f"{order['symbol']} {action}"
                )

    def process_order_event(self, event: Event):
        order: OrderData = event.data
        if order.vt_orderid in self.terminal_orders:
            # 已归档终态订单，轮询/重启的重复推送直接忽略
            return

        previous = self.order_snapshots.get(order.vt_orderid)
        changed = (
            previous is None
            or previous.status != order.status
            or previous.traded != order.traded
            or previous.raw_status != order.raw_status
        )
        if not changed:
            return

        self.order_snapshots[order.vt_orderid] = copy(order)
        managed_order = self.managed_orders.get(order.vt_orderid)
        if managed_order:
            managed_order["latest_order"] = copy(order)
        self._record_history("order", order)
        self.write_log(self._format_order_message(order))
        if order.status in TERMINAL_ORDER_STATUSES:
            self._archive_terminal_order(order)

    def process_trade_event(self, event: Event):
        trade: TradeData = event.data
        self._record_history("trade", trade)
        self.write_log(
            f"成交回报: order={trade.orderid} trade={trade.tradeid} "
            f"{trade.symbol}.{trade.exchange.value} {trade.direction.value} "
            f"price={trade.price} volume={trade.volume}"
        )

    def _normalize_compare_values(self, var_value, target_value):
        # datetime统一转为时间戳比较，配置格式示例: 2026-06-13 10:30:00 +0800
        if isinstance(var_value, datetime):
            target_text = str(target_value)
            if not DATETIME_VALUE_RE.match(target_text):
                raise ValueError(f"时间格式异常: {target_text}")
            return var_value.timestamp(), datetime.strptime(target_text, '%Y-%m-%d %H:%M:%S %z').timestamp()

        if isinstance(var_value, numbers.Real) and isinstance(target_value, str):
            target_value = float(target_value)

        return var_value, target_value

    def _tick_matches(self, rule, tick):
        gateway = str(rule.get('data_gateway', self.data_gateway_name)).upper()
        return (tick.symbol == rule['symbol'] and tick.exchange == EXCHANGE_MAP[rule['exchange']]
                and (not tick.gateway_name or tick.gateway_name.upper() == gateway))

    def _resolve_variable_value(self, order, tick, variable, cond=None):
        if variable.startswith(('tools.', 'signal.')):
            return self._indicator_value(order, cond, tick)
        return getattr(tick, variable, None)

    def _validate_notification_config(self, order):
        options = order['notification']
        if not isinstance(options, dict) or set(options) - {'mode', 'cooldown_seconds'}:
            raise ValueError('notification只支持mode/cooldown_seconds')
        if options.get('mode', 'once') not in ('once', 'on_enter'):
            raise ValueError('notification.mode必须是once/on_enter')
        cooldown = options.get('cooldown_seconds', 60)
        if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or not math.isfinite(cooldown) or cooldown < 0:
            raise ValueError('cooldown_seconds必须是>=0的有限数值')
        if not any(action['type'] == 'notify' for action in order['actions']):
            raise ValueError('notification需要至少一个notify动作')

    def _indicator_key(self, rule, node):
        return (rule['exchange'], rule['symbol'], json.dumps({
            'node': {key: node[key] for key in ('variable', 'params', 'output') if key in node},
            'timeframe': timeframe_of(node, self.config),
        }, sort_keys=True))

    def _indicator_value(self, rule, node, tick):
        key = self._indicator_key(rule, node)
        cached = self.indicator_cache.get(key)
        if cached and cached['next_end'] is not None and tick.datetime.timestamp() < cached['next_end']:
            return cached['result']['value'] if cached['result'] is not None else None
        if key not in self._indicator_pending and not self._closed:
            self._indicator_pending.add(key)
            self._signal_executor.submit(self._refresh_indicator, key, rule, node, copy(tick), cached)
        return None

    def _refresh_indicator(self, key, rule, node, tick, cached):
        result, error, end, next_end = None, None, None, None
        try:
            market = market_of('CN' if rule['exchange'] in ('SH', 'SZ') else rule['exchange'])
            now = tick.datetime
            now = now.replace(tzinfo=market.timezone) if now.tzinfo is None else now.astimezone(market.timezone)
            # Only the worker accesses this shared calendar cache; no network work on the event thread.
            saved = self._session_cache.get(market.name)
            if saved is None or saved[0] != now.date():
                dates = [now.date() + timedelta(days=i) for i in range(-14, 15)]
                saved = (now.date(), trading_sessions(market, dates, config=self.config))
                self._session_cache[market.name] = saved
            ends = period_ends(saved[1], timeframe_of(node, self.config))
            completed = [closing for closing in ends if closing <= now]
            target = completed[-1] if completed else None
            next_end = next((closing.timestamp() for closing in ends if closing > now), None)
            end = target.isoformat() if target else None
            if cached and cached['end'] == end:
                result, error = cached['result'], cached['error']
            elif target and (cached is not None or target.date() == now.date()):
                result = calculate(node, f"{rule['exchange']}.{rule['symbol']}", self.config, now,
                                   SIGNAL_FUNCTIONS.get(node['variable'].split('.', 1)[1]))
                if result['bar_end'] != end:
                    raise ValueError(f'目标周期{end}的K线尚不可用')
        except Exception as exc:
            result, error = None, str(exc)
        if not self._closed:
            self.event_engine.put(Event(EVENT_INDICATOR, (key, {
                'end': end, 'next_end': next_end, 'result': result, 'error': error})))

    def process_indicator(self, event):
        key, cached = event.data
        previous = self.indicator_cache.get(key)
        self.indicator_cache[key] = cached
        self._indicator_pending.discard(key)
        error = cached['error']
        if error and (previous is None or previous.get('error') != error):
            self.write_log(f'指标数据不可用 {key[0]}.{key[1]}: {error}')
        if cached['result'] is not None:
            # Complete the check authorized by the latest Tick, including orders, on the event thread.
            for tick in list(self._last_ticks.values()):
                if tick.symbol == key[1] and tick.exchange == EXCHANGE_MAP[key[0]]:
                    self.process_tick(Event(EVENT_TICK, tick))

    def _process_notification(self, order, tick, matched):
        actions = [action for action in order['actions'] if action['type'] == 'notify']
        if not actions:
            return True
        state = self.notification_state[order['id']]
        options = order.get('notification', {})
        if options.get('mode', 'once') == 'once' and state['fired']:
            return True
        entered = matched and not state['matched']
        now = time.time()
        send = entered and (
            state['last_sent'] is None or now - state['last_sent'] >= options.get('cooldown_seconds', 60))
        if matched == state['matched'] and not send:
            return True
        previous = dict(state)
        state['matched'] = matched
        if send:
            state['last_sent'] = now
            state['fired'] = True
            state['sequence'] += 1
        if not self._save_state():
            self.notification_state[order['id']] = previous
            return False
        if send:
            details = []
            def describe(node):
                children = node.get('items', node.get('conditions'))
                if children is not None:
                    for child in children:
                        describe(child)
                    return
                if node['variable'].startswith(('tools.', 'signal.')):
                    result = self.indicator_cache[self._indicator_key(order, node)]['result']
                    text = (f"{node['variable']} {node.get('output', '')} {result['timeframe']} "
                            f"params={result['params']} value={result['value']} "
                            f"{node['operator']} {node['value']} {result['bar_start']}~{result['bar_end']}")
                else:
                    text = f"{node['variable']} value={getattr(tick, node['variable'])} {node['operator']} {node['value']}"
                details.append(text)
            describe(order['conditions'])
            for index, action in enumerate(actions):
                message = (f"{action.get('message') or order['id']} | {order['exchange']}.{order['symbol']} | "
                           + ' | '.join(details) + f" | event={order['id']}:{state['sequence']}:{index}:{now:.6f}")
                if self.order_history_path:
                    try:
                        with open(self.order_history_path, 'a', encoding='utf-8') as stream:
                            stream.write(json.dumps({'kind': 'notification', 'id': order['id'], 'message': message,
                                                     'recorded_at': now}, ensure_ascii=False) + '\n')
                    except OSError as exc:
                        self.write_log(f'通知审计日志写入失败: {exc}')
                self.write_log(message)
        return True

    def close(self):
        self._closed = True
        self.event_engine.unregister(EVENT_INDICATOR, self.process_indicator)
        self._signal_executor.shutdown(wait=True, cancel_futures=True)

    def _validate_order_config(self, order_config: dict):
        if not isinstance(order_config, dict):
            raise ValueError("单个条件单配置必须是对象")
        unknown_fields = set(order_config) - ORDER_CONFIG_FIELDS
        if unknown_fields:
            raise ValueError(
                f"条件单 {order_config.get('id', '<unknown>')} 包含未支持字段: "
                f"{', '.join(sorted(unknown_fields))}"
            )
        for field in ('id', 'symbol', 'exchange', 'conditions', 'actions'):
            if field not in order_config:
                raise ValueError(f"条件单缺少字段: {field}")
        if not isinstance(order_config['id'], str) or not order_config['id']:
            raise ValueError('条件单id必须是非空字符串')
        if not isinstance(order_config.get('enabled', False), bool):
            raise ValueError('条件单enabled必须是布尔值')
        if not isinstance(order_config['symbol'], str):
            raise ValueError(
                f"条件单 {order_config['id']} symbol必须是字符串，YAML中请加引号: "
                f"{order_config['symbol']!r}"
            )
        if order_config['exchange'] not in EXCHANGE_MAP:
            raise ValueError(f"条件单 {order_config['id']} 不支持的交易所: {order_config['exchange']}")
        if not isinstance(order_config['actions'], list) or not order_config['actions']:
            raise ValueError(f"条件单 {order_config['id']} actions必须是非空列表")
        self._validate_condition_config(order_config['id'], order_config['conditions'])
        for action in order_config['actions']:
            self._validate_action_config(order_config['id'], action)
        if 'notification' in order_config:
            self._validate_notification_config(order_config)
        if 'cancel' in order_config:
            self._validate_cancel_config(order_config['id'], order_config['cancel'])

    def _validate_condition_config(self, order_id: str, node: dict):
        if not isinstance(node, dict):
            raise ValueError(f"条件单 {order_id} 条件节点必须是对象")

        items = node.get('items', node.get('conditions'))
        if items is not None:
            unknown_fields = set(node) - GROUP_CONDITION_FIELDS
            if unknown_fields:
                raise ValueError(
                    f"条件单 {order_id} 条件组包含未支持字段: {', '.join(sorted(unknown_fields))}"
                )
            if not isinstance(items, list) or not items:
                raise ValueError(f"条件单 {order_id} 条件组items/conditions必须是非空列表")
            op = str(node.get('logical_operator', node.get('operator', 'AND'))).upper()
            if op not in ('AND', 'OR'):
                raise ValueError(f"条件单 {order_id} 不支持的逻辑操作符: {op}")
            for item in items:
                self._validate_condition_config(order_id, item)
            return

        unknown_fields = set(node) - LEAF_CONDITION_FIELDS
        if unknown_fields:
            raise ValueError(
                f"条件单 {order_id} 条件叶子包含未支持字段: {', '.join(sorted(unknown_fields))}"
            )
        for field in ('variable', 'operator', 'value'):
            if field not in node:
                raise ValueError(f"条件单 {order_id} 条件缺少字段: {field}")
        if node['operator'] not in ('>', '<', '>=', '<=', '==', '!=', 'contains'):
            raise ValueError(f"条件单 {order_id} 不支持的比较符: {node['operator']}")

        if not isinstance(node['variable'], str):
            raise ValueError('variable必须是字符串')
        variable = node['variable']
        operator = node['operator']
        value = node['value']
        if value is None or isinstance(value, (bool, list, dict)):
            raise ValueError(
                f"条件单 {order_id} {variable} 的value类型不支持: {value!r}"
            )

        if variable in UNRELIABLE_TICK_VARIABLES:
            raise ValueError(
                f"条件单 {order_id} 变量 {variable} 不可用："
                f"富途行情无真实涨跌停价，请用具体价格数值表达边界"
            )

        if set(node) & {'params', 'timeframe', 'output'} and not variable.startswith(('signal.', 'tools.')):
            raise ValueError('params/timeframe/output仅用于指标条件')
        if variable.startswith(('signal.', 'tools.')):
            validate_indicator(node)
            if getattr(self, 'config', None) is not None and (variable.startswith('tools.') or set(node) & {'params', 'timeframe'}):
                timeframe_of(node, self.config)
        if variable.startswith('tools.'):
            if operator not in DIRECTIONAL_OPERATORS:
                raise ValueError('tools.*只支持 >/</>=/<=')
            try:
                valid = math.isfinite(float(value))
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError('指标阈值必须是有限数值')
        elif variable.startswith('signal.'):
            parts = variable.split('.')
            if len(parts) != 2 or parts[1] not in SIGNAL_FUNCTIONS:
                raise ValueError(
                    f"条件单 {order_id} 不支持的信号变量: {variable}"
                    f"（可用: {', '.join('signal.' + s for s in sorted(SIGNAL_FUNCTIONS))}）"
                )
            if operator not in EQUALITY_OPERATORS:
                raise ValueError(
                    f"条件单 {order_id} {variable} 是字符串信号，"
                    f"只支持 ==/!=/contains，不支持: {operator}"
                )
        elif variable == 'datetime':
            if operator not in DIRECTIONAL_OPERATORS:
                raise ValueError(
                    f"条件单 {order_id} datetime为连续值（对齐IBKR无相等概念），"
                    f"只支持 >/</>=/<=，不支持: {operator}"
                )
            if not DATETIME_VALUE_RE.match(str(value)):
                raise ValueError(
                    f"条件单 {order_id} datetime比较值格式必须为 "
                    f"YYYY-MM-DD HH:MM:SS +0800: {value!r}"
                )
        elif variable in NUMERIC_TICK_VARIABLES:
            if operator not in DIRECTIONAL_OPERATORS:
                raise ValueError(
                    f"条件单 {order_id} {variable} 为连续数值（对齐IBKR无相等概念），"
                    f"只支持 >/</>=/<=，不支持: {operator}"
                )
            try:
                float(value)
            except (TypeError, ValueError):
                raise ValueError(
                    f"条件单 {order_id} {variable} 的value无法解析为数字: {value!r}"
                )
        elif variable in TICK_CONDITION_VARIABLES:
            # 剩余为字符串型tick字段（symbol/name等），只支持相等语义
            if operator not in EQUALITY_OPERATORS:
                raise ValueError(
                    f"条件单 {order_id} {variable} 是字符串字段，"
                    f"只支持 ==/!=/contains，不支持: {operator}"
                )
        else:
            raise ValueError(
                f"条件单 {order_id} 不支持的变量: {variable}"
                f"（可用tick字段见 orders.example.yaml）"
            )

    def _validate_action_config(self, order_id: str, action: dict):
        if not isinstance(action, dict):
            raise ValueError(f"条件单 {order_id} action必须是对象")
        action_type = str(action.get('type', '')).lower()
        if action_type == 'notify':
            unknown_fields = set(action) - NOTIFY_ACTION_FIELDS
            if unknown_fields:
                raise ValueError(
                    f"条件单 {order_id} notify action包含未支持字段: {', '.join(sorted(unknown_fields))}"
                )
            return

        if action_type not in ORDER_TYPE_MAP:
            raise ValueError(f"条件单 {order_id} 不支持的action type: {action.get('type')}")
        unknown_fields = set(action) - ORDER_ACTION_FIELDS
        if unknown_fields:
            raise ValueError(
                f"条件单 {order_id} 下单action包含未支持字段: {', '.join(sorted(unknown_fields))}"
            )
        for field in ('action', 'quantity'):
            if field not in action:
                raise ValueError(f"条件单 {order_id} 下单action缺少字段: {field}")
        if str(action['action']).lower() not in DIRECTION_MAP:
            raise ValueError(f"条件单 {order_id} 不支持的下单方向: {action['action']}")

        try:
            quantity = float(action['quantity'])
        except (TypeError, ValueError):
            raise ValueError(f"条件单 {order_id} quantity无法解析为数字: {action['quantity']!r}")
        if quantity <= 0:
            raise ValueError(f"条件单 {order_id} quantity必须大于0: {action['quantity']!r}")

        if action_type == 'limit':
            if 'price' not in action:
                raise ValueError(f"条件单 {order_id} limit下单action缺少price")
            try:
                price = float(action['price'])
            except (TypeError, ValueError):
                raise ValueError(f"条件单 {order_id} price无法解析为数字: {action['price']!r}")
            if price < 0:
                raise ValueError(f"条件单 {order_id} price不能为负数: {action['price']!r}")

    def _validate_cancel_config(self, order_id: str, cancel_config: dict):
        if not isinstance(cancel_config, dict):
            raise ValueError(f"条件单 {order_id} cancel必须是对象")
        unknown_fields = set(cancel_config) - CANCEL_CONFIG_FIELDS
        if unknown_fields:
            raise ValueError(
                f"条件单 {order_id} cancel包含未支持字段: {', '.join(sorted(unknown_fields))}"
            )
        if not cancel_config.get("enabled", False):
            return
        if "conditions" not in cancel_config:
            raise ValueError(f"条件单 {order_id} cancel缺少字段: conditions")
        self._validate_condition_config(order_id, cancel_config["conditions"])

    def _orders_with_cancel(self):
        return [
            order for order in self.active_orders.values()
            if order.get("cancel", {}).get("enabled", False)
        ]

    def _order_config_hash(self, order_config: dict) -> str:
        text = json.dumps(order_config, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]

    def _load_state(self) -> dict:
        if not self.state_file_path or not os.path.exists(self.state_file_path):
            return {}
        try:
            with open(self.state_file_path, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
            if not isinstance(data, dict):
                raise ValueError("状态文件必须是JSON对象")
            return data
        except (OSError, ValueError) as e:
            self.write_log(f"读取条件单状态文件失败，按空状态处理: {e}")
            return {}

    def _save_state(self):
        if not self.state_file_path:
            return True
        try:
            tmp_path = f"{self.state_file_path}.tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(
                    {"triggered": self.triggered_state, "terminal_orders": self.terminal_orders, "notifications": self.notification_state},
                    f, ensure_ascii=False, indent=2,
                )
            os.replace(tmp_path, self.state_file_path)
            return True
        except OSError as e:
            self.write_log(f"写入条件单状态文件失败: {e}")
            return False

    def _mark_triggered(self, order_config: dict):
        self.triggered_state[order_config['id']] = {
            "config_hash": self._order_config_hash(order_config),
            "triggered_at": datetime.now().isoformat(timespec='seconds'),
        }
        if self._save_state():
            return True
        self.triggered_state.pop(order_config['id'], None)
        return False

    def _archive_terminal_order(self, order: OrderData):
        """终态订单归档到状态文件并释放内存；轮询/重启重复推送的终态订单直接忽略"""
        self.terminal_orders[order.vt_orderid] = {
            "status": order.status.value,
            "traded": order.traded,
            "date": datetime.now().strftime("%Y-%m-%d"),
        }
        self.order_snapshots.pop(order.vt_orderid, None)
        self.managed_orders.pop(order.vt_orderid, None)
        self._save_state()

    def _validate_gateway_references(self):
        known_gateways = self.main_engine.get_gateway_names()
        for order in self.active_orders.values():
            referenced = {str(order.get("data_gateway", self.data_gateway_name)).upper()}
            for action in order['actions']:
                if action['type'] != 'notify':
                    referenced.add(str(action.get('order_gateway', order.get('order_gateway', self.order_gateway_name))).upper())
            missing = referenced - known_gateways
            if missing:
                raise ValueError(
                    f"条件单 {order['id']} 引用未注册网关: {', '.join(sorted(missing))}"
                    f"（已注册: {', '.join(sorted(known_gateways)) or '无'}）"
                )

    def _register_managed_order(self, order_config: dict, vt_orderid: str, gateway_name: str):
        gateway_from_vt, orderid = self._split_vt_orderid(vt_orderid)
        gateway_name = gateway_from_vt or gateway_name
        self.managed_orders[vt_orderid] = {
            "order_config": order_config,
            "gateway_name": gateway_name,
            "orderid": orderid,
            "cancel_attempts": 0,
            "last_cancel_ts": 0.0,
            "latest_order": self.order_snapshots.get(vt_orderid),
        }

    def _split_vt_orderid(self, vt_orderid: str) -> tuple[str, str]:
        if "." not in vt_orderid:
            return "", vt_orderid
        return vt_orderid.split(".", 1)

    def _process_cancel_conditions(self, tick: TickData):
        for vt_orderid, managed_order in list(self.managed_orders.items()):
            order_config = managed_order["order_config"]
            cancel_config = order_config.get("cancel", {})
            if not cancel_config.get("enabled", False):
                continue
            if managed_order["cancel_attempts"] >= CANCEL_MAX_ATTEMPTS:
                continue
            if time.time() - managed_order["last_cancel_ts"] < CANCEL_RETRY_INTERVAL:
                continue
            if not self._is_managed_order_open(managed_order):
                continue
            if tick.symbol != order_config['symbol'] or tick.exchange != EXCHANGE_MAP[order_config['exchange']]:
                continue
            data_gateway = str(order_config.get("data_gateway", self.data_gateway_name)).upper()
            if tick.gateway_name and tick.gateway_name.upper() != data_gateway:
                continue
            if not self._evaluate_condition_node(order_config, cancel_config["conditions"], tick):
                continue

            req = CancelRequest(
                orderid=managed_order["orderid"],
                symbol=order_config["symbol"],
                exchange=EXCHANGE_MAP[order_config["exchange"]],
            )
            result = self.main_engine.cancel_order(req, managed_order["gateway_name"])
            managed_order["last_cancel_ts"] = time.time()
            managed_order["cancel_attempts"] += 1
            attempts = f'{managed_order["cancel_attempts"]}/{CANCEL_MAX_ATTEMPTS}'
            if result is not None:
                self.write_log(f"条件单 {order_config['id']} 撤单条件触发，已提交撤单({attempts}): {vt_orderid}")
            else:
                self.write_log(f"条件单 {order_config['id']} 撤单条件触发，但撤单提交失败({attempts}): {vt_orderid}")
            if managed_order["cancel_attempts"] >= CANCEL_MAX_ATTEMPTS:
                self.write_log(
                    f"条件单 {order_config['id']} 撤单尝试已达上限，不再自动撤单，"
                    f"请人工处理（订单可能已成交/废单/柜台拒绝撤单）: {vt_orderid}"
                )

    def _is_managed_order_open(self, managed_order: dict) -> bool:
        order = managed_order.get("latest_order")
        if order is None:
            vt_orderid = f"{managed_order['gateway_name']}.{managed_order['orderid']}"
            order = self.order_snapshots.get(vt_orderid)
        if order is None:
            return True
        return order.status not in TERMINAL_ORDER_STATUSES

    def _format_order_message(self, order: OrderData) -> str:
        raw_status = f" raw={order.raw_status}" if order.raw_status is not None else ""
        status_msg = f" msg={order.status_msg}" if order.status_msg else ""
        return (
            f"订单状态: order={order.orderid} {order.symbol}.{order.exchange.value} "
            f"{order.direction.value} status={order.status.value}{raw_status}{status_msg} "
            f"traded={order.traded}/{order.volume} price={order.price}"
        )

    def _record_history(self, kind: str, data):
        if isinstance(data, OrderData):
            payload = {
                "kind": kind,
                "orderid": data.orderid,
                "symbol": data.symbol,
                "exchange": data.exchange.value,
                "direction": data.direction.value,
                "status": data.status.value,
                "raw_status": data.raw_status,
                "status_msg": data.status_msg,
                "traded": data.traded,
                "volume": data.volume,
                "price": data.price,
                "reference": data.reference,
            }
        else:
            payload = {
                "kind": kind,
                "orderid": data.orderid,
                "tradeid": data.tradeid,
                "symbol": data.symbol,
                "exchange": data.exchange.value,
                "direction": data.direction.value,
                "price": data.price,
                "volume": data.volume,
            }

        payload["recorded_at"] = datetime.now().isoformat()
        if not self.order_history_path:
            return
        try:
            line = json.dumps(payload, ensure_ascii=False, default=str)
            with open(self.order_history_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            self.write_log(f"审计日志写入失败: {e}")
