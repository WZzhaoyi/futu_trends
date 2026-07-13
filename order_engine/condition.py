import configparser
import hashlib
import json
import numbers
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields as dataclass_fields
import yaml
from copy import copy
from datetime import datetime

from data import get_kline_data
from order_engine.event_engine import Event, EVENT_LOG, EVENT_ORDER, EVENT_TICK, EVENT_TRADE
from order_engine.core import MainEngine, BaseEngine
from order_engine.models import (
    TickData, OrderRequest, SubscribeRequest, CancelRequest, LogData, OrderData, TradeData,
    Exchange, Direction, Offset, OrderType, Status,
)
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
    "data_gateway", "order_gateway", "conditions", "actions", "cancel",
}

CANCEL_CONFIG_FIELDS = {"enabled", "conditions"}
GROUP_CONDITION_FIELDS = {"logical_operator", "operator", "items", "conditions"}
LEAF_CONDITION_FIELDS = {"variable", "operator", "value"}
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
SIGNAL_CACHE_TTL = 30
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
        self.signal_cache = {}
        self._signal_pending = set()
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
            orders = yaml_config.get('orders', [])
            if not isinstance(orders, list):
                raise ValueError("ORDER_CONFIG中的orders必须是列表")

            state = self._load_state()
            self.triggered_state = state.get("triggered", {})
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

                # 触发状态持久化：已触发且配置未修改的条件单不再装载，防止重启后重复下单
                triggered = self.triggered_state.get(order_id)
                config_hash = self._order_config_hash(order_config)
                if triggered and triggered.get('config_hash') == config_hash:
                    self.write_log(
                        f"条件单 {order_id} 已于 {triggered.get('triggered_at')} 触发过，跳过装载"
                        f"（修改该条件单配置或删除状态文件条目可重新激活）"
                    )
                    continue
                if triggered:
                    del self.triggered_state[order_id]
                    self._save_state()
                self.active_orders[order_id] = order_config

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

        else:
            self.write_log(f"yaml配置不存在: {self.yaml_config_path}")
            raise Exception(f"yaml配置不存在: {self.yaml_config_path}")

    def process_tick(self, event: Event):
        tick: TickData = event.data
        # 集合竞价前/坏数据的tick价格为0，不参与任何条件评估，防止止损类条件误触发
        if tick.last_price is None or tick.last_price <= 0:
            return
        for order_id, order in list(self.active_orders.items()):
            # 检查标的匹配
            if tick.symbol != order['symbol'] or tick.exchange != EXCHANGE_MAP[order['exchange']]:
                continue
            data_gateway = str(order.get("data_gateway", self.data_gateway_name)).upper()
            if tick.gateway_name and tick.gateway_name.upper() != data_gateway:
                continue

            if self._check_condition(order, tick):
                self.write_log(f"条件单 '{order.get('description', order_id)}' 触发")
                # 一次性语义（对齐IBKR条件单）：触发即退休并持久化，动作失败只通知、不重试
                del self.active_orders[order_id]
                self._mark_triggered(order)
                self._execute_actions(order)
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

        var_value = self._resolve_variable_value(order, tick, variable)
        if var_value is None:
            self.write_log(f"条件单 {order['id']} {variable} 变量值为空")
            return False

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
                self.write_log(f"通知: {action.get('message', '')}")
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

    def _resolve_variable_value(self, order: dict, tick: TickData, variable: str):
        if variable.startswith("signal."):
            return self._resolve_signal_variable(order, tick, variable)
        return getattr(tick, variable, None)

    def _resolve_signal_variable(self, order: dict, tick: TickData, variable: str):
        parts = variable.split(".")
        if len(parts) != 2:
            self.write_log(f"条件单 {order['id']} 不支持的信号变量: {variable}")
            return None

        return self._get_signal_message(order, tick, parts[1]) or ""

    def _get_signal_message(self, order: dict, tick: TickData, name: str) -> str:
        """读取信号缓存。缓存过期时在后台线程刷新，不阻塞tick处理；
        本次返回旧值（无旧值按空字符串参与比较）。"""
        signal_func = SIGNAL_FUNCTIONS.get(name)
        if signal_func is None:
            self.write_log(f"条件单 {order['id']} 不支持的信号: {name}")
            return ""
        if self.config is None:
            self.write_log(f"条件单 {order['id']} 尚未加载配置，无法计算信号: {name}")
            return ""

        code = f"{tick.exchange.value}.{tick.symbol}"
        ktype = self.config.get("CONFIG", "FUTU_PUSH_TYPE", fallback="")
        cache_key = (code, ktype, name)
        cached = self.signal_cache.get(cache_key)
        if cached and time.time() - cached["ts"] < SIGNAL_CACHE_TTL:
            return cached["msg"]

        if cache_key not in self._signal_pending:
            self._signal_pending.add(cache_key)
            self._signal_executor.submit(
                self._refresh_signal, cache_key, order['id'], signal_func, name, code
            )
        return cached["msg"] if cached else ""

    def _refresh_signal(self, cache_key, order_id: str, signal_func, name: str, code: str):
        """后台线程计算信号并写入缓存"""
        msg = ""
        try:
            df = get_kline_data(code, self.config, max_count=1000)
            if df is None or len(df) < 90:
                self.write_log(f"条件单 {order_id} 信号 {name} K线不足: {code}")
            elif name == "balance":
                msg = signal_func(df) or ""
            else:
                msg = signal_func(df, code, self.config) or ""
        except Exception as e:
            self.write_log(f"条件单 {order_id} 信号 {name} 计算异常: {e}")

        self.signal_cache[cache_key] = {"ts": time.time(), "msg": msg}
        self._signal_pending.discard(cache_key)

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

        variable = str(node['variable'])
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

        if variable.startswith('signal.'):
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
            return
        try:
            tmp_path = f"{self.state_file_path}.tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(
                    {"triggered": self.triggered_state, "terminal_orders": self.terminal_orders},
                    f, ensure_ascii=False, indent=2,
                )
            os.replace(tmp_path, self.state_file_path)
        except OSError as e:
            self.write_log(f"写入条件单状态文件失败: {e}")

    def _mark_triggered(self, order_config: dict):
        self.triggered_state[order_config['id']] = {
            "config_hash": self._order_config_hash(order_config),
            "triggered_at": datetime.now().isoformat(timespec='seconds'),
        }
        self._save_state()

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
            referenced = {
                str(order.get("data_gateway", self.data_gateway_name)).upper(),
                str(order.get("order_gateway", self.order_gateway_name)).upper(),
            }
            for action in order['actions']:
                if isinstance(action, dict) and 'order_gateway' in action:
                    referenced.add(str(action['order_gateway']).upper())
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
