"""
轻量交易引擎，替代 vnpy 的 MainEngine 和 BaseEngine。
负责网关注册、路由和子引擎管理。
"""
from typing import Dict, Type

from order_engine.event_engine import (
    Event, EventEngine,
    EVENT_LOG, EVENT_TICK, EVENT_ORDER, EVENT_TRADE,
    EVENT_ACCOUNT, EVENT_POSITION, EVENT_CONTRACT,
)
from order_engine.models import LogData, OrderRequest, SubscribeRequest


class BaseGateway:
    """网关基类"""

    def __init__(self, event_engine: EventEngine, gateway_name: str):
        self.event_engine = event_engine
        self.gateway_name = gateway_name

    def connect(self, setting: dict):
        raise NotImplementedError

    def subscribe(self, req: SubscribeRequest):
        raise NotImplementedError

    def send_order(self, req: OrderRequest) -> str:
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def on_event(self, event_type: str, data):
        event = Event(type=event_type, data=data)
        self.event_engine.put(event)

    def on_tick(self, tick):
        self.on_event(EVENT_TICK, tick)

    def on_order(self, order):
        self.on_event(EVENT_ORDER, order)

    def on_trade(self, trade):
        self.on_event(EVENT_TRADE, trade)

    def on_account(self, account):
        self.on_event(EVENT_ACCOUNT, account)

    def on_position(self, position):
        self.on_event(EVENT_POSITION, position)

    def on_contract(self, contract):
        self.on_event(EVENT_CONTRACT, contract)

    def write_log(self, msg: str):
        log = LogData(msg=msg, gateway_name=self.gateway_name)
        event = Event(type=EVENT_LOG, data=log)
        self.event_engine.put(event)


class BaseEngine:
    """子引擎基类"""

    def __init__(self, main_engine: "MainEngine", event_engine: EventEngine, engine_name: str):
        self.main_engine = main_engine
        self.event_engine = event_engine
        self.engine_name = engine_name


class MainEngine:
    """轻量主引擎：管理网关和子引擎"""

    def __init__(self, event_engine: EventEngine):
        self.event_engine = event_engine
        self._gateways: Dict[str, BaseGateway] = {}
        self._engines: Dict[str, BaseEngine] = {}
        self.event_engine.start()

    def add_gateway(self, gateway_class: Type[BaseGateway], gateway_name: str):
        gateway = gateway_class(self.event_engine, gateway_name)
        self._gateways[gateway_name] = gateway
        return gateway

    def connect(self, setting: dict, gateway_name: str):
        gateway = self._gateways.get(gateway_name)
        if gateway:
            gateway.connect(setting)

    def subscribe(self, req: SubscribeRequest, gateway_name: str):
        gateway = self._gateways.get(gateway_name)
        if gateway:
            gateway.subscribe(req)

    def send_order(self, req: OrderRequest, gateway_name: str) -> str:
        gateway = self._gateways.get(gateway_name)
        if gateway:
            return gateway.send_order(req)
        return ""

    def add_engine(self, engine_class: Type[BaseEngine]) -> BaseEngine:
        engine = engine_class(self, self.event_engine)
        self._engines[engine.engine_name] = engine
        return engine

    def close(self):
        self.event_engine.stop()
        for gateway in self._gateways.values():
            gateway.close()
