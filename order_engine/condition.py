import configparser
import json
import numbers
import os
import re
from collections.abc import Callable
import yaml
from copy import copy
from datetime import datetime

from order_engine.event_engine import Event, EVENT_LOG, EVENT_ORDER, EVENT_TICK, EVENT_TRADE
from order_engine.core import MainEngine, BaseEngine
from order_engine.models import (
    TickData, OrderRequest, SubscribeRequest, LogData, OrderData, TradeData,
    Exchange, Direction, Offset, OrderType,
)

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


class ConditionOrderEngine(BaseEngine):
    """条件单引擎"""

    def __init__(self, main_engine: MainEngine, event_engine, engine_name='ConditionOrder'):
        super().__init__(main_engine, event_engine, engine_name)
        self.active_orders = {}
        self.order_snapshots = {}
        self.order_history = []
        self.order_history_path = None
        self.data_gateway_name = "FUTU"
        self.order_gateway_name = "QMT"

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

            for order_config in orders:
                self._validate_order_config(order_config)
                if order_config.get('enabled', False):
                    self.active_orders[order_config['id']] = order_config

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
                if executed and order.get('trigger_once', True):
                    del self.active_orders[order_id]
                elif not executed:
                    self.write_log(f"条件单 {order_id} 动作执行失败，保持激活等待下次触发")

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
        if operator not in ('>', '<', '>=', '<=', '==', '!='):
            self.write_log(f"条件单 {order['id']} {variable} 不支持的比较符: {operator}")
            return False
        if 'value' not in cond:
            self.write_log(f"条件单 {order['id']} {variable} 缺少value")
            return False

        var_value = getattr(tick, variable, None)
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
        previous = self.order_snapshots.get(order.orderid)
        changed = (
            previous is None
            or previous.status != order.status
            or previous.traded != order.traded
            or previous.raw_status != order.raw_status
        )
        if not changed:
            return

        self.order_snapshots[order.orderid] = copy(order)
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

    def _validate_order_config(self, order_config: dict):
        if not isinstance(order_config, dict):
            raise ValueError("单个条件单配置必须是对象")
        for field in ('id', 'symbol', 'exchange', 'conditions', 'actions'):
            if field not in order_config:
                raise ValueError(f"条件单缺少字段: {field}")
        if order_config['exchange'] not in EXCHANGE_MAP:
            raise ValueError(f"条件单 {order_config['id']} 不支持的交易所: {order_config['exchange']}")
        if not isinstance(order_config['actions'], list) or not order_config['actions']:
            raise ValueError(f"条件单 {order_config['id']} actions必须是非空列表")

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
