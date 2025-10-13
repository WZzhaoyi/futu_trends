import futu as ft
import threading
import time
import datetime
from datetime import datetime
from copy import copy
from typing import Dict, Set

from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    TickData, OrderRequest, OrderData, TradeData, PositionData, AccountData, 
    SubscribeRequest, ContractData, CancelRequest
)
from vnpy.trader.constant import Exchange, Direction, Offset, Status, OrderType, Product
from vnpy.trader.event import EVENT_TICK, EVENT_ORDER, EVENT_TRADE, EVENT_ACCOUNT, EVENT_POSITION, EVENT_TIMER
from vnpy.trader.utility import ZoneInfo

from xtquant import xtconstant
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount, XtTrade, XtAsset, XtOrder, XtOrderError, XtOrderResponse, XtPosition, XtCancelError, XtCancelOrderResponse
import xtquant.xtdata

# 富途交易所映射
EXCHANGE_VT2FUTU = {
    Exchange.SMART: "US",
    Exchange.SEHK: "HK", 
    Exchange.SSE: "SH",
    Exchange.SZSE: "SZ",
}
EXCHANGE_FUTU2VT = {v: k for k, v in EXCHANGE_VT2FUTU.items()}

# QMT交易所映射 不支持US
QMT_EXCHANGE_MAP = {
    Exchange.SSE: 'SH',
    Exchange.SZSE: 'SZ',
    Exchange.SEHK: 'HK',  # 港股 港股通归入港股
    Exchange.SHHK: 'HGT',  # 沪港通
    Exchange.SZHK: 'SGT',  # 深港通
}
QMT_TO_VN_EXCHANGE = {v: k for k, v in QMT_EXCHANGE_MAP.items()}

# 交易所到账号类型映射
EXCHANGE_TO_ACCOUNT_TYPE = {
    Exchange.SSE: 'STOCK',      # 上交所A股
    Exchange.SZSE: 'STOCK',     # 深交所A股
    Exchange.SEHK: 'HUGANGTONG', # 港股（默认使用港股通）
    Exchange.SHHK: 'HUGANGTONG', # 沪港通
    Exchange.SZHK: 'HUGANGTONG', # 深港通（默认使用港股通）
}

# QMT交易类型映射
QMT_TRADE_TYPE = {
    Direction.LONG: xtconstant.STOCK_BUY,
    Direction.SHORT: xtconstant.STOCK_SELL,
}
QMT_TO_VN_TRADE_TYPE = {v: k for k, v in QMT_TRADE_TYPE.items()}

# QMT订单状态映射
QMT_ORDER_STATUS = {
    49: Status.SUBMITTING,
    50: Status.NOTTRADED,
    55: Status.PARTTRADED,
    56: Status.ALLTRADED,
    54: Status.CANCELLED,
    57: Status.REJECTED,
}

# 产品类型映射
QMT_PRODUCT_MAP = {
    'STOCK': Product.EQUITY,
    'ETF': Product.ETF,
    'BOND': Product.BOND,
    'INDEX': Product.INDEX,
}

CHINA_TZ = ZoneInfo("Asia/Shanghai")

def convert_symbol_vt2futu(symbol: str, exchange: Exchange) -> str:
    """将vnpy合约名称转换为富途格式"""
    futu_exchange = EXCHANGE_VT2FUTU.get(exchange, exchange.value)
    return f"{futu_exchange}.{symbol}"

def convert_symbol_futu2vt(code: str) -> tuple:
    """将富途合约名称转换为vnpy格式"""
    code_list = code.split(".")
    futu_exchange = code_list[0]
    futu_symbol = ".".join(code_list[1:])
    exchange = EXCHANGE_FUTU2VT.get(futu_exchange, Exchange.LOCAL)
    return futu_symbol, exchange

def convert_symbol_vt2qmt(symbol: str, exchange: Exchange, market: str) -> str:
    """将vnpy合约名称转换为QMT格式"""
    qmt_exchange = QMT_EXCHANGE_MAP.get(exchange)
    # qmt为国内券商提供服务，需要将港股映射为沪港通或深港通
    if market == 'HUGANGTONG' and (exchange == Exchange.SEHK or exchange == Exchange.SHHK):
        qmt_exchange = 'HGT'
    if market == 'SHENGANGTONG' and (exchange == Exchange.SEHK or exchange == Exchange.SZHK):
        qmt_exchange = 'SGT'
    if not qmt_exchange:
        raise ValueError(f"不支持的交易所: {exchange}")
    return f"{symbol}.{qmt_exchange}"

