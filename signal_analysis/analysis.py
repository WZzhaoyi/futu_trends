from multiprocessing import Pool, cpu_count

import numpy as np
from tqdm import tqdm
from .KD import *
from .indicators import RSI, Indicator, KD, MACD

class Optimization:
    def __init__(self, df, indicator:Indicator, look_ahead, target_multiplier, atr_period, signal_count_target):
        self.df = df
        self.look_ahead = look_ahead
        self.target_multiplier = target_multiplier
        self.atr_period = atr_period
        self.signal_count_target = signal_count_target
        self.indicator = indicator

    def __call__(self, params):
        df_with_signals = self.indicator.calculate(self.df.copy(), params, mode='train')
        result = calculate_win_rate(df_with_signals, look_ahead=self.look_ahead, 
                                  target_multiplier=self.target_multiplier, atr_period=self.atr_period)
        
        score = self.indicator.calculate_score(result, self.signal_count_target)
        
        return -score  # 负值用于最小化

def technical_analysis(df, name, indicator_type='KD', evals=500, look_ahead=0):
    """
    指标分析
    """
    # 选择指标
    indicators = {'KD': KD(), 'MACD': MACD(), 'RSI': RSI()}
    indicator = indicators.get(indicator_type)
    if not indicator:
        raise ValueError(f"不支持的指标类型: {indicator_type}")
    
    # 获取参数空间
    space = indicator.get_space()
    
    # 计算市场参数（复用原有逻辑）
    atr_period = calculate_atr_period(df)
    df_states = analyze_market_states(df, period=atr_period)
    current_state = df_states.iloc[-1]
    currnet_look_ahead = determine_look_ahead(current_state['Historical_Volatility'], current_state['Trend_Length'])
    if look_ahead <= 0:
        look_ahead = currnet_look_ahead
    target_multiplier = calculate_target_multiplier(df, atr_period=atr_period, look_ahead=look_ahead)
    signal_target_percentage = get_signal_target_percentage(current_state['Historical_Volatility'])
    signal_count_target = len(df) * signal_target_percentage
    
    print(f"\n--------{indicator_type} analysis for {name}--------")
    print(f"Current Market State: {current_state['Period']}")
    print(f"Pre-calculated: look_ahead={look_ahead}, target_multiplier={target_multiplier:.2f}, atr_period={atr_period}, signal_target_percentage={signal_target_percentage:.2%}")
    
    # 创建目标函数
    optimization = Optimization(df, indicator, look_ahead, target_multiplier, atr_period, signal_count_target)
    
    # 运行优化
    scores, best_params = [], []
    n_optimizations = 20
    optimization_args = [
        (i, space, optimization, evals, 100, 0.001, np.random.randint(0, 1000000))
        for i in range(n_optimizations)
    ]
    
    n_processes = max(1, cpu_count() - 1)
    with Pool(processes=n_processes) as pool:
        results = list(tqdm(pool.imap(run_bayes_optimization, optimization_args), total=n_optimizations))
    
    scores, best_params = zip(*results)
    best_idx = np.argmax(scores)
    best = best_params[best_idx]
    
    # 参数转换
    best_params = indicator.get_params(best)
    
    # 最终结果计算
    title = f'{name} {indicator_type} Signals'
    
    print(f"\n--------Training signals for {name} with best params--------")
    df_final = indicator.calculate(df, best_params, mode='train')
    result = calculate_win_rate(df_final, look_ahead=look_ahead, 
                               target_multiplier=target_multiplier, atr_period=atr_period)
    df_visual = result['detailed_df']
    original_plot = display_signals(df_visual, title, best_params, result)
    
    print(f"\n--------Checked signals for {name} with best params--------")
    df_checked = indicator.calculate(df, best_params, mode='check')
    result_checked = calculate_win_rate(df_checked, look_ahead=look_ahead, 
                                       target_multiplier=target_multiplier, atr_period=atr_period)
    checked_plot = display_signals(result_checked['detailed_df'], f'Checked {title}', best_params, result_checked)

    del result["detailed_df"]
    
    meta_info = {
        'strategy': indicator.name,
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
