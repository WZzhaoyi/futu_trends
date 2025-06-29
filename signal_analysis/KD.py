from io import BytesIO
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
    trends_df = pd.DataFrame(trends_data, index=[trend['end_date'] for trend in trends_data])
    
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
    periods = 1 / freqs
    valid_idx = np.where((periods > 5) & (periods <= max_period))
    dominant_period = periods[valid_idx][np.argmax(power[valid_idx])]
    return int(dominant_period) if dominant_period else 20

# 随机指标计算（矢量化）
def Stochastic(high, low, close, k_period, d_period):
    low_min = low.rolling(window=int(k_period), min_periods=1).min()
    high_max = high.rolling(window=int(k_period), min_periods=1).max()
    k = 100 * (close - low_min) / (high_max - low_min)
    d = k.rolling(window=int(d_period), min_periods=1).mean()
    return k, d

# 矢量化信号检测，区分显性和隐秘强信号
def detect_stochastic_signals_vectorized(df: pd.DataFrame, params: dict, mode='train'):
    """
    使用随机指标检测信号
    
    Args:
        df: 包含OHLCV数据的DataFrame
        params: 参数字典，包含以下字段：
            - k_period: K线周期
            - d_period: D线周期
            - overbought: 超买阈值
            - oversold: 超卖阈值
            - support_ma_period: 支撑位MA周期
            - resistance_ma_period: 阻力位MA周期
            # - atr_period_explicit: 显性信号ATR周期
            # - atr_period_hidden: 隐性信号ATR周期
            - strength_threshold: 信号强度阈值
        mode: 模式，'train'或'eval', 训练时无未来函数, 推理时考虑未来确认
    """
    df = df.copy()
    k, d = Stochastic(df['high'], df['low'], df['close'], 
                      params['k_period'], params['d_period'])
    support_ma = df['close'].rolling(window=int(params['support_ma_period']), min_periods=1).mean()
    resistance_ma = df['close'].rolling(window=int(params['resistance_ma_period']), min_periods=1).mean()
    
    df['signal_strength'] = abs(k - d)

    support_condition = (k > d) & (k.shift(1) <= d.shift(1)) & (k < params['oversold']) & (df['close'] < support_ma)
    resistance_condition = (k < d) & (k.shift(1) >= d.shift(1)) & (k > params['overbought']) & (df['close'] > resistance_ma)

    if mode == 'check':
        # 推理考虑未来阳线
        support_condition = support_condition & (((df['close'].shift(-1) > df['close'])&(df['open'].shift(-1) < df['close'].shift(-1))) | ((df['close'] < df['close'].shift(-2))&(df['close'].shift(-1) < df['close'].shift(-2))))
        resistance_condition = resistance_condition & (((df['close'].shift(-1) < df['close'])&(df['open'].shift(-1) > df['close'].shift(-1))) | ((df['close'] > df['close'].shift(-2))&(df['close'].shift(-1) > df['close'].shift(-2))))
    elif mode == 'train':
        # 训练无未来函数
        pass
    
    df['reversal'] = np.select(
        [support_condition, resistance_condition],
        ['support reversal', 'resistance reversal'],
        default='none'
    )
    
    df['is_strong'] = np.where(
        (df['reversal'] != 'none') & 
        (df['signal_strength'] >= round(params['strength_threshold'],1)),
        1, 0
    )

    return df

def get_target_price(df, is_support=True, target_multiplier=1, atr_period=20):
    df = df.copy()
    atr = ATR(df['high'], df['low'], df['close'], period=atr_period).fillna(0).iloc[-1]
    close = df['close'].iloc[-1]
    target = None
    if is_support:
        target = close + atr * target_multiplier
    else:
        target = close - atr * target_multiplier
    return round(target, 3) if isinstance(target, float) and target > 0 else None

def calculate_win_rate(df, look_ahead=10, target_multiplier=1, atr_period=20):
    df = df.copy()
    df['atr'] = ATR(df['high'], df['low'], df['close'], period=atr_period)
    
    df['support_target'] = df['close'] + df['atr'] * target_multiplier
    df['resistance_target'] = df['close'] - df['atr'] * target_multiplier
    
    df['future_high'] = get_future_range_numba(df['high'].values, look_ahead, is_high=True)
    df['future_low'] = get_future_range_numba(df['low'].values, look_ahead, is_high=False)
    
    df['recent_high'] = df['high'].rolling(window=3, min_periods=1).max()
    df['recent_low'] = df['low'].rolling(window=3, min_periods=1).min()
    
    df['support_win'] = np.where(
        (df['reversal'] == 'support reversal') & (df['future_high'] >= df['support_target']) & (df['recent_low'] <= df['future_low']),
        1, 0
    )
    df['resistance_win'] = np.where(
        (df['reversal'] == 'resistance reversal') & (df['future_low'] <= df['resistance_target']) & (df['recent_high'] >= df['future_high']),
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
        'detailed_df': df
    }

