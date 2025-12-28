from io import BytesIO
import math
import random
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from numba import njit
from hyperopt import hp, fmin, tpe, Trials
from IPython.display import display
from statsmodels.tsa.stattools import acf
from scipy.signal import periodogram
import os
from tqdm import tqdm
from multiprocessing import Pool, cpu_count
import matplotlib.collections as mcoll
import matplotlib.dates as mdates
import warnings
import logging

# 过滤 FutureWarning 警告
warnings.filterwarnings('ignore', category=FutureWarning, message='.*DataFrame.swapaxes.*')

# 设置随机种子
random_seed = 42  # 可以选择任意整数（如 42、2025 等）
np.random.seed(random_seed)
random.seed(random_seed)

# ATR计算（矢量化）
def ATR(high, low, close, period=14):
    tr = pd.DataFrame(index=high.index)
    tr['HL'] = high - low
    tr['HC'] = abs(high - close.shift(1))
    tr['LC'] = abs(low - close.shift(1))
    tr['TR'] = tr[['HL', 'HC', 'LC']].max(axis=1)
    return tr['TR'].rolling(window=int(period), min_periods=1).mean()

# Numba加速未来范围计算
@njit
def get_future_range_numba(series, look_ahead, is_high=True):
    future_values = np.full(len(series), np.nan)
    for i in range(len(series) - look_ahead):
        if is_high:
            future_values[i] = np.max(series[i+1:i+1+look_ahead])
        else:
            future_values[i] = np.min(series[i+1:i+1+look_ahead])
    return future_values

def calculate_trend_duration(df, min_trend_days=5):
    """
    使用均线理论计算趋势长度，以趋势终结日期为索引
    
    参数:
    - min_trend_days: 最小趋势天数
    
    返回:
    - DataFrame包含趋势信息：trend_length, direction
    - 索引为趋势终结日期
    """
    # 计算均线
    df = df.copy()
    df['ma_short'] = df['close'].rolling(window=5).mean()
    df['ma_middle'] = df['close'].rolling(window=10).mean()
    df['ma_long'] = df['close'].rolling(window=15).mean()
    
    trends_data = []
    current_trend = {
        'direction': 0,  # 1: 上升, -1: 下降
        'days': 0,
    }
    
    for i in range(15, len(df)):
        today = df.index[i]
        # 判断趋势方向
        is_uptrend = (df['ma_short'].iloc[i] > df['ma_middle'].iloc[i] > df['ma_long'].iloc[i] and
                     df['ma_short'].iloc[i] > df['ma_short'].iloc[i-1])
        is_downtrend = (df['ma_short'].iloc[i] < df['ma_middle'].iloc[i] < df['ma_long'].iloc[i] and
                       df['ma_short'].iloc[i] < df['ma_short'].iloc[i-1])
        
        if current_trend['direction'] == 0:  # 新趋势开始
            if is_uptrend:
                current_trend = {
                    'direction': 1,
                    'days': 1,
                }
            elif is_downtrend:
                current_trend = {
                    'direction': -1,
                    'days': 1,
                }
        else:  # 已有趋势
            if (current_trend['direction'] == 1 and is_uptrend) or \
               (current_trend['direction'] == -1 and is_downtrend):
                # 趋势继续
                current_trend['days'] += 1
            else:
                # 趋势结束
                if current_trend['days'] >= min_trend_days:
                    trends_data.append({
                        'trend_length': current_trend['days'],
                        'direction': current_trend['direction'],
                        'end_date': today
                    })
                # 重置趋势
                current_trend = {
                    'direction': 0,
                    'days': 0,
                }
    
    # 转换为DataFrame，使用趋势终结日期作为索引
    trends_df = pd.DataFrame(trends_data, index=pd.Index([trend['end_date'] for trend in trends_data]))
    
    # 丢弃首尾趋势
    if len(trends_df) > 2:
        trends_df = trends_df.iloc[1:-1]
    
    return trends_df

