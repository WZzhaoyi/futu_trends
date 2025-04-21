import json
import os
from ft_config import get_config
from data import get_kline_data
from signal_analysis import detect_stochastic_signals_vectorized
from tools import *
import datetime
import configparser
from notification_engine import NotificationEngine
from decimal import Decimal, ROUND_HALF_UP
import sqlite3
import pandas as pd

def round_decimal(value, places=2):
    """
    将浮点数转换为 Decimal 并四舍五入到指定小数位数
    """
    if isinstance(value, float):
        value = str(value)  # 将 float 转换为字符串以避免精度损失
    return Decimal(value).quantize(Decimal(f'0.{"0" * places}'), rounding=ROUND_HALF_UP)

def inside_MA(close, last_low, last_high): # 计算MA5,10,15中的最小值和最大值 对比是否区间重叠
    last_index = close.size - 1
    ma5 = round_decimal(MA(close,5)[last_index])
    ma10 = round_decimal(MA(close,10)[last_index])
    ma15 = round_decimal(MA(close,15)[last_index])

    min_ma = min(ma5, ma10, ma15)
    max_ma = max(ma5, ma10, ma15)

    if max_ma < last_low or min_ma > last_high: # 如果第一个区间的结束小于第二个区间的开始，或者第一个区间的开始大于第二个区间的结束，说明不重叠
        return False
    else:
        return True

def is_reverse(code: str, df: pd.DataFrame | None, config: configparser.ConfigParser) -> str | None:
    """检查是否出现反转信号"""
    assert len(df) >= 90
    
    # 获取参数 - 优先从数据库读取，失败则从JSON读取
    try:
        db_path = config.get("CONFIG", "KD_PARAMS_DB", fallback=None)
        if db_path and os.path.exists(db_path):
            with sqlite3.connect(db_path) as conn:
                result = conn.execute("""
                    SELECT best_params FROM stock_params WHERE stock_code = ?
                """, (code,)).fetchone()
                
                if result:
                    params = json.loads(result[0])
                else:
                    # 回退到JSON文件
                    params_file = config.get("CONFIG", "KD_PARAMS")
                    with open(params_file, 'r') as f:
                        params = json.load(f).get(code, {}).get('best_params')
        else:
            # 直接读取JSON文件
            params_file = config.get("CONFIG", "KD_PARAMS")
            with open(params_file, 'r') as f:
                params = json.load(f).get(code, {}).get('best_params')
                
        if not params:
            print(f"No parameters found for {code}")
            return 'No parameters'
            
    except Exception as e:
        print(f"Error reading parameters for {code}: {str(e)}")
        return 'Parameter error'
    
    # 使用参数进行信号检测
    result = detect_stochastic_signals_vectorized(
        df,
        k_period=params['k_period'],
        d_period=params['d_period'],
        overbought=params['overbought'],
        oversold=params['oversold'],
        support_ma_period=params['support_ma_period'],
        resistance_ma_period=params['resistance_ma_period'],
        atr_period_explicit=params['atr_period_explicit'],
        atr_period_hidden=params['atr_period_hidden'],
        strength_threshold=params['strength_threshold']
    )
    
    # 获取最后一行的信号
    last_row = result.iloc[-1]
    reversal =  last_row['reversal']
    is_strong =  last_row['is_strong']
    
    # 检查是否有反转信号
    msg = ''
    if reversal != 'none':
        msg += reversal
    if is_strong == 1:
        msg += u'🚨'
    return None if msg == '' else msg

def is_continue(data:pd.DataFrame)->str|None:# 检查macd趋势延续/低位金叉/高位死叉
    assert len(data) >= 26
    # 计算MACD
    dif, dea = MACD(data['close'], 12, 26, 9)
    data['DIF'] = dif
    data['DEA'] = dea
    
    # 获取交叉状态
    crossover = crossover_status(data['DIF'], data['DEA'])
    golden_crosses = [i for i, c in enumerate(crossover) if c == 1]  # 金叉索引
    dead_crosses = [i for i, c in enumerate(crossover) if c == -1]  # 死叉索引
    
    msg = ''
    last_row_pos = len(data) - 1
    
    # 检测趋势延续信号
    if golden_crosses and golden_crosses[-1] == last_row_pos and len(golden_crosses) > 1:
        # 找到前一次金叉之后、当前金叉之前的死叉
        prev_gc = golden_crosses[-2]
        prev_dc = next((dc for dc in dead_crosses if prev_gc < dc < last_row_pos), None)
        
        if prev_dc is not None:
            # 检查前一次死叉到当前金叉之间DEA是否都大于0
            if data['DEA'].iloc[prev_dc:last_row_pos+1].min() > 0:
                msg += '上升延续'
    
    if dead_crosses and dead_crosses[-1] == last_row_pos and len(dead_crosses) > 1:
        # 找到前一次死叉之后、当前死叉之前的金叉
        prev_dc = dead_crosses[-2]
        prev_gc = next((gc for gc in golden_crosses if prev_dc < gc < last_row_pos), None)
        
        if prev_gc is not None:
            # 检查前一次金叉到当前死叉之间DEA是否都小于0
            if data['DEA'].iloc[prev_gc:last_row_pos+1].max() < 0:
                msg += '下降延续'
    
    # 检测低位金叉和高位死叉
    if golden_crosses and golden_crosses[-1] == last_row_pos:
        dif_high_threshold = data['DIF'].quantile(0.4)
        dea_high_threshold = data['DEA'].quantile(0.4)
        
        if data['DIF'].iloc[last_row_pos] <= dif_high_threshold and data['DEA'].iloc[last_row_pos] <= dea_high_threshold:
            msg += '低位金叉🚨'
    
    if dead_crosses and dead_crosses[-1] == last_row_pos:
        dif_low_threshold = data['DIF'].quantile(0.6)
        dea_low_threshold = data['DEA'].quantile(0.6)
        
        if data['DIF'].iloc[last_row_pos] >= dif_low_threshold and data['DEA'].iloc[last_row_pos] >= dea_low_threshold:
            msg += '高位死叉🚨'
    
    return None if msg == '' else msg