def display_kd_signals(df_visual, title, best_params, result):
    """
    绘制KD信号可视化图表
    
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
    print(f"Support Recall: {result['support_recall']:.2%}")
    print(f"Resistance Recall: {result['resistance_recall']:.2%}")

    fig = plt.figure(figsize=(20, 10))
    plt.yscale('log')  # 设置y轴为对数坐标
    plt.plot(df_visual.index, df_visual['close'], label='Close Price', color='blue', alpha=0.5)

    # 强信号
    plt.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 1) & (df_visual['support_win'] == 1)].index,
                df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 1) & (df_visual['support_win'] == 1)]['close'],
                color='darkgreen', marker='o', label='Strong Support (Win)', s=60)
    plt.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 1) & (df_visual['support_win'] == 0)].index,
                df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 1) & (df_visual['support_win'] == 0)]['close'],
                color='lightgreen', marker='o', label='Strong Support (Lose)', s=60)
    plt.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 1) & (df_visual['resistance_win'] == 1)].index,
                df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 1) & (df_visual['resistance_win'] == 1)]['close'],
                color='darkred', marker='s', label='Strong Resistance (Win)', s=60)
    plt.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 1) & (df_visual['resistance_win'] == 0)].index,
                df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 1) & (df_visual['resistance_win'] == 0)]['close'],
                color='salmon', marker='s', label='Strong Resistance (Lose)', s=60)
                
                
    # 弱信号
    plt.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 0) & (df_visual['support_win'] == 1)].index,
                df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 0) & (df_visual['support_win'] == 1)]['close'],
                color='darkgreen', marker='o', label='Weak Support (Win)', s=30, alpha=0.3)
    plt.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 0) & (df_visual['support_win'] == 0)].index,
                df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong'] == 0) & (df_visual['support_win'] == 0)]['close'],
                color='lightgreen', marker='o', label='Weak Support (Lose)', s=30, alpha=0.3)
    plt.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 0) & (df_visual['resistance_win'] == 1)].index,
                df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 0) & (df_visual['resistance_win'] == 1)]['close'],
                color='darkred', marker='s', label='Weak Resistance (Win)', s=30, alpha=0.3)
    plt.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 0) & (df_visual['resistance_win'] == 0)].index,
                df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong'] == 0) & (df_visual['resistance_win'] == 0)]['close'],
                color='salmon', marker='s', label='Weak Resistance (Lose)', s=30, alpha=0.3)

    # 增强标题信息
    title = (
        f'{title}\n'
        f'k={best_params["k_period"]}, d={best_params["d_period"]}, overbought={best_params["overbought"]}, oversold={best_params["oversold"]}, support_ma={best_params["support_ma_period"]}, resistance_ma={best_params["resistance_ma_period"]}, threshold={best_params["strength_threshold"]:.1f}\n'
        f'Support Win Rate: {result["support_win_rate"]:.2%}, Resistance Win Rate: {result["resistance_win_rate"]:.2%}\n'
        f'Strong Support Win Rate: {result["strong_support_win_rate"]:.2%}, Strong Resistance Win Rate: {result["strong_resistance_win_rate"]:.2%}\n'
        f'Support Recall: {result["support_recall"]:.2%} Resistance Recall: {result["resistance_recall"]:.2%}'
    )
    plt.title(title, fontsize=12)
    plt.xlabel('Date')
    plt.ylabel('Close Price')
    plt.grid(True, alpha=0.3)
    plt.legend(loc='best')
    img_buf = BytesIO()
    fig.savefig(img_buf, format='png', bbox_inches='tight')
    plt.close(fig)  # 关闭图形，释放内存
    img_buf.seek(0)  # 将缓冲区的指针重置到开始位置
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
    
    score = -trials.best_trial['result']['loss']
    return score, best

class OptimizationObjective:
    def __init__(self, df, look_ahead, target_multiplier, atr_period, signal_count_target):
        self.df = df
        self.look_ahead = look_ahead
        self.target_multiplier = target_multiplier
        self.atr_period = atr_period
        self.signal_count_target = signal_count_target

    def __call__(self, params):
        # 转换为整数值
        params_int = {
            'k_period': int(params['k_period']),
            'd_period': int(params['d_period']),
            'overbought': params['overbought'],
            'oversold': params['oversold'],
            'support_ma_period': int(params['support_ma_period']),
            'resistance_ma_period': int(params['resistance_ma_period']),
            'strength_threshold': params['strength_threshold']
        }
        df_with_signals = detect_stochastic_signals_vectorized(self.df.copy(), params_int, mode='train')
        result = calculate_win_rate(df_with_signals, look_ahead=self.look_ahead, 
                                  target_multiplier=self.target_multiplier, 
                                  atr_period=self.atr_period)
        
        support_f1 = 2 * (result['strong_support_win_rate'] * result['support_recall']) / (result['strong_support_win_rate'] + result['support_recall']) if (result['strong_support_win_rate'] + result['support_recall']) > 0 else 0
        resistance_f1 = 2 * (result['strong_resistance_win_rate'] * result['resistance_recall']) / (result['strong_resistance_win_rate'] + result['resistance_recall']) if (result['strong_resistance_win_rate'] + result['resistance_recall']) > 0 else 0
        score = (support_f1 + resistance_f1) / 2
        
        # 添加信号数量惩罚项
        signal_count_penalty = min(1.0, min(result['strong_support_signals_count'], result['strong_resistance_signals_count']) / self.signal_count_target)
        adjusted_score = score * signal_count_penalty
        
        return -adjusted_score  # 负值用于最小化

def KD_analysis(df, name, evals=500, look_ahead:int=0):
    # 参数空间
    space = {
        'k_period': hp.quniform('k_period', 9, 21, 1),  #聚焦有效范围
        'd_period': hp.quniform('d_period', 3, 7, 1),
        'overbought': hp.quniform('overbought', 50, 90, 5),
        'oversold': hp.quniform('oversold', 10, 50, 5),
        'support_ma_period': hp.quniform('support_ma_period', 5, 60, 5),
        'resistance_ma_period': hp.quniform('resistance_ma_period', 5, 60, 5),
        'strength_threshold': hp.quniform('strength_threshold', 0.1, 4, 0.1)
    }

    # 主流atr_period
    atr_period = calculate_atr_period(df)
    # 分析市场状态
    df_states = analyze_market_states(df, period=atr_period)
    # 最近市场状态
    current_state = df_states.iloc[-1]
    currnet_look_ahead = determine_look_ahead(current_state['Historical_Volatility'], current_state['Trend_Length'])
    if look_ahead <= 0:
       look_ahead = currnet_look_ahead
    target_multiplier = calculate_target_multiplier(df, atr_period=atr_period, look_ahead=look_ahead)

    # 根据市场状态调整目标百分比
    signal_target_percentage = get_signal_target_percentage(current_state['Historical_Volatility'])
    signal_count_target = len(df) * signal_target_percentage

    print(f"\n--------KD analysis for {name}--------")
    print(f"Current Market State: {current_state['Period']}")
    print(f"Current Market State Volatility: {current_state['Historical_Volatility']}")
    print(f"Current Market signal target percentage: {signal_target_percentage*100}%")
    print(f"Recommended look_ahead for {current_state['Period']}: {currnet_look_ahead} days")
    print(f"Pre-calculated: look_ahead={look_ahead}, target_multiplier={target_multiplier:.2f}, atr_period={atr_period}")

    # 创建目标函数对象
    objective = OptimizationObjective(df, look_ahead, target_multiplier, atr_period, signal_count_target)

    # 进程池并行优化
    scores = []
    best_params = []
    n_optimizations = 20

    optimization_args = [
        (i, space, objective, evals, 100, 0.001, np.random.randint(0, 1000000))
        for i in range(n_optimizations)
    ]

    n_processes = max(1, cpu_count() - 1)  # 保留一个CPU核心
    with Pool(processes=n_processes) as pool:
        results = list(tqdm(
            pool.imap(run_bayes_optimization, optimization_args),
            total=n_optimizations
        ))

    scores, best_params = zip(*results)

    best_idx = np.argmax(scores)
    print(f"best score: {scores[best_idx]:.4f} best params: {best_params[best_idx]}")

    best = best_params[best_idx]

    # 将best参数转换为实际值
    best_params = {
        'k_period': int(best['k_period']),
        'd_period': int(best['d_period']),
        'overbought': best['overbought'],
        'oversold': best['oversold'],
        'support_ma_period': int(best['support_ma_period']),
        'resistance_ma_period': int(best['resistance_ma_period']),
        'strength_threshold': round(best['strength_threshold'], 1)
    }

    # 使用最佳参数计算最终信号
    print(f"\n--------Training signals for {name} with best params--------")
    df = detect_stochastic_signals_vectorized(df, best_params, mode='train')
    result = calculate_win_rate(df, look_ahead=look_ahead, target_multiplier=target_multiplier, atr_period=atr_period)
    df_visual = result['detailed_df']
    title = f'{name} Stochastic Oscillator Signals (look_ahead:{look_ahead} signal_target_percent:{(signal_target_percentage*100):.1f}%)'

    original_plot = display_kd_signals(df_visual, title, best_params, result)

    print(f"\n--------Checked signals for {name} with best params--------")
    df_checked = detect_stochastic_signals_vectorized(df, best_params, mode='check')
    result_checked = calculate_win_rate(df_checked, look_ahead=look_ahead, target_multiplier=target_multiplier, atr_period=atr_period)
    df_visual_checked = result_checked['detailed_df']
    checked_plot = display_kd_signals(df_visual_checked, f'Checked {title}', best_params, result_checked)

    del result["detailed_df"]

    meta_info = {
        'strategy': 'KD',
        'period_end': current_state['Period'].strftime('%Y-%m-%d %H:%M:%S %Z'),
        'signal_target_percent': signal_target_percentage,
        'volatility': current_state['Historical_Volatility'],
        'look_ahead': look_ahead,
        'target_multiplier': target_multiplier,
        'atr_period': atr_period
    }

    return {
            'performance': result,
            'best_params': best_params,
            'signal': df_visual,
            'plot': original_plot,
            'checked_plot': checked_plot,
            'meta_info': meta_info
            }