# 按时间段分析市场状态
def analyze_market_states(df, period=21):
    df = df.copy()
    # df['date'] = df.index
    group_count = np.ceil(len(df) / period)
    grouped = np.array_split(df, group_count)
    
    volatility = []
    historical_volatility = []
    trend_length = []
    periods = []
    
    # 预先计算全局趋势
    trends_df = calculate_trend_duration(df)
    
    for i, group in enumerate(grouped):
        name = group.index[-1]
        atr = ATR(group['high'], group['low'], group['close'], period=period)
        vol = atr.mean() / group['close'].mean()
        volatility.append(vol)
        
        # 获取当前分组最后一天之前的所有趋势
        group_end = group.index[-1]
        historical_trends = trends_df[trends_df.index <= group_end]['trend_length'].values
        
        # 计算历史趋势长度中位数
        if len(historical_trends) > 0:
            trend_median = np.mean([f for f in historical_trends if f > 0])
            trend_length.append(trend_median)
        else:
            trend_length.append(0)

        historical_volatility.append(np.mean(volatility))
        periods.append(name)
    
    return pd.DataFrame({
        'Period': periods,
        'Volatility': volatility,
        'Historical_Volatility': historical_volatility,
        'Trend_Length': trend_length
    })

def determine_look_ahead(volatility, trend_length):
    if volatility > 0.03 and trend_length < 7:
        return 7
    elif volatility < 0.02 and trend_length > 10:
        return 14
    else:
        return 10
    
def get_signal_target_percentage(volatility):
    if volatility >= 0.035:
        return 0.07  # 震荡行情，信号密集
    elif volatility >= 0.010:
        return 0.05  # 适中信号
    else:
        return 0.03  # 稀疏信号

def calculate_target_multiplier(df, atr_period=20, look_ahead=10):
    df = df.copy()
    atr = ATR(df['high'], df['low'], df['close'], period=atr_period)
    df['atr'] = atr
    df['future_high'] = get_future_range_numba(df['high'].values, look_ahead, is_high=True)
    df['future_low'] = get_future_range_numba(df['low'].values, look_ahead, is_high=False)
    
    support_returns = (df['future_high'] - df['close']) / df['atr']
    resistance_returns = (df['close'] - df['future_low']) / df['atr']
    valid_returns = pd.concat([support_returns.dropna(), resistance_returns.dropna()])
    
    return np.mean(valid_returns) if len(valid_returns) > 0 else 1.0

def calculate_atr_period(df, max_period=60):
    daily_range = df['high'] - df['low']
    freqs, power = periodogram(daily_range.dropna())
    
    # 过滤掉频率为0的值
    non_zero_mask = freqs > 0
    freqs = freqs[non_zero_mask]
    power = power[non_zero_mask]
    
    periods = 1 / freqs
    valid_idx = np.where((periods > 5) & (periods <= max_period))
    
    if len(valid_idx[0]) == 0:
        return 20  # 如果没有有效周期，返回默认值
    
    dominant_period = periods[valid_idx][np.argmax(power[valid_idx])]
    return int(dominant_period) if dominant_period else 20

def get_target_price(df, target_multiplier=1.1, atr_period=20)->tuple[float, float]:
    # 获取目标价格 [low, high]
    df = df.copy()
    atr = ATR(df['high'], df['low'], df['close'], period=atr_period).iloc[-1]
    close = df['close'].iloc[-1]
    target_high = close + atr * target_multiplier
    target_low = close - atr * target_multiplier
    return round(target_low, 3), round(target_high, 3)

