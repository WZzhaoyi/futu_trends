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

def calculate_trend_duration(df, min_return=0.001, smooth_window=3):
    smoothed_returns = df['close'].pct_change().rolling(window=smooth_window, min_periods=1).mean()
    trends = []
    current_trend = 0
    
    for ret in smoothed_returns.dropna():
        if ret >= min_return and current_trend >= 0:
            current_trend += 1
        elif ret <= -min_return and current_trend <= 0:
            current_trend -= 1
        elif (ret > min_return and current_trend < 0) or (ret < -min_return and current_trend > 0):
            if abs(current_trend) >= 2:
                trends.append(abs(current_trend))
            current_trend = 1 if ret > min_return else -1
        else:
            if abs(current_trend) >= 2:
                trends.append(abs(current_trend))
            current_trend = 0
    
    if abs(current_trend) >= 2:
        trends.append(abs(current_trend))
    
    return trends

# 按时间段分析市场状态
def analyze_market_states(df, period='Q'):  # 'Q' 为季度，'Y' 为年度
    df['date'] = df.index
    grouped = df.groupby(pd.Grouper(key='date', freq=period))
    
    volatility = []
    trend_length = []
    periods = []
    
    for name, group in grouped:
        # 计算波动性（标准化 ATR）
        atr = ATR(group['high'], group['low'], group['close'], period=36)
        vol = atr.mean() / group['close'].mean()  # 波动性相对价格
        volatility.append(vol)
        
        # 计算趋势长度（中位数）
        trends = calculate_trend_duration(group, min_return=0, smooth_window=10)
        trend_len = np.median(trends) if trends else 10
        trend_length.append(trend_len)
        
        periods.append(name)
    
    return pd.DataFrame({
        'Period': periods,
        'Volatility': volatility,
        'Trend_Length': trend_length
    })

def determine_look_ahead(volatility, trend_length):
    # look_ahead简洁参考规则
    # 规则 1：根据波动性和趋势长度选择 look_ahead
    # 高波动 + 短趋势（震荡行情）：
    # 波动性 > 0.03，且趋势长度 < 7 天。
    # 推荐 look_ahead：7 天。
    # 理由：捕捉快速顶底，信号密集，适合每周 2-3 次（每月 ≈ 10-15 次）。
    # 适用标的：高波动个股（如科技股）、震荡期指数。
    # 低波动 + 长趋势（趋势行情）：
    # 波动性 < 0.02，且趋势长度 > 10 天。
    # 推荐 look_ahead：14 天。
    # 理由：减少无效信号，确认趋势反转，信号频率降低（每月 ≈ 5-10 次）。
    # 适用标的：低波动指数（如蓝筹股指数）、趋势期市场。
    # 中等波动 + 中等趋势（混合行情）：
    # 波动性 0.02-0.03，或趋势长度 7-10 天。
    # 推荐 look_ahead：10 天。
    # 理由：平衡胜率和召回率，信号频率适中（每月 ≈ 7-12 次）。
    # 适用标的：中波动指数（如上证综指）、混合期市场。
    # 规则 2：调整信号频率
    # 如果信号频率过高（每月 > 15 次）：
    # 增加 strength_threshold（如 0.1 → 0.2），减少信号数量。
    # 或缩短 atr_period_explicit 和 atr_period_hidden（如 45 → 20），提高信号质量。
    # 如果信号频率过低（每月 < 5 次）：
    # 降低 strength_threshold（如 0.2 → 0.1），增加信号数量。
    # 或延长 look_ahead（如 7 → 10），捕捉更多反转。
    # 规则 3：通用性扩展
    # 高波动标的（个股或高风险资产）：
    # 默认波动性阈值可放宽（> 0.04 算高波动），趋势长度阈值缩短（< 5 天）。
    # 低波动标的（债券指数或稳定资产）：
    # 默认波动性阈值收紧（< 0.015 算低波动），趋势长度阈值延长（> 12 天）。
    # 调整范围：
    # 波动性范围：0.01-0.06（灵活调整）。
    # 趋势长度范围：3-20 天（根据分布调整）。
    if volatility > 0.03 and trend_length < 7:
        return 7
    elif volatility < 0.02 and trend_length > 10:
        return 14
    else:
        return 10
    
