import configparser
import time

from futu import RET_OK, ModifyUserSecurityOp, OpenQuoteContext
from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount

from ft_config import get_config
def sync_qmt_futu_position(group_name:str, config:configparser.ConfigParser, clear_group:bool=True):
    # 获取qmt持仓 同步到futu group
    session_id = int(time.time())
    path = config.get("CONFIG", "QMT_PATH")
    account_id = config.get("CONFIG", "QMT_ACCOUNT_ID")
    xt_trader = XtQuantTrader(path, session_id)
    # 启动交易线程
    xt_trader.start()
    # 建立交易连接，返回0表示连接成功
    connect_result = xt_trader.connect()
    if connect_result != 0:
        raise Exception(f"建立交易连接失败: {connect_result}")
    
    # a股账户
    a_stock_acc = StockAccount(account_id, 'STOCK')
    subscribe_result = xt_trader.subscribe(account=a_stock_acc)
    if subscribe_result != 0:
        raise Exception(f"订阅账户失败: {subscribe_result}")
    positions = xt_trader.query_stock_positions(a_stock_acc)
    #取各品种 总持仓 可用持仓
    a_stock_position_total_dict = {i.stock_code : i.m_nVolume for i in positions}
    print(a_stock_acc.account_id, 'a股持仓字典', a_stock_position_total_dict)

    # 沪港通账户
    hk_stock_acc = StockAccount(account_id, 'HUGANGTONG')
    subscribe_result = xt_trader.subscribe(account=hk_stock_acc)
    if subscribe_result != 0:
        raise Exception(f"订阅账户失败: {subscribe_result}")
    positions = xt_trader.query_stock_positions(hk_stock_acc)
    #取各品种 总持仓 可用持仓
    hk_stock_position_total_dict = {i.stock_code : i.m_nVolume for i in positions}
    print(hk_stock_acc.account_id, '沪港通持仓字典', hk_stock_position_total_dict)

    code_list = ['.'.join(reversed(qmt_code.split('.'))) for qmt_code in list(a_stock_position_total_dict.keys())]
    code_list.extend(['HK.' + qmt_code.split('.')[0] for qmt_code in list(hk_stock_position_total_dict.keys())])
    
    # 获取futu group
    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    quote_ctx = OpenQuoteContext(host=host, port=port)
    ret, data = quote_ctx.get_user_security(group_name)
    if ret == RET_OK:
        if clear_group:
            old_code_list = list(data['code'])
            if old_code_list:
                print(f'清空{group_name}')
                quote_ctx.modify_user_security(group_name, ModifyUserSecurityOp.MOVE_OUT, old_code_list)
        quote_ctx.modify_user_security(group_name, ModifyUserSecurityOp.ADD, code_list)
    else:
        print(f'获取{group_name}失败 {data}')
    quote_ctx.close()
    xt_trader.stop()
    return code_list
if __name__ == "__main__":
    config = get_config('./env/trade.ini')
    sync_qmt_futu_position("QMT", config)