def calculate_win_rate(df, look_ahead=10, target_multiplier=1.1, atr_period=20, check_high_low=True):
    df = df.copy()
    df['atr'] = ATR(df['high'], df['low'], df['close'], period=atr_period)
    
    df['support_target'] = df['close'] + df['atr'] * target_multiplier
    df['resistance_target'] = df['close'] - df['atr'] * target_multiplier
    
    df['future_high'] = get_future_range_numba(df['high'].values, look_ahead, is_high=True)
    df['future_low'] = get_future_range_numba(df['low'].values, look_ahead, is_high=False)
    
    df['recent_high'] = df['high'].rolling(window=3, min_periods=1).max()
    df['recent_low'] = df['low'].rolling(window=3, min_periods=1).min()
    
    if check_high_low:
        df['support_win'] = np.where(
            (df['reversal'] == 'support reversal') & (df['future_high'] >= df['support_target']) & (df['recent_low'] <= df['future_low']),
            1, 0
        )
        df['resistance_win'] = np.where(
            (df['reversal'] == 'resistance reversal') & (df['future_low'] <= df['resistance_target']) & (df['recent_high'] >= df['future_high']),
            1, 0
        )
    else:
        df['support_win'] = np.where(
            (df['reversal'] == 'support reversal') & (df['future_high'] >= df['support_target']),
            1, 0
        )
        df['resistance_win'] = np.where(
            (df['reversal'] == 'resistance reversal') & (df['future_low'] <= df['resistance_target']),
            1, 0
        )
    
    support_signals = df[df['reversal'] == 'support reversal']
    resistance_signals = df[df['reversal'] == 'resistance reversal']
    support_win_rate = support_signals['support_win'].mean() if len(support_signals) > 0 else 0
    resistance_win_rate = resistance_signals['resistance_win'].mean() if len(resistance_signals) > 0 else 0

    strong_support_signals = df[(df['reversal'] == 'support reversal') & (df['is_strong'] == 1)]
    strong_resistance_signals = df[(df['reversal'] == 'resistance reversal') & (df['is_strong'] == 1)]
    strong_support_win_rate = strong_support_signals['support_win'].mean() if len(strong_support_signals) > 0 else 0
    strong_resistance_win_rate = strong_resistance_signals['resistance_win'].mean() if len(strong_resistance_signals) > 0 else 0
    
    support_recall = len(strong_support_signals) / len(support_signals) if len(support_signals) > 0 else 0
    resistance_recall = len(strong_resistance_signals) / len(resistance_signals) if len(resistance_signals) > 0 else 0
    
    return {
        'support_win_rate': support_win_rate,
        'support_signals_count': len(support_signals),
        'resistance_win_rate': resistance_win_rate,
        'resistance_signals_count': len(resistance_signals),
        'strong_support_win_rate': strong_support_win_rate,
        'strong_support_signals_count': len(strong_support_signals),
        'strong_resistance_win_rate': strong_resistance_win_rate,
        'strong_resistance_signals_count': len(strong_resistance_signals),
        'support_recall': support_recall,
        'resistance_recall': resistance_recall,
        'support_z_score': trading_system_z_score(df[df['reversal'] == 'support reversal']['support_win'].tolist()),
        'resistance_z_score': trading_system_z_score(df[df['reversal'] == 'resistance reversal']['resistance_win'].tolist()),
        'detailed_df': df
    }

def trading_system_z_score(trades: list, win_value=1, loss_value=0):
  """
  计算交易系统盈亏序列的Z-score。

  参数:
    trades (list): 一个由1（盈利）和0（亏损）组成的列表。
                   例如: [1, 1, 0, 1, 0, 0, 1]

  返回:
    float or 0: 计算出的Z-score或错误信息。
  """
  n = len(trades)
  if n < 30:
    return 0

  wins = trades.count(win_value)
  losses = trades.count(loss_value)

  if wins == 0 or losses == 0:
      return 0

  # 计算R（序列总数）
  r = 1
  for i in range(1, n):
    if trades[i] != trades[i-1]:
      r += 1

  p = 2.0 * wins * losses
  
  numerator = n * (r - 0.5) - p
  denominator = math.sqrt((p * (p - n)) / (n - 1))
  
  if denominator == 0:
    return 0
      
  z_score = numerator / denominator
  return z_score