def get_signal_target_percentage(volatility):
    if volatility > 0.03:
        return 0.07  # 震荡行情，信号密集
    elif volatility < 0.02:
        return 0.03  # 趋势行情，信号稀疏
    else:
        return 0.05  # 混合行情，适中

def calculate_target_multiplier(df, atr_period=20, look_ahead=10):
    df = df.copy()
    atr = ATR(df['high'], df['low'], df['close'], period=atr_period)
    df['atr'] = atr
    df['future_high'] = get_future_range_numba(df['high'].values, look_ahead, is_high=True)
    df['future_low'] = get_future_range_numba(df['low'].values, look_ahead, is_high=False)
    
    support_returns = (df['future_high'] - df['close']) / df['atr']
    resistance_returns = (df['close'] - df['future_low']) / df['atr']
    valid_returns = pd.concat([support_returns.dropna(), resistance_returns.dropna()])
    
    return np.median(valid_returns) if len(valid_returns) > 0 else 1.0

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
def detect_stochastic_signals_vectorized(df: pd.DataFrame, k_period=14, d_period=3, overbought=80, oversold=20, support_ma_period=20, resistance_ma_period=20,atr_period_explicit=14, atr_period_hidden=14, strength_threshold=2):
    df = df.copy()
    k, d = Stochastic(df['high'], df['low'], df['close'], k_period, d_period)
    support_ma = df['close'].rolling(window=int(support_ma_period), min_periods=1).mean()
    resistance_ma = df['close'].rolling(window=int(resistance_ma_period), min_periods=1).mean()
    vol_ma = df['volume'].rolling(window=5, min_periods=1).mean()
    atr_explicit = ATR(df['high'], df['low'], df['close'], period=atr_period_explicit)
    atr_hidden = ATR(df['high'], df['low'], df['close'], period=atr_period_hidden)
    
    df['signal_strength'] = abs(k - d)
    df['k_amplitude'] = df['high'] - df['low']
    
    support_condition = (k > d) & (k.shift(1) <= d.shift(1)) & (k < oversold) & (df['close'] < support_ma)
    resistance_condition = (k < d) & (k.shift(1) >= d.shift(1)) & (k > overbought) & (df['close'] > resistance_ma)
    
    df['reversal'] = np.select(
        [support_condition, resistance_condition],
        ['support reversal', 'resistance reversal'],
        default='none'
    )
    
    # 显性强信号 放量突破
    df['is_strong_explicit'] = np.where(
        (df['reversal'] != 'none') & 
        (df['signal_strength'] >= strength_threshold) & 
        (df['k_amplitude'] > atr_explicit),
        # (df['volume'] >= vol_ma),
        1, 0
    )
    
    # 隐秘强信号 小实体衰竭
    df['is_strong_hidden'] = np.where(
        (df['reversal'] != 'none') & 
        (df['signal_strength'] >= strength_threshold) & 
        (df['k_amplitude'] < atr_hidden), 
        # (df['volume'] <= vol_ma),
        # (abs(df['close'] - ma) <= atr_hidden),
        1, 0
    )
    
    df['is_strong'] = np.where(
        (df['is_strong_explicit'] == 1) | (df['is_strong_hidden'] == 1),
        1, 0
    )
    
    return df