def is_breakout(high, low, close, N:int=10)->str|None:# K线突破/跌破均线
    close_ema = EMA(close, N)
    last_close = round_decimal(close.iloc[-1])
    last_ema = round_decimal(close_ema[-1])
    prev_close = round_decimal(close.iloc[-2])
    prev_ema = round_decimal(close_ema[-2])
    if last_close > last_ema and prev_close <= prev_ema:
        return f'突破ema{N}'
    if last_close < last_ema and prev_close >= prev_ema:
        return f'跌破ema{N}'
    return None

def is_top_down(data:pd.DataFrame) -> str|None:# KDJ顶部和底部信号/背离
    assert len(data) >= 40
    # 计算KDJ
    k,d,j = KDJ(data['close'], data['high'], data['low'])
    data['K'] = k
    data['D'] = d
    data['J'] = j
    
    j_values = data['J']
    msg = ''

    if j_values.iloc[-1] <= 100 and all(j > 100 for j in j_values[-4:-2]):
        msg += f'顶消失'
    # 底消失信号
    elif j_values.iloc[-1] >= 0 and all(j < 0 for j in j_values[-4:-2]):
        msg += f'底消失'
    
    # 背离检测
    # KDJ背离
    crossover = crossover_status(data['K'], data['D'])
    golden_crosses = [i for i, c in enumerate(crossover) if c == 1]  # 金叉索引
    dead_crosses = [i for i, c in enumerate(crossover) if c == -1]  # 死叉索引
    kdj_divergence = detect_divergence(data['K'], data['D'], data['close'], golden_crosses, dead_crosses)
    kdj_div_value = kdj_divergence.iloc[-1]
    
    # MACD背离
    dif, dea = MACD(data['close'], 12, 26, 9)
    data['DIF'] = dif
    data['DEA'] = dea
    macd_crossover = crossover_status(data['DIF'], data['DEA'])
    macd_golden_crosses = [i for i, c in enumerate(macd_crossover) if c == 1]  # 金叉索引
    macd_dead_crosses = [i for i, c in enumerate(macd_crossover) if c == -1]  # 死叉索引
    macd_divergence = detect_divergence(data['DIF'], data['DEA'], data['close'], macd_golden_crosses, macd_dead_crosses)
    macd_div_value = macd_divergence.iloc[-1]
    
    # 综合背离信号
    if kdj_div_value == 1 or macd_div_value == 1:
        msg += '顶背离🚨'
    if kdj_div_value == -1 or macd_div_value == -1:
        msg += '底背离🚨'

    return None if msg == '' else msg

def is_balance(data: pd.DataFrame, M: int = 3, N: int = 5) -> str | None: # 量价关系平衡
    assert len(data) >= max(M*6, N*6)
    
    # 成交量变化率
    data['vol_change'] = data['volume'] / data['volume'].shift(1) - 1
    
    # K线实体绝对值
    data['entity'] = abs(data['close'] - data['open'])
    
    # K线实体变化率
    data['entity_change'] = data['entity'] / data['entity'].shift(1) - 1
    data.loc[data['entity'].shift(1) == 0, 'entity_change'] = 0  # 处理分母为0的情况
    
    data['ma_short'] = MA(data['close'], N)  # 短期均线
    data['ma_mid'] = MA(data['close'], N*2)  # 中期均线
    data['ma_long'] = MA(data['close'], N*3)  # 长期均线
    
    # 均线斜率
    data['ma_short_slope'] = (data['ma_short'] - data['ma_short'].shift(1)) / data['ma_short'].shift(1)
    data['ma_mid_slope'] = (data['ma_mid'] - data['ma_mid'].shift(1)) / data['ma_mid'].shift(1)
    
    # 判断上涨/下跌趋势
    # 1. 短期均线在中期均线上方，且中期均线在长期均线上方
    # 2. 短期均线斜率为正
    data['up_trend'] = (data['ma_short'] > data['ma_mid']) & (data['ma_mid'] > data['ma_long']) & (data['ma_short_slope'] > 0)
    
    # 1. 短期均线在中期均线下方，且中期均线在长期均线下方
    # 2. 短期均线斜率为负
    data['down_trend'] = (data['ma_short'] < data['ma_mid']) & (data['ma_mid'] < data['ma_long']) & (data['ma_short_slope'] < 0)
    
    # 综合趋势判断
    data['trend'] = data['up_trend'] | data['down_trend']
    
    # 判断放量/缩量
    data['vol_up'] = data['vol_change'].shift(1).rolling(M-1).sum() >= M-1
    data['vol_down'] = (data['vol_change'].shift(1) < 0).rolling(M-1).sum() >= M-1
    
    # 获取最后一行的数据
    last_row = data.iloc[-1]
    prev_row = data.iloc[-2]
    
    # 初始化消息
    msg = ''
    
    # 检测成交量反转
    if ((last_row['vol_up'] and last_row['vol_change'] < 0 and last_row['trend']) or 
        (last_row['vol_down'] and last_row['vol_change'] > 0 and last_row['trend'])):
        msg += '成交量反转🚨'
    
    # 检测量价失衡
    if (last_row['entity_change'] < -0.4 and 
        last_row['vol_change'] > -0.1 and 
        last_row['trend']):
        msg += '量价失衡🚨'
    
    return None if msg == '' else msg