def display_signals(df_visual, title, best_params, result):
    """
    信号可视化
    Args:
        df_visual: 包含信号数据的DataFrame
        title: 图表标题
        best_params: 最佳参数字典
        result: 结果字典
    Returns:
        BytesIO: 包含图表图像的缓冲区
    """
    print(f"Best Parameters: {best_params}")
    print(f"Overall Support Reversal Win Rate: {result['support_win_rate']:.2%} (Signals: {result['support_signals_count']})")
    print(f"Overall Resistance Reversal Win Rate: {result['resistance_win_rate']:.2%} (Signals: {result['resistance_signals_count']})")
    print(f"Strong Support Reversal Win Rate: {result['strong_support_win_rate']:.2%} (Signals: {result['strong_support_signals_count']})")
    print(f"Strong Resistance Reversal Win Rate: {result['strong_resistance_win_rate']:.2%} (Signals: {result['strong_resistance_signals_count']})")
    print(f"Support Recall: {result['support_recall']:.2%} Resistance Recall: {result['resistance_recall']:.2%}")
    print(f"Support Z-Score: {result['support_z_score']:.2f} Resistance Z-Score: {result['resistance_z_score']:.2f}")

    fig, ax = plt.subplots(figsize=(20, 10))
    plt.yscale('log')

    # 大实体标记
    atr = ATR(df_visual['high'], df_visual['low'], df_visual['close'], period=20).fillna(0)
    change_pct = (df_visual['close'] - df_visual['open']) / df_visual['open']
    threshold = 1.5 * atr / df_visual['open']
    strength = change_pct.abs() / threshold
    big_bull = change_pct > threshold
    big_bear = change_pct < -threshold
    bull_strength = np.clip(strength, 0.5, 1.0)
    bear_strength = np.clip(strength, 0.5, 1.0)

    idx = df_visual.index.to_pydatetime() if hasattr(df_visual.index, 'to_pydatetime') else df_visual.index
    x = mdates.date2num(idx)
    y = np.array(df_visual['close'])
    color_arr = []
    alpha_arr = []
    for i in range(1, len(df_visual)):
        if big_bull.iloc[i]:
            color_arr.append((1, 0, 0, bull_strength.iloc[i]))  # red, alpha by strength
        elif big_bear.iloc[i]:
            color_arr.append((0, 0.5, 0, bear_strength.iloc[i]))  # green, alpha by strength
        else:
            color_arr.append((0, 0, 1, 0.5))  # blue, normal alpha
        alpha_arr.append(1)
    # 绘制K线
    points = np.array([x, y]).T.reshape(-1, 1, 2)
    segments = np.concatenate([points[:-1], points[1:]], axis=1)
    lc = mcoll.LineCollection(segments, colors=color_arr, linewidths=2)
    ax.add_collection(lc)
    ax.autoscale()
    ax.xaxis_date()

    # y轴自适应
    ymin = np.nanmin(df_visual['close'])
    ymax = np.nanmax(df_visual['close'])
    ymin = ymin * 0.9
    ymax = ymax * 1.1
    ax.set_ylim(ymin, ymax)

    # 强信号
    ax.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 1) & (df_visual['support_win'] == 1)].index,
                df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 1) & (df_visual['support_win'] == 1)]['close'],
                color='darkgreen', marker='o', label='Strong Support (Win)', s=60)
    ax.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 1) & (df_visual['support_win'] == 0)].index,
                df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 1) & (df_visual['support_win'] == 0)]['close'],
                color='lightgreen', marker='o', label='Strong Support (Lose)', s=60)
    ax.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 1) & (df_visual['resistance_win'] == 1)].index,
                df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 1) & (df_visual['resistance_win'] == 1)]['close'],
                color='darkred', marker='s', label='Strong Resistance (Win)', s=60)
    ax.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 1) & (df_visual['resistance_win'] == 0)].index,
                df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 1) & (df_visual['resistance_win'] == 0)]['close'],
                color='salmon', marker='s', label='Strong Resistance (Lose)', s=60)
    
    # 弱信号
    if result["support_recall"] < 1 or result["resistance_recall"] < 1:
        ax.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 0) & (df_visual['support_win'] == 1)].index,
                    df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 0) & (df_visual['support_win'] == 1)]['close'],
                    color='darkgreen', marker='o', label='Weak Support (Win)', s=30, alpha=0.3)
        ax.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 0) & (df_visual['support_win'] == 0)].index,
                    df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 0) & (df_visual['support_win'] == 0)]['close'],
                    color='lightgreen', marker='o', label='Weak Support (Lose)', s=30, alpha=0.3)
        ax.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 0) & (df_visual['resistance_win'] == 1)].index,
                    df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 0) & (df_visual['resistance_win'] == 1)]['close'],
                    color='darkred', marker='s', label='Weak Resistance (Win)', s=30, alpha=0.3)
        ax.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 0) & (df_visual['resistance_win'] == 0)].index,
                    df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 0) & (df_visual['resistance_win'] == 0)]['close'],
                    color='salmon', marker='s', label='Weak Resistance (Lose)', s=30, alpha=0.3)

    # 标题
    title = (
        f'{title}\n'
        f'{", ".join([f"{k}={v}" for k,v in best_params.items()])}\n'
        f'Support Win Rate: {result["support_win_rate"]:.2%}({result["support_signals_count"]}), Resistance Win Rate: {result["resistance_win_rate"]:.2%}({result["resistance_signals_count"]})\n'
        f'Strong Support Win Rate: {result["strong_support_win_rate"]:.2%}, Strong Resistance Win Rate: {result["strong_resistance_win_rate"]:.2%}\n'
        f'Support Recall: {result["support_recall"]:.2%} Resistance Recall: {result["resistance_recall"]:.2%} Support Z-Score: {result["support_z_score"]:.2f} Resistance Z-Score: {result["resistance_z_score"]:.2f}'
    )
    ax.set_title(title, fontsize=12)
    ax.set_xlabel('Date')
    ax.set_ylabel('Close Price')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='best')
    img_buf = BytesIO()
    fig.savefig(img_buf, format='png', bbox_inches='tight')
    plt.close(fig)
    img_buf.seek(0)
    return img_buf

