import configparser
import os
import re
from typing import Callable
import yaml
from datetime import datetime

from order_engine.event_engine import Event, EVENT_LOG, EVENT_TICK
from order_engine.core import MainEngine, BaseEngine
from order_engine.models import (
    TickData, OrderRequest, SubscribeRequest, LogData,
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

    def write_log(self, msg: str, source: str = "") -> None:
        log = LogData(msg=msg, gateway_name=source)
        event = Event(EVENT_LOG, log)
        self.event_engine.put(event)

    def load(self, config: configparser.ConfigParser, notify_calc: Callable[[str], None] = None):
        """加载配置"""
        self.yaml_config_path = config.get("CONFIG", "ORDER_CONFIG", fallback=None)
        if os.path.exists(self.yaml_config_path):
            # 加载条件单
            with open(self.yaml_config_path, 'r', encoding='utf-8') as f:
                yaml_config = yaml.safe_load(f)
            for order_config in yaml_config.get('orders', []):
                if order_config.get('enabled', False):
                    self.active_orders[order_config['id']] = order_config

            self.event_engine.register(EVENT_TICK, self.process_tick)
            if isinstance(notify_calc, Callable):
                # 注册LOG事件回调
                def process_log(event: Event):
                    log: LogData = event.data
                    notify_calc(log.msg)
                self.event_engine.register(EVENT_LOG, process_log)

            # 订阅行情 暂定futu
            symbols_to_subscribe = set()
            for order in self.active_orders.values():
                req = SubscribeRequest(symbol=order['symbol'], exchange=EXCHANGE_MAP[order['exchange']])
                symbols_to_subscribe.add((req.symbol, req.exchange))

            for symbol, exchange in symbols_to_subscribe:
                req = SubscribeRequest(symbol=symbol, exchange=exchange)
                self.main_engine.subscribe(req, "FUTU")

        else:
            self.write_log(f"yaml配置不存在: {self.yaml_config_path}")
            raise Exception(f"yaml配置不存在: {self.yaml_config_path}")

    def process_tick(self, event: Event):
        tick: TickData = event.data
        for order_id, order in list(self.active_orders.items()):
            # 检查标的匹配
            if tick.symbol != order['symbol'] or tick.exchange != EXCHANGE_MAP[order['exchange']]:
                continue

            if self._check_condition(order, tick):
                self.write_log(f"条件单 '{order['description']}' 触发")
                self._execute_actions(order)
                if order.get('trigger_once', True):
                    del self.active_orders[order_id]

    def _check_condition(self, order: dict, tick: TickData):
        items = order['conditions']['items']
        op = order['conditions'].get('logical_operator', 'AND').upper()

        results = []
        for cond in items:
            result = False

            if 'variable' not in cond:
                results.append(False)
                self.write_log(f"条件单 {order['id']} {cond['variable']} 变量不存在")
                continue

            var_value = getattr(tick, cond['variable'], None)
            if var_value is None:
                results.append(False)
                self.write_log(f"条件单 {order['id']} {cond['variable']} 变量值为空")
                continue

            target_value = cond['value']
            operator = cond['operator']

            # 如果是datetime类型，统一转为UTC时间戳进行比较
            if isinstance(var_value, datetime):
                target_value = str(target_value)
                match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}\d{2})$', target_value)
                if match:
                    target_value = datetime.strptime(target_value, '%Y-%m-%d %H:%M:%S %z')
                    var_value = var_value.timestamp()
                    target_value = target_value.timestamp()
                else:
                    results.append(False)
                    self.write_log(f"条件单 {order['id']} {cond['variable']} 时间格式异常: {target_value}")
                    continue

            try:
                if operator == '>':   result = var_value > target_value
                elif operator == '<':   result = var_value < target_value
                elif operator == '>=':  result = var_value >= target_value
                elif operator == '<=':  result = var_value <= target_value
                elif operator == '==':  result = var_value == target_value
            except Exception as e:
                self.write_log(f"条件单 {order['id']} {cond['variable']} 比较异常: {e}")
                result = False

            results.append(result)

        if not results: return False
        return all(results) if op == 'AND' else any(results)

    def _execute_actions(self, order):
        for action in order['actions']:
            if action['type'] == 'notify':
                self.write_log(f"通知: {action['message']}")
            else:
                req = OrderRequest(
                    symbol=order['symbol'],
                    exchange=EXCHANGE_MAP[order['exchange']],
                    direction=DIRECTION_MAP[action['action']],
                    type=ORDER_TYPE_MAP[action['type']],
                    volume=action['quantity'],
                    price=action.get('price', 0),
                    offset=Offset.OPEN
                )
                self.main_engine.send_order(req, "QMT")
