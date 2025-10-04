import configparser
import time
import os
from futu import RET_OK, ModifyUserSecurityOp, OpenQuoteContext
from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount
from tools import ib_code_to_futu_code

from ib_insync import *
import asyncio

from ft_config import get_config
def sync_qmt_futu_position(config:configparser.ConfigParser):
    # 获取qmt持仓 同步到futu group
    session_id = int(time.time())
    path = config.get("CONFIG", "QMT_PATH")
    account_id = config.get("CONFIG", "QMT_ACCOUNT_ID")

    if not os.path.exists(path) or not account_id:
        print("QMT路径或账户ID不存在")
        return []

    xt_trader = XtQuantTrader(path, session_id)
    # 启动交易线程
    xt_trader.start()
    # 建立交易连接，返回0表示连接成功
    connect_result = xt_trader.connect()
    if connect_result != 0:
        raise Exception(f"建立交易连接失败: {connect_result}")
    
    code_list = []
    
    # a股账户
    a_stock_acc = StockAccount(account_id, 'STOCK')
    subscribe_result = xt_trader.subscribe(account=a_stock_acc)
    if subscribe_result != 0:
        raise Exception(f"订阅账户失败: {subscribe_result}")
    positions = xt_trader.query_stock_positions(a_stock_acc)
    
    for position in positions:
        if position.m_nVolume > 0:
            code_list.append('.'.join(reversed(position.stock_code.split('.'))))
            print(position.stock_code, position.m_nVolume)

    # 沪港通账户
    hk_stock_acc = StockAccount(account_id, 'HUGANGTONG')
    subscribe_result = xt_trader.subscribe(account=hk_stock_acc)
    if subscribe_result != 0:
        raise Exception(f"订阅账户失败: {subscribe_result}")
    positions = xt_trader.query_stock_positions(hk_stock_acc)
    
    for position in positions:
        if position.m_nVolume > 0:
            code_list.append('HK.' + position.stock_code.split('.')[0])
            print(position.stock_code, position.m_nVolume)
    
    xt_trader.stop()
    return code_list

async def sync_ibkr_futu_position(config:configparser.ConfigParser):
    # 获取ibkr持仓 同步到futu group
    try:
        ib = IB()

        host = config.get("CONFIG", "IBKR_HOST")
        port = int(config.get("CONFIG", "IBKR_PORT"))
        if not host or not port:
            print("IBKR主机或端口不存在")
            return []
        await ib.connectAsync(host, port, clientId=1)
        print("成功连接到 IB Gateway！")

        # 异步请求持仓
        positions = await ib.reqPositionsAsync()

        # 打印每个持仓的详细信息
        if positions:
            print("\n持仓信息:")
            for position in positions:
                print(f"账户: {position.account}, "
                    f"合约: {position.contract.symbol}, "
                    f"数量: {position.position}, "
                    f"平均成本: {position.avgCost}")
        else:
            print("当前没有持仓。")
            return []
    
    except Exception as e:
        print(f"获取持仓信息失败: {e}")
        return []
    
    finally:
        ib.disconnect()

    return [ib_code_to_futu_code(position.contract) for position in positions]

if __name__ == "__main__":
    config = get_config('./env/trade.ini')
    qmt_code_list = sync_qmt_futu_position(config)
    ibkr_code_list = asyncio.run(sync_ibkr_futu_position(config))

    # 获取futu group
    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    group_name = config.get("CONFIG", "FUTU_GROUP")
    
    clear_group = True
    
    quote_ctx = OpenQuoteContext(host=host, port=port)
    ret, data = quote_ctx.get_user_security(group_name)
    if ret == RET_OK:
        if clear_group:
            old_code_list = list(data['code'])
            if old_code_list:
                print(f'清空{group_name}')
                quote_ctx.modify_user_security(group_name, ModifyUserSecurityOp.MOVE_OUT, old_code_list)
        quote_ctx.modify_user_security(group_name, ModifyUserSecurityOp.ADD, qmt_code_list + ibkr_code_list)
    else:
        print(f'获取{group_name}失败 {data}')
    quote_ctx.close()
