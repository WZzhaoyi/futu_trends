import configparser
import json
import numbers
import os
import re
import time
from collections.abc import Callable
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


class ConditionOrderEngine(BaseEngine):
    """条件单引擎"""

    def __init__(self, main_engine: MainEngine, event_engine, engine_name='ConditionOrder'):
        super().__init__(main_engine, event_engine, engine_name)
        self.active_orders = {}
        self.managed_orders = {}
        self.order_snapshots = {}
        self.order_history = []
        self.order_history_path = None
        self.data_gateway_name = "FUTU"
        self.order_gateway_name = "QMT"
        self.config = None
        self.signal_cache = {}

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
        self.data_gateway_name = config.get("CONFIG", "ORDER_DATA_GATEWAY", fallback="FUTU").strip().upper()
        self.order_gateway_name = config.get("CONFIG", "ORDER_TRADE_GATEWAY", fallback="QMT").strip().upper()
        self.config = config
        if self.order_history_path:
            history_dir = os.path.dirname(os.path.abspath(self.order_history_path))
            os.makedirs(history_dir, exist_ok=True)

        if os.path.exists(self.yaml_config_path):
            # 加载条件单
            with open(self.yaml_config_path, 'r', encoding='utf-8') as f:
                yaml_config = yaml.safe_load(f) or {}
            orders = yaml_config.get('orders', [])
            if not isinstance(orders, list):
                raise ValueError("ORDER_CONFIG中的orders必须是列表")

            seen_order_ids = set()
            for order_config in orders:
                self._validate_order_config(order_config)
                order_id = order_config['id']
                if order_id in seen_order_ids:
                    raise ValueError(f"条件单id重复: {order_id}")
                seen_order_ids.add(order_id)
                if order_config.get('enabled', False):
                    self.active_orders[order_id] = order_config

            self.event_engine.register(EVENT_TICK, self.process_tick)
            self.event_engine.register(EVENT_ORDER, self.process_order_event)
            self.event_engine.register(EVENT_TRADE, self.process_trade_event)
            if callable(notify_calc):
                # 注册LOG事件回调
                def process_log(event: Event):
                    log: LogData = event.data
                    notify_calc(log.msg)
                self.event_engine.register(EVENT_LOG, process_log)

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
                self.main_engine.subscribe(req, data_gateway)

        else:
            self.write_log(f"yaml配置不存在: {self.yaml_config_path}")
            raise Exception(f"yaml配置不存在: {self.yaml_config_path}")

    def process_tick(self, event: Event):
        tick: TickData = event.data
        for order_id, order in list(self.active_orders.items()):
            # 检查标的匹配
            if tick.symbol != order['symbol'] or tick.exchange != EXCHANGE_MAP[order['exchange']]:
                continue
            data_gateway = str(order.get("data_gateway", self.data_gateway_name)).upper()
            if tick.gateway_name and tick.gateway_name.upper() != data_gateway:
                continue

            if self._check_condition(order, tick):
                self.write_log(f"条件单 '{order.get('description', order_id)}' 触发")
                executed = self._execute_actions(order)
                if executed:
                    del self.active_orders[order_id]
                elif not executed:
                    self.write_log(f"条件单 {order_id} 动作执行失败，保持激活等待下次触发")
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
        success = True
        sent_orders = 0

        for action in order['actions']:
            action_type = str(action.get('type', '')).lower()
            if action_type == 'notify':
                self.write_log(f"通知: {action.get('message', '')}")
            else:
                try:
                    direction = DIRECTION_MAP[str(action['action']).lower()]
                    order_type = ORDER_TYPE_MAP[action_type]
                    req = OrderRequest(
                        symbol=order['symbol'],
                        exchange=EXCHANGE_MAP[order['exchange']],
                        direction=direction,
                        type=order_type,
                        volume=float(action['quantity']),
                        price=float(action.get('price', 0)),
                        offset=Offset.OPEN
                    )
                except Exception as e:
                    self.write_log(f"条件单 {order['id']} 动作配置异常: {e}")
                    success = False
                    continue

                order_gateway = str(
                    action.get("order_gateway", order.get("order_gateway", self.order_gateway_name))
                ).upper()
                vt_orderid = self.main_engine.send_order(req, order_gateway)
                if vt_orderid:
                    sent_orders += 1
                    self._register_managed_order(order, vt_orderid, order_gateway)
                    self.write_log(f"条件单 {order['id']} 已提交订单: {vt_orderid}")
                else:
                    self.write_log(f"条件单 {order['id']} 下单失败: {order['symbol']} {action}")
                    success = False

        if sent_orders and not success:
            self.write_log(f"条件单 {order['id']} 存在部分动作失败；已提交订单不再重复触发")
            return True

        return success

    def process_order_event(self, event: Event):
        order: OrderData = event.data
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
            match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}\d{2})$', target_text)
            if not match:
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
        now = time.time()
        cached = self.signal_cache.get(cache_key)
        if cached and now - cached["ts"] < SIGNAL_CACHE_TTL:
            return cached["msg"]

        try:
            df = get_kline_data(code, self.config, max_count=1000)
            if df is None or len(df) < 90:
                self.write_log(f"条件单 {order['id']} 信号 {name} K线不足: {code}")
                msg = ""
            elif name == "balance":
                msg = signal_func(df) or ""
            else:
                msg = signal_func(df, code, self.config) or ""
        except Exception as e:
            self.write_log(f"条件单 {order['id']} 信号 {name} 计算异常: {e}")
            msg = ""

        self.signal_cache[cache_key] = {"ts": now, "msg": msg}
        return msg

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

    def _register_managed_order(self, order_config: dict, vt_orderid: str, gateway_name: str):
        gateway_from_vt, orderid = self._split_vt_orderid(vt_orderid)
        gateway_name = gateway_from_vt or gateway_name
        self.managed_orders[vt_orderid] = {
            "order_config": order_config,
            "gateway_name": gateway_name,
            "orderid": orderid,
            "cancel_requested": False,
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
            if managed_order.get("cancel_requested"):
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
            if result is not None:
                managed_order["cancel_requested"] = True
                self.write_log(f"条件单 {order_config['id']} 撤单条件触发，已提交撤单: {vt_orderid}")
            else:
                self.write_log(f"条件单 {order_config['id']} 撤单条件触发，但撤单提交失败: {vt_orderid}")

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
        self.order_history.append(payload)
        if not self.order_history_path:
            return
        try:
            with open(self.order_history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"write order history failed: {e}")