def calculate_win_rate(df, look_ahead=10, target_multiplier=1, atr_period=20):
    df = df.copy()
    df['atr'] = ATR(df['high'], df['low'], df['close'], period=atr_period)
    
    df['support_target'] = df['close'] + df['atr'] * target_multiplier
    df['resistance_target'] = df['close'] - df['atr'] * target_multiplier
    
    df['future_high'] = get_future_range_numba(df['high'].values, look_ahead, is_high=True)
    df['future_low'] = get_future_range_numba(df['low'].values, look_ahead, is_high=False)
    
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
    
    explicit_strong_support_signals = df[(df['reversal'] == 'support reversal') & (df['is_strong_explicit'] == 1)]
    explicit_strong_resistance_signals = df[(df['reversal'] == 'resistance reversal') & (df['is_strong_explicit'] == 1)]
    explicit_strong_support_win_rate = explicit_strong_support_signals['support_win'].mean() if len(explicit_strong_support_signals) > 0 else 0
    explicit_strong_resistance_win_rate = explicit_strong_resistance_signals['resistance_win'].mean() if len(explicit_strong_resistance_signals) > 0 else 0
    
    hidden_strong_support_signals = df[(df['reversal'] == 'support reversal') & (df['is_strong_hidden'] == 1)]
    hidden_strong_resistance_signals = df[(df['reversal'] == 'resistance reversal') & (df['is_strong_hidden'] == 1)]
    hidden_strong_support_win_rate = hidden_strong_support_signals['support_win'].mean() if len(hidden_strong_support_signals) > 0 else 0
    hidden_strong_resistance_win_rate = hidden_strong_resistance_signals['resistance_win'].mean() if len(hidden_strong_resistance_signals) > 0 else 0
    
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
        'explicit_strong_support_win_rate': explicit_strong_support_win_rate,
        'explicit_strong_support_signals_count': len(explicit_strong_support_signals),
        'explicit_strong_resistance_win_rate': explicit_strong_resistance_win_rate,
        'explicit_strong_resistance_signals_count': len(explicit_strong_resistance_signals),
        'hidden_strong_support_win_rate': hidden_strong_support_win_rate,
        'hidden_strong_support_signals_count': len(hidden_strong_support_signals),
        'hidden_strong_resistance_win_rate': hidden_strong_resistance_win_rate,
        'hidden_strong_resistance_signals_count': len(hidden_strong_resistance_signals),
        'support_recall': support_recall,
        'resistance_recall': resistance_recall,
        'detailed_df': df
    }

