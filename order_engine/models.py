"""
轻量数据模型和枚举定义，替代 vnpy 的 trader.object 和 trader.constant。
字段命名统一使用 Futu API 惯例。
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Exchange(str, Enum):
    SSE = "SH"       # 上交所
    SZSE = "SZ"      # 深交所
    SEHK = "HK"      # 港交所
    SMART = "US"     # 美股
    SHHK = "HGT"     # 沪港通
    SZHK = "SGT"     # 深港通
    LOCAL = "LOCAL"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Offset(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class Status(str, Enum):
    SUBMITTING = "SUBMITTING"
    NOTTRADED = "NOTTRADED"
    PARTTRADED = "PARTTRADED"
    ALLTRADED = "ALLTRADED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class Product(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    BOND = "BOND"
    INDEX = "INDEX"


@dataclass
class TickData:
    symbol: str = ""
    exchange: Exchange = Exchange.LOCAL
    datetime: datetime = None
    gateway_name: str = ""
    name: str = ""

    last_price: float = 0.0
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    pre_close: float = 0.0
    volume: float = 0.0

    limit_up: float = 0.0
    limit_down: float = 0.0

    # 五档行情
    ask_price_1: float = 0.0
    ask_price_2: float = 0.0
    ask_price_3: float = 0.0
    ask_price_4: float = 0.0
    ask_price_5: float = 0.0
    ask_volume_1: float = 0.0
    ask_volume_2: float = 0.0
    ask_volume_3: float = 0.0
    ask_volume_4: float = 0.0
    ask_volume_5: float = 0.0
    bid_price_1: float = 0.0
    bid_price_2: float = 0.0
    bid_price_3: float = 0.0
    bid_price_4: float = 0.0
    bid_price_5: float = 0.0
    bid_volume_1: float = 0.0
    bid_volume_2: float = 0.0
    bid_volume_3: float = 0.0
    bid_volume_4: float = 0.0
    bid_volume_5: float = 0.0

    @property
    def vt_symbol(self) -> str:
        return f"{self.symbol}.{self.exchange.value}"


@dataclass
class OrderRequest:
    symbol: str = ""
    exchange: Exchange = Exchange.LOCAL
    direction: Direction = Direction.LONG
    type: OrderType = OrderType.LIMIT
    volume: float = 0.0
    price: float = 0.0
    offset: Offset = Offset.OPEN


@dataclass
class SubscribeRequest:
    symbol: str = ""
    exchange: Exchange = Exchange.LOCAL


@dataclass
class CancelRequest:
    orderid: str = ""
    symbol: str = ""
    exchange: Exchange = Exchange.LOCAL


@dataclass
class OrderData:
    symbol: str = ""
    exchange: Exchange = Exchange.LOCAL
    orderid: str = ""
    gateway_name: str = ""
    type: OrderType = OrderType.LIMIT
    direction: Direction = Direction.LONG
    offset: Offset = Offset.OPEN
    volume: float = 0.0
    traded: float = 0.0
    price: float = 0.0
    status: Status = Status.SUBMITTING
    datetime: datetime = None
    reference: str = ""

    @property
    def vt_orderid(self) -> str:
        return f"{self.gateway_name}.{self.orderid}"


@dataclass
class TradeData:
    symbol: str = ""
    exchange: Exchange = Exchange.LOCAL
    orderid: str = ""
    tradeid: str = ""
    gateway_name: str = ""
    direction: Direction = Direction.LONG
    price: float = 0.0
    volume: float = 0.0
    datetime: datetime = None


@dataclass
class PositionData:
    symbol: str = ""
    exchange: Exchange = Exchange.LOCAL
    direction: Direction = Direction.LONG
    gateway_name: str = ""
    volume: float = 0.0
    yd_volume: float = 0.0
    price: float = 0.0
    pnl: float = 0.0


@dataclass
class AccountData:
    accountid: str = ""
    balance: float = 0.0
    frozen: float = 0.0
    gateway_name: str = ""


@dataclass
class ContractData:
    symbol: str = ""
    exchange: Exchange = Exchange.LOCAL
    name: str = ""
    product: Product = Product.EQUITY
    gateway_name: str = ""
    pricetick: float = 0.0
    size: int = 1
    min_volume: int = 1

    @property
    def vt_symbol(self) -> str:
        return f"{self.symbol}.{self.exchange.value}"


@dataclass
class LogData:
    msg: str = ""
    gateway_name: str = ""