def run_bayes_optimization(args):
    """运行贝叶斯优化"""
    i, space, objective, max_evals, patience, min_delta, random_seed = args
    
    if random_seed is not None:
        np.random.seed(random_seed)
        random.seed(random_seed)
    
    trials = Trials()
    best_score = float('-inf')
    no_improvement_count = 0
    
    def early_stop_fn(trials):
        nonlocal best_score, no_improvement_count
        
        # 获取当前最佳分数
        current_score = -trials.best_trial['result']['loss']
        
        # 检查是否有显著改善
        if current_score > best_score + min_delta:
            best_score = current_score
            no_improvement_count = 0
            return False, {}  # 返回元组 (stop, kwargs)
        
        # 无显著改善，增加计数
        no_improvement_count += 1
        
        # 如果连续多代无改善，触发早停
        if no_improvement_count >= patience:
            return True, {}  # 返回元组 (stop, kwargs)
        
        return False, {}  # 返回元组 (stop, kwargs)
    
    # 关闭hyperopt的日志输出
    hyperopt_logger = logging.getLogger('hyperopt')
    original_level = hyperopt_logger.level
    hyperopt_logger.setLevel(logging.WARNING)
    
    try:
        # 执行贝叶斯优化
        best = fmin(
            fn=objective,
            space=space,
            algo=tpe.suggest,
            max_evals=max_evals,
            trials=trials,
            early_stop_fn=early_stop_fn,
            verbose=False
        )
    finally:
        # 恢复原始日志级别
        hyperopt_logger.setLevel(original_level)
    
    score = -trials.best_trial['result']['loss']
    return score, best