def check_trends(code_in_group, config: configparser.ConfigParser):
    """
    检查股票趋势并返回DataFrame格式的结果
    返回的DataFrame包含以下列：
    - name: 股票名称
    - msg: 趋势信息
    - momentum: 动量因子值
    """
    trend_type = config.get("CONFIG", "TREND_TYPE").split(',')
    if not (code_in_group.size and len(trend_type)):
        return pd.DataFrame(columns=['name', 'msg', 'momentum'])
        
    results = []
    for idx, futu_code in enumerate(code_in_group['code'].values):
        df = get_kline_data(futu_code, config)

        # 添加对 df 的检查
        if df is None:
            print(f"Warning: Failed to get data for {futu_code}")
            continue
            
        if len(df) == 0:
            print(f"Warning: Empty data for {futu_code}")
            continue

        high = pd.Series(df['high'].values.ravel())
        low = pd.Series(df['low'].values.ravel())
        close = pd.Series(df['close'].values.ravel())
        name = code_in_group['name'].iloc[idx]

        if len(high) == 0 or len(low) == 0 or len(close) == 0:
            print(f"Warning: No data for {futu_code}")
            continue

        msg = f'{futu_code} {name}'
        for i in trend_type:
            if i.lower() == 'breakout':
                bo = is_breakout(high,low,close) # 突破/跌破EMA均线
                if bo is not None:
                    msg += f' | {bo}'
            elif i.lower() == 'reverse':
                rev = is_reverse(futu_code,df,config) # 趋势反转
                if rev is not None:
                    msg += f' | {rev}'
            elif i.lower() == 'continue':
                co = is_continue(df) # 趋势延续
                if co is not None:
                    msg += f' | {co}'
            elif i.lower() == 'topdown':
                td = is_top_down(df) # 顶底结构
                if td is not None:
                    msg += f' | {td}'
            elif i.lower() == 'balance':
                bal = is_balance(df) # 量价关系平衡
                if bal is not None:
                    msg += f' | {bal}'
        
        # 计算动量因子
        momentum = calc_momentum(close)
        
        # 获取最后两个动量值，用于判断方向
        last_momentum = momentum.iloc[-1]
        prev_momentum = momentum.iloc[-2]
        msg += f' | {last_momentum:.3f}'

        if last_momentum > prev_momentum:
            msg += f'↑'
        elif last_momentum < prev_momentum:
            msg += f'↓'
        else:
            msg += f'→'
        
        # 添加到结果列表
        results.append({
            'futu_code': futu_code,
            'name': name,
            'msg': msg,
            'momentum': last_momentum
        })
    
    # 创建DataFrame并按动量因子排序
    if results:
        # 添加一行动量值为0的记录作为0轴指示
        results.append({
            'futu_code': 'ZERO_AXIS',
            'name': '动量0轴',
            'msg': '━━━━━━━━动量0轴━━━━━━━━',
            'momentum': 0.000
        })
        
        result_df = pd.DataFrame(results)
        result_df.set_index('futu_code', inplace=True)
        result_df.sort_values('momentum', ascending=False, inplace=True)
        return result_df
    else:
        return pd.DataFrame(columns=['name', 'msg', 'momentum'])

if __name__ == "__main__":
    config = get_config()
    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    group = config.get("CONFIG", "FUTU_GROUP")

    ls = code_in_futu_group(group,host,port)
    trends_df = check_trends(ls,config)

    notification = NotificationEngine(config)
    notification.send_futu_message(trends_df.index.tolist(),trends_df['msg'].tolist())
    notification.send_telegram_message('{} {}:\n{}'.format(datetime.datetime.now().strftime('%Y-%m-%d'), group, '\n'.join(trends_df['msg'])),'https://www.futunn.com/')
    notification.send_email(group,'<p>{} {}:<br>{}</p>'.format(datetime.datetime.now().strftime('%Y-%m-%d'), group, '<br>'.join(trends_df['msg'])))