def KD_analysis(df, name, pl=False, evals=500): # { 'performance': result, 'best_params': best_params, 'signal': df_visual }
    # 参数空间
    space = {
        'k_period': hp.quniform('k_period', 9, 20, 1),  #聚焦有效范围
        'd_period': hp.quniform('d_period', 3, 7, 1),
        'overbought': hp.quniform('overbought', 70, 90, 5),
        'oversold': hp.quniform('oversold', 10, 30, 5),
        'support_ma_period': hp.quniform('support_ma_period', 5, 60, 5),
        'resistance_ma_period': hp.quniform('resistance_ma_period', 5, 60, 5),
        'atr_period_explicit': hp.quniform('atr_period_explicit', 5, 60, 5),
        'atr_period_hidden': hp.quniform('atr_period_hidden', 5, 60, 5),
        'strength_threshold': hp.quniform('strength_threshold', 0.1, 1.5, 0.1)
    }

    # 用atr_period计算 target_multiplier
    atr_period = calculate_atr_period(df)
    # 分析市场状态(每月)
    df_states = analyze_market_states(df, period='ME')
    # 最近一个月市场状态
    current_state = df_states.iloc[-1]
    # 根据近期Trend Duration Distribution估计
    currnet_look_ahead = determine_look_ahead(current_state['Volatility'], current_state['Trend_Length'])
    look_ahead = 10 
    target_multiplier = calculate_target_multiplier(df, atr_period=atr_period, look_ahead=look_ahead)

    # 根据市场状态调整目标百分比
    # volatility = ATR(df['high'], df['low'], df['close'], period=30).mean() / df['close'].mean()
    signal_count_target = len(df) * 0.07 # get_signal_target_percentage(volatility)

    # 目标函数，调整优化目标以保留更多信号
    def objective(params):
        # 转换为整数值
        params_int = {
            'k_period': int(params['k_period']),
            'd_period': int(params['d_period']),
            'overbought': params['overbought'],
            'oversold': params['oversold'],
            'support_ma_period': int(params['support_ma_period']),
            'resistance_ma_period': int(params['resistance_ma_period']),
            'atr_period_explicit': int(params['atr_period_explicit']),
            'atr_period_hidden': int(params['atr_period_hidden']),
            'strength_threshold': params['strength_threshold']
        }
        df_with_signals = detect_stochastic_signals_vectorized(df.copy(), **params_int)
        result = calculate_win_rate(df_with_signals, look_ahead=look_ahead, target_multiplier=target_multiplier, atr_period=atr_period)
        
        # 计算F2得分（β=2，更重视召回率）
        # beta = 2
        # support_precision = result['strong_support_win_rate']
        # support_recall = result['support_recall']
        # resistance_precision = result['strong_resistance_win_rate']
        # resistance_recall = result['resistance_recall']
        
        # support_f2 = (1 + beta**2) * (support_precision * support_recall) / (beta**2 * support_precision + support_recall) if (support_precision + support_recall) > 0 else 0
        # resistance_f2 = (1 + beta**2) * (resistance_precision * resistance_recall) / (beta**2 * resistance_precision + resistance_recall) if (resistance_precision + resistance_recall) > 0 else 0
        # score = (support_f2 + resistance_f2) / 2

        support_f1 = 2 * (result['strong_support_win_rate'] * result['support_recall']) / (result['strong_support_win_rate'] + result['support_recall']) if (result['strong_support_win_rate'] + result['support_recall']) > 0 else 0
        resistance_f1 = 2 * (result['strong_resistance_win_rate'] * result['resistance_recall']) / (result['strong_resistance_win_rate'] + result['resistance_recall']) if (result['strong_resistance_win_rate'] + result['resistance_recall']) > 0 else 0
        score = (support_f1 + resistance_f1) / 2
        
        # 添加信号数量惩罚项
        # signal_count_penalty = min(result['strong_support_signals_count'], result['strong_resistance_signals_count']) / 100
        signal_count_penalty = max(0.1, min(1.0, min(result['strong_support_signals_count'], result['strong_resistance_signals_count']) / signal_count_target))
        adjusted_score = score * signal_count_penalty
        
        return -adjusted_score  # 负值用于最小化

    # 执行贝叶斯优化
    trials = Trials()
    best = fmin(objective, space, algo=tpe.suggest, max_evals=evals, trials=trials, rstate=np.random.default_rng(random_seed))

    # 将best参数转换为实际值
    best_params = {
        'k_period': int(best['k_period']),
        'd_period': int(best['d_period']),
        'overbought': best['overbought'],
        'oversold': best['oversold'],
        'support_ma_period': int(best['support_ma_period']),
        'resistance_ma_period': int(best['resistance_ma_period']),
        'atr_period_explicit': int(best['atr_period_explicit']),
        'atr_period_hidden': int(best['atr_period_hidden']),
        'strength_threshold': best['strength_threshold']
    }

    # 使用最佳参数计算最终信号
    df = detect_stochastic_signals_vectorized(df, **best_params)
    result = calculate_win_rate(df)

    print(f"\n--------KD analysis for {name}--------")
    print(f"Recommended look_ahead for {current_state['Period']}: {currnet_look_ahead} days")
    print(f"Pre-calculated: look_ahead={look_ahead}, target_multiplier={target_multiplier:.2f}, atr_period={atr_period}")
    print(f"Best Parameters: {best_params}")
    print(f"Overall Support Reversal Win Rate: {result['support_win_rate']:.2%} (Signals: {result['support_signals_count']})")
    print(f"Overall Resistance Reversal Win Rate: {result['resistance_win_rate']:.2%} (Signals: {result['resistance_signals_count']})")
    print(f"Strong Support Reversal Win Rate: {result['strong_support_win_rate']:.2%} (Signals: {result['strong_support_signals_count']})")
    print(f"Strong Resistance Reversal Win Rate: {result['strong_resistance_win_rate']:.2%} (Signals: {result['strong_resistance_signals_count']})")
    print(f"Explicit Strong Support Win Rate: {result['explicit_strong_support_win_rate']:.2%} (Signals: {result['explicit_strong_support_signals_count']})")
    print(f"Explicit Strong Resistance Win Rate: {result['explicit_strong_resistance_win_rate']:.2%} (Signals: {result['explicit_strong_resistance_signals_count']})")
    print(f"Hidden Strong Support Win Rate: {result['hidden_strong_support_win_rate']:.2%} (Signals: {result['hidden_strong_support_signals_count']})")
    print(f"Hidden Strong Resistance Win Rate: {result['hidden_strong_resistance_win_rate']:.2%} (Signals: {result['hidden_strong_resistance_signals_count']})")
    print(f"Support Recall: {result['support_recall']:.2%}")
    print(f"Resistance Recall: {result['resistance_recall']:.2%}")

    df_visual = result['detailed_df']

    if pl:
        fig = plt.figure(figsize=(16, 10))
        plt.plot(df_visual.index, df_visual['close'], label='Close Price', color='blue', alpha=0.5)

        # 显性强信号
        plt.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong_explicit'] == 1) & (df_visual['support_win'] == 1)].index,
                    df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong_explicit'] == 1) & (df_visual['support_win'] == 1)]['close'],
                    color='darkgreen', marker='o', label='Explicit Strong Support (Win)', s=100)
        plt.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong_explicit'] == 1) & (df_visual['support_win'] == 0)].index,
                    df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong_explicit'] == 1) & (df_visual['support_win'] == 0)]['close'],
                    color='lightgreen', marker='o', label='Explicit Strong Support (Lose)', s=100)
        plt.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong_explicit'] == 1) & (df_visual['resistance_win'] == 1)].index,
                    df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong_explicit'] == 1) & (df_visual['resistance_win'] == 1)]['close'],
                    color='darkred', marker='s', label='Explicit Strong Resistance (Win)', s=100)
        plt.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong_explicit'] == 1) & (df_visual['resistance_win'] == 0)].index,
                    df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong_explicit'] == 1) & (df_visual['resistance_win'] == 0)]['close'],
                    color='salmon', marker='s', label='Explicit Strong Resistance (Lose)', s=100)

        # 隐秘强信号（用不同标记区分）
        plt.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong_hidden'] == 1) & (df_visual['support_win'] == 1)].index,
                    df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong_hidden'] == 1) & (df_visual['support_win'] == 1)]['close'],
                    color='darkgreen', marker='^', label='Hidden Strong Support (Win)', s=100)
        plt.scatter(df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong_hidden'] == 1) & (df_visual['support_win'] == 0)].index,
                    df_visual[(df_visual['reversal'] == 'support reversal') & (df_visual['is_strong_hidden'] == 1) & (df_visual['support_win'] == 0)]['close'],
                    color='lightgreen', marker='^', label='Hidden Strong Support (Lose)', s=100)
        plt.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong_hidden'] == 1) & (df_visual['resistance_win'] == 1)].index,
                    df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong_hidden'] == 1) & (df_visual['resistance_win'] == 1)]['close'],
                    color='darkred', marker='v', label='Hidden Strong Resistance (Win)', s=100)
        plt.scatter(df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong_hidden'] == 1) & (df_visual['resistance_win'] == 0)].index,
                    df_visual[(df_visual['reversal'] == 'resistance reversal') & (df_visual['is_strong_hidden'] == 1) & (df_visual['resistance_win'] == 0)]['close'],
                    color='salmon', marker='v', label='Hidden Strong Resistance (Lose)', s=100)

        # 弱信号（淡化显示）
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
            f'{name} Stochastic Oscillator Signals (look_ahead={look_ahead})\n'
            f'k={best_params["k_period"]}, d={best_params["d_period"]}, overbought={best_params["overbought"]}, oversold={best_params["oversold"]}, support_ma={best_params["support_ma_period"]}, resistance_ma={best_params["resistance_ma_period"]}, atr_explicit={best_params["atr_period_explicit"]}, atr_hidden={best_params["atr_period_hidden"]}, threshold={best_params["strength_threshold"]:.1f}\n'
            f'Explicit Strong Support Win Rate: {result["explicit_strong_support_win_rate"]:.2%} (Signals: {result["explicit_strong_support_signals_count"]}) Explicit Strong Resistance Win Rate: {result["explicit_strong_resistance_win_rate"]:.2%} (Signals: {result["explicit_strong_resistance_signals_count"]})\n'
            f'Hidden Strong Support Win Rate: {result["hidden_strong_support_win_rate"]:.2%} (Signals: {result["hidden_strong_support_signals_count"]}) Hidden Strong Resistance Win Rate: {result["hidden_strong_resistance_win_rate"]:.2%} (Signals: {result["hidden_strong_resistance_signals_count"]})\n'
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
    else:
        img_buf = None

    del result["detailed_df"]

    return { 
            'look_ahead': look_ahead,
            'target_multiplier': target_multiplier,
            'atr_period': atr_period,
            'performance': result,
            'best_params': best_params,
            'signal': df_visual,
            'plot': img_buf
            }