def convert_symbol_qmt2vt(code: str, market: str = None) -> tuple:
    """将QMT合约名称转换为vnpy格式"""
    symbol, suffix = code.rsplit('.')
    
    # 如果market参数未提供，从代码后缀自动推断
    if market is None:
        if suffix in ['SH', 'SZ']:
            market = 'STOCK'
        elif suffix in ['HK', 'HGT', 'SGT']:
            market = 'HUGANGTONG'
        else:
            market = 'STOCK'  # 默认
    
    # 根据market参数调整交易所映射
    if market == 'HUGANGTONG' and suffix == 'HK':
        # 港股通交易，HK后缀映射到SHHK
        exchange = Exchange.SHHK
    else:
        exchange = QMT_TO_VN_EXCHANGE.get(suffix)
    
    if not exchange:
        raise ValueError(f"不支持的QMT交易所: {suffix}")
    return symbol, exchange

def timestamp_to_datetime(timestamp: int) -> datetime:
    """时间戳转换为datetime"""
    return datetime.fromtimestamp(timestamp / 1000)

class FutuGateway(BaseGateway):
    """富途行情Gateway - 专注于行情推送"""
    
    default_name = "FUTU"
    default_setting = {
        "host": "127.0.0.1",
        "port": 11111,
    }
    exchanges = [Exchange.SEHK, Exchange.SSE, Exchange.SZSE, Exchange.SMART]

    def __init__(self, event_engine, name="FUTU"):
        super().__init__(event_engine, name)
        self.quote_ctx = None
        self.ticks: Dict[str, TickData] = {}

    def connect(self, setting: dict):
        """连接行情接口"""
        try:
            self.quote_ctx = ft.OpenQuoteContext(host=setting["host"], port=setting["port"])
            
            # 设置行情推送处理器
            class QuoteHandler(ft.StockQuoteHandlerBase):
                gateway = self
                
                def on_recv_rsp(self, rsp_str):
                    ret_code, content = super(QuoteHandler, self).on_recv_rsp(rsp_str)
                    if ret_code != ft.RET_OK:
                        return ft.RET_ERROR, content
                    self.gateway.process_quote(content)
                    return ft.RET_OK, content

            self.quote_ctx.set_handler(QuoteHandler())
            self.quote_ctx.start()
            
            self.write_log("富途行情Gateway连接成功")
            
        except Exception as e:
            self.write_log(f"富途行情Gateway连接失败: {e}")

    def subscribe(self, req: SubscribeRequest):
        """订阅行情"""

        if req.exchange not in self.exchanges:
            self.write_log(f"futu不支持订阅行情: {req.symbol} {req.exchange}")
            return
        
        futu_symbol = convert_symbol_vt2futu(req.symbol, req.exchange)
        
        code, data = self.quote_ctx.subscribe(futu_symbol, "QUOTE", True)
        if code:
            self.write_log(f"订阅行情失败：{data}")
        else:
            self.write_log(f"订阅行情成功: {futu_symbol}")

    def get_tick(self, code: str) -> TickData:
        """获取或创建Tick数据"""
        tick = self.ticks.get(code, None)
        symbol, exchange = convert_symbol_futu2vt(code)
        
        if not tick:
            tick = TickData(
                symbol=symbol,
                exchange=exchange,
                datetime=datetime.now(CHINA_TZ),
                gateway_name=self.gateway_name,
            )
            self.ticks[code] = tick
        
        return tick

    def process_quote(self, data):
        """处理行情推送"""
        for _, row in data.iterrows():
            symbol = row["code"]
            
            # 解析时间
            date = row["data_date"].replace("-", "")
            time_str = row["data_time"]
            if '.' in time_str:
                time_str = time_str.split('.')[0]
            dt = datetime.strptime(f"{date} {time_str}", "%Y%m%d %H:%M:%S")
            dt = dt.replace(tzinfo=CHINA_TZ)
            
            # 获取或创建Tick数据
            tick = self.get_tick(symbol)
            tick.datetime = dt
            tick.open_price = row["open_price"]
            tick.high_price = row["high_price"]
            tick.low_price = row["low_price"]
            tick.pre_close = row["prev_close_price"]
            tick.last_price = row["last_price"]
            tick.volume = row["volume"]
            
            # 设置涨跌停价格
            if "price_spread" in row:
                spread = row["price_spread"]
                tick.limit_up = tick.last_price + spread * 10
                tick.limit_down = tick.last_price - spread * 10
            
            # 推送Tick数据
            self.on_tick(copy(tick))
            print(f'{tick.datetime} futu Tick: {symbol} {tick.name} {tick.last_price}')

    def send_order(self, req: OrderRequest):
        """富途Gateway不支持交易"""
        self.write_log("富途Gateway仅支持行情数据，不支持交易")
        return ""

    def cancel_order(self, req):
        """富途Gateway不支持交易"""
        self.write_log("富途Gateway仅支持行情数据，不支持交易")

    def query_account(self):
        """富途Gateway不支持交易"""
        self.write_log("富途Gateway仅支持行情数据，不支持交易")

    def query_position(self):
        """富途Gateway不支持交易"""
        self.write_log("富途Gateway仅支持行情数据，不支持交易")

    def close(self):
        """关闭连接"""
        if self.quote_ctx:
            self.quote_ctx.close()
            self.write_log("富途行情Gateway已关闭")

