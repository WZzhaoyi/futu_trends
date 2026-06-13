from order_engine.condition import ConditionOrderEngine
from order_engine.core import MainEngine, BaseEngine
from order_engine.event_engine import EventEngine
from order_engine.models import (
    Exchange, Direction, Offset, Status, OrderType, Product,
    TickData, OrderData, TradeData, PositionData, AccountData,
    ContractData, OrderRequest, SubscribeRequest, CancelRequest, LogData,
)


def __getattr__(name):
    if name in ("FutuGateway", "QmtGateway"):
        from order_engine.gateway import FutuGateway, QmtGateway
        return {"FutuGateway": FutuGateway, "QmtGateway": QmtGateway}[name]
    raise AttributeError(f"module 'order_engine' has no attribute {name!r}")