class QmtGateway(BaseGateway, XtQuantTraderCallback):
    """QMT交易Gateway - 专注于A股和港股通交易"""
    
    default_setting = {
        "path": "C:/Users/Administrator/mini_qmt/",
        "session_id": 123456,
        "account_id": "YOUR_ACCOUNT_ID",
    }
    exchanges = [Exchange.SSE, Exchange.SZSE, Exchange.SHHK, Exchange.SZHK, Exchange.SEHK]
    TRADE_TYPE = (Product.ETF, Product.EQUITY, Product.BOND, Product.INDEX)

    def __init__(self, event_engine, name="QMT"):
        super().__init__(event_engine, name)
        self.contracts: Dict[str, ContractData] = {}
        self.trader: XtQuantTrader = None
        self.accounts: Dict[str, StockAccount] = {}
        self.orders: Dict[str, OrderData] = {}
        self.trades: Dict[str, TradeData] = {}
        self.limit_ups = {}
        self.limit_downs = {}
        self.count = -1
        self.session_id = int(datetime.now().strftime('%H%M%S'))
        self.inited = False
        self.account_id = None
        
        # 注册定时事件
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def connect(self, setting: dict):
        """连接交易接口"""
        try:
            account = setting['account_id']
            path = setting['path']
            self.account_id = account
            
            self.trader = XtQuantTrader(path=path, session=self.session_id)
            self.trader.register_callback(self)
            self.trader.start()
            
            # 连接
            cnn_msg = self.trader.connect()
            if cnn_msg == 0:
                self.write_log('QMT连接成功')
            else:
                self.write_log(f'QMT连接失败：{cnn_msg}')
                return
            
            account_types = ['STOCK', 'HUGANGTONG']
            for account_type in account_types:
                try:
                    account_obj = StockAccount(account, account_type)
                    sub_msg = self.trader.subscribe(account=account_obj)
                    if sub_msg == 0:
                        self.accounts[account_type] = account_obj
                        self.write_log(f'订阅{account_type}账户成功')
                    else:
                        self.write_log(f'订阅{account_type}账户失败：{sub_msg}')
                except Exception as e:
                    self.write_log(f'创建{account_type}账户失败：{e}')
            
            if self.accounts:
                self.inited = True
                self.write_log(f'成功初始化{len(self.accounts)}个账户类型')
            else:
                self.write_log('未能成功初始化任何账户')
                return
            
            # 获取合约信息
            self._get_contracts()
            
        except Exception as e:
            self.write_log(f"QMT连接异常: {e}")

    def _get_account_by_exchange(self, exchange: Exchange) -> StockAccount:
        """根据交易所获取对应的账号对象"""
        account_type = EXCHANGE_TO_ACCOUNT_TYPE.get(exchange, 'STOCK')
        account = self.accounts.get(account_type)
        if not account:
            account = self.accounts.get('STOCK')
            if not account:
                raise ValueError(f"未找到适合交易所{exchange}的账号")
        return account

    def subscribe(self, req: SubscribeRequest):
        """订阅行情"""
        try:
            if req.exchange not in self.exchanges:
                self.write_log(f"qmt不支持订阅行情: {req.symbol} {req.exchange}")
                return
            
            account_type = EXCHANGE_TO_ACCOUNT_TYPE.get(req.exchange, 'STOCK')
            qmt_code = convert_symbol_vt2qmt(req.symbol, req.exchange, account_type)
            return xtquant.xtdata.subscribe_quote(
                stock_code=qmt_code,
                period='tick',
                callback=self._on_tick
            )
        except Exception as e:
            self.write_log(f"订阅行情失败: {e}")

    def send_order(self, req: OrderRequest):
        """发送订单"""
        try:
            if req.exchange not in self.exchanges:
                self.write_log(f"qmt不支持交易: {req.symbol} {req.exchange}")
                return
            
            # 根据交易所获取对应的账号
            account = self._get_account_by_exchange(req.exchange)
            account_type = EXCHANGE_TO_ACCOUNT_TYPE.get(req.exchange, 'STOCK')
            
            order_id = self._get_order_id()
            
            # 发送异步订单
            qmt_code = convert_symbol_vt2qmt(req.symbol, req.exchange, account_type)
            seq = self.trader.order_stock_async(
                account=account,
                stock_code=qmt_code,
                order_type=QMT_TRADE_TYPE[req.direction],
                price_type=xtconstant.FIX_PRICE if req.type == OrderType.LIMIT else xtconstant.MARKET_PEER_PRICE_FIRST,
                order_volume=int(req.volume),
                price=req.price,
                order_remark=order_id,
            )

            if seq == -1:
                raise Exception(f"{qmt_code} {order_id}")
            
            # 创建订单对象
            order = OrderData(
                gateway_name=self.gateway_name,
                symbol=req.symbol,
                exchange=req.exchange,
                orderid=order_id,
                type=req.type,
                direction=req.direction,
                offset=req.offset,
                volume=req.volume,
                price=req.price,
                status=Status.SUBMITTING
            )
            self.orders[order_id] = order
            self.write_log(f"使用{account_type}账号发送订单: {req.symbol} {req.exchange}")
            return order.vt_orderid
            
        except Exception as e:
            self.write_log(f"发送订单失败: {e}")
            return ""

    def cancel_order(self, req: CancelRequest):
        """撤销订单"""
        try:
            order = self.orders.get(req.orderid)
            if order and hasattr(order, 'reference'):
                account = self._get_account_by_exchange(order.exchange)
                return self.trader.cancel_order_stock_async(
                    account=account, 
                    order_id=order.reference
                )
        except Exception as e:
            self.write_log(f"撤销订单失败: {e}")

    def query_account(self):
        """查询账户信息"""
        if self.trader and self.accounts:
            for account_type, account in self.accounts.items():
                self.trader.query_stock_asset_async(account, callback=self._on_account)

    def query_position(self):
        """查询持仓信息"""
        if self.trader and self.accounts:
            for account_type, account in self.accounts.items():
                self.trader.query_stock_positions_async(account, callback=self._on_positions)

    def query_order(self):
        """查询订单信息"""
        if self.trader and self.accounts:
            for account_type, account in self.accounts.items():
                self.trader.query_stock_orders_async(account, callback=self._on_orders)

    def query_trade(self):
        """查询成交信息"""
        if self.trader and self.accounts:
            for account_type, account in self.accounts.items():
                self.trader.query_stock_trades_async(account, callback=self._on_trades)

    def process_timer_event(self, event):
        """定时事件处理"""
        if not self.inited:
            return
            
        if self.count == -1:
            self.query_trade()
        self.count += 1

        if self.count % 5 == 0:
            self.query_order()

        if self.count % 7 == 0:
            self.query_account()
            self.query_position()
            
        if self.count < 21:
            return
        self.count = 0

    def _get_contracts(self):
        """获取合约信息"""
        self.write_log('开始获取合约信息')
        contract_ids = set()
        sectors = ['上证A股', '深证A股', '科创板', '创业板', '沪市ETF', '深市ETF']
        
        for sector in sectors:
            try:
                stock_list = xtquant.xtdata.get_stock_list_in_sector(sector_name=sector)
                for symbol in stock_list:
                    if symbol in contract_ids:
                        continue
                    contract_ids.add(symbol)
                    
                    info = xtquant.xtdata.get_instrument_detail(symbol)
                    contract_type = xtquant.xtdata.get_instrument_type(symbol)
                    
                    if info is None or contract_type is None:
                        continue
                    
                    try:
                        exchange = QMT_TO_VN_EXCHANGE[info['ExchangeID']]
                    except KeyError:
                        continue
                    
                    if exchange not in self.exchanges:
                        continue
                    
                    product = QMT_PRODUCT_MAP.get(contract_type, Product.EQUITY)
                    if product not in self.TRADE_TYPE:
                        continue

                    contract = ContractData(
                        gateway_name=self.gateway_name,
                        symbol=info['InstrumentID'],
                        exchange=exchange,
                        name=info['InstrumentName'],
                        product=product,
                        pricetick=info['PriceTick'],
                        size=100,
                        min_volume=100
                    )
                    
                    self.limit_ups[contract.vt_symbol] = info['UpStopPrice']
                    self.limit_downs[contract.vt_symbol] = info['DownStopPrice']
                    self.contracts[contract.vt_symbol] = contract
                    self.on_contract(contract)
                    
            except Exception as e:
                self.write_log(f"获取{sector}合约信息失败: {e}")
        
        self.write_log(f'获取合约信息完成，共{len(self.contracts)}个合约')

    def _get_order_id(self):
        """生成订单ID"""
        self.count += 1
        return f'{self.session_id}#{self.count}'

    def _on_tick(self, datas):
        """行情推送回调"""
        for code, data_list in datas.items():
            try:
                symbol, exchange = convert_symbol_qmt2vt(code)
                for data in data_list:
                    dt = timestamp_to_datetime(data['time']).replace(tzinfo=CHINA_TZ)
                    
                    tick = TickData(
                        gateway_name=self.gateway_name,
                        symbol=symbol,
                        exchange=exchange,
                        datetime=dt,
                        last_price=data['lastPrice'],
                        volume=data['volume'],
                        open_price=data['open'],
                        high_price=data['high'],
                        low_price=data['low'],
                        pre_close=data['lastClose'],
                        ask_price_1=data['askPrice'][0],
                        ask_price_2=data['askPrice'][1],
                        ask_price_3=data['askPrice'][2],
                        ask_price_4=data['askPrice'][3],
                        ask_price_5=data['askPrice'][4],
                        ask_volume_1=data['askVol'][0],
                        ask_volume_2=data['askVol'][1],
                        ask_volume_3=data['askVol'][2],
                        ask_volume_4=data['askVol'][3],
                        ask_volume_5=data['askVol'][4],
                        bid_price_1=data['bidPrice'][0],
                        bid_price_2=data['bidPrice'][1],
                        bid_price_3=data['bidPrice'][2],
                        bid_price_4=data['bidPrice'][3],
                        bid_price_5=data['bidPrice'][4],
                        bid_volume_1=data['bidVol'][0],
                        bid_volume_2=data['bidVol'][1],
                        bid_volume_3=data['bidVol'][2],
                        bid_volume_4=data['bidVol'][3],
                        bid_volume_5=data['bidVol'][4],
                    )
                    
                    # 设置合约名称和涨跌停价格
                    contract = self.contracts.get(tick.vt_symbol)
                    if contract:
                        tick.name = contract.name
                    tick.limit_up = self.limit_ups.get(tick.vt_symbol, 0)
                    tick.limit_down = self.limit_downs.get(tick.vt_symbol, 0)
                    
                    self.on_tick(tick)
                    
            except Exception as e:
                self.write_log(f"处理行情数据失败: {e}")

    def _on_account(self, asset: XtAsset):
        """账户信息回调"""
        account = AccountData(
            accountid=asset.account_id,
            frozen=asset.frozen_cash,
            balance=asset.total_asset,
            gateway_name=self.gateway_name
        )
        self.on_account(account)

    def _on_positions(self, pos_list: list[XtPosition]):
        """持仓信息回调"""
        for pos in pos_list:
            try:
                symbol, exchange = convert_symbol_qmt2vt(pos.stock_code)
                position = PositionData(
                    gateway_name=self.gateway_name,
                    symbol=symbol,
                    exchange=exchange,
                    direction=Direction.LONG,
                    volume=pos.volume,
                    yd_volume=pos.yesterday_volume,
                    price=pos.open_price,
                    pnl=pos.market_value - pos.volume * pos.open_price
                )
                self.on_position(position)
            except Exception as e:
                self.write_log(f"处理持仓信息失败: {e}")

    def _on_orders(self, order_list: list[XtOrder]):
        """订单信息回调"""
        for order in order_list:
            try:
                symbol, exchange = convert_symbol_qmt2vt(order.stock_code)
                vn_order = OrderData(
                    orderid=order.order_remark,
                    symbol=symbol,
                    exchange=exchange,
                    price=order.price,
                    volume=order.order_volume,
                    traded=order.traded_volume,
                    gateway_name=self.gateway_name,
                    status=QMT_ORDER_STATUS.get(order.order_status, Status.SUBMITTING),
                    direction=QMT_TO_VN_TRADE_TYPE.get(order.order_type, Direction.LONG),
                    datetime=timestamp_to_datetime(order.order_time),
                    reference=order.order_id
                )
                
                # 检查订单状态变化
                old_order = self.orders.get(vn_order.orderid)
                if old_order:
                    old_status = old_order.status
                    old_traded = old_order.traded
                else:
                    old_status = None
                    old_traded = 0
                
                self.orders[vn_order.orderid] = vn_order
                self.on_order(vn_order)
                
                # 如果有成交变化，输出成交信息
                if vn_order.traded > old_traded and old_status != Status.ALLTRADED:
                    new_traded = vn_order.traded - old_traded
                    self.write_log(f'订单成交: {vn_order.symbol} {vn_order.exchange} {vn_order.direction} '
                                f'成交{new_traded}股，累计{vn_order.traded}/{vn_order.volume}股')
                
                # 如果订单完成，输出完成信息
                if vn_order.status == Status.ALLTRADED:
                    self.write_log(f'✅ 订单完成: {vn_order.symbol} {vn_order.exchange} {vn_order.direction} '
                                    f'全部成交{vn_order.traded}股')
                elif vn_order.status == Status.CANCELLED:
                    self.write_log(f'⏹️ 订单撤销: {vn_order.symbol} {vn_order.exchange} {vn_order.direction} '
                                    f'已撤销，成交{vn_order.traded}股')
                elif vn_order.status == Status.REJECTED:
                    self.write_log(f'❌ 订单拒绝: {vn_order.symbol} {vn_order.exchange} {vn_order.direction} '
                                    f'被拒绝')

            except Exception as e:
                self.write_log(f"处理订单信息失败: {e}")

    def _on_trades(self, trade_list: list[XtTrade]):
        """成交信息回调"""
        for trade in trade_list:
            try:
                symbol, exchange = convert_symbol_qmt2vt(trade.stock_code)
                if not trade.order_remark:
                    continue
                    
                trade_data = TradeData(
                    gateway_name=self.gateway_name,
                    symbol=symbol,
                    exchange=exchange,
                    orderid=trade.order_remark,
                    tradeid=trade.traded_id,
                    price=trade.traded_price,
                    datetime=timestamp_to_datetime(trade.traded_time),
                    volume=trade.traded_volume,
                    direction=QMT_TO_VN_TRADE_TYPE.get(trade.order_type, Direction.LONG)
                )
                self.on_trade(trade_data)
            except Exception as e:
                self.write_log(f"处理成交信息失败: {e}")

    # XtQuantTraderCallback 回调方法
    def on_order_stock_async_response(self, response: XtOrderResponse):
        """异步下单响应"""
        self.write_log(f'下单响应: {response.order_remark} - {response.error_msg or "成功"}')
        order = self.orders.get(response.order_remark)
        if order and response.error_msg:
            order.status = Status.REJECTED
            self.on_order(order)

    def on_cancel_order_stock_async_response(self, response: XtCancelOrderResponse):
        """异步撤单响应"""
        self.write_log(f'撤单结果: {response.cancel_result}')

    def on_order_error(self, order_error: XtOrderError):
        """订单错误回调"""
        self.write_log(f'订单错误: {order_error.error_msg}')
        order = self.orders.get(order_error.order_remark)
        if order:
            order.status = Status.REJECTED
            self.on_order(order)

    def on_cancel_error(self, cancel_error: XtCancelError):
        """撤单错误回调"""
        self.write_log(f'撤单错误: {cancel_error.error_msg}')

    def close(self):
        """关闭连接"""
        if self.trader:
            self.trader.stop()
            self.write_log("QMT交易Gateway已关闭")
