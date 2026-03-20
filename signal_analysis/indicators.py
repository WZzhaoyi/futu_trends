from abc import ABC, abstractmethod
import pandas as pd
import numpy as np
from typing import Dict, Tuple
from hyperopt import hp

from signal_analysis.tool import ATR, calculate_win_rate

class Indicator(ABC):
    """技术指标基类"""

    @property
    def name(self):
        return self.__class__.__name__
    
    @abstractmethod
    def calculate(self, df: pd.DataFrame, params: Dict, mode='train', atr_period=None, target_multiplier=None, consume_ratio=None) -> pd.DataFrame:
        """计算指标并返回带信号的DataFrame
        mode='check' 时需提供 atr_period 和 target_multiplier 用于确认
        consume_ratio: 消耗过滤阈值，None 表示不过滤"""
        pass
    
    @abstractmethod
    def get_space(self) -> Dict:
        """返回参数搜索空间"""
        pass

    def get_params(self, params: Dict) -> Dict:
        """将参数转换为整数或浮点数"""
        return {k: int(v) if k.endswith('_period') else round(v, 1) if k == 'strength_threshold' else v 
                  for k, v in params.items()}

    def calculate_win_rate(self, df: pd.DataFrame, look_ahead=10, target_multiplier=1.1, atr_period=20) -> Dict:
        """计算胜率"""
        return calculate_win_rate(df, look_ahead, target_multiplier, atr_period, check_high_low=True)

    @abstractmethod
    def calculate_score(self, result: Dict, signal_count_target: float) -> float:
        """计算优化评分"""
        pass
    
    @abstractmethod
    def _future_confirmation(self, df, is_support) -> pd.Series:
        """未来确认 - 不能直接用作胜率计算"""
        ...

    def _check_confirmation(self, df, is_support, signal_cond, atr_period, target_multiplier, consume_ratio=None):
        """形态确认 + 目标空间消耗过滤
        consume_ratio: None 表示仅确认不过滤，float 表示消耗比例阈值"""
        pattern = self._future_confirmation(df, is_support)
        result = signal_cond & pattern
        if consume_ratio is not None:
            confirm_move = (df['close'].shift(-1) - df['close']).abs()
            atr = ATR(df['high'], df['low'], df['close'], period=atr_period)
            target_distance = atr * target_multiplier
            not_consumed = confirm_move < consume_ratio * target_distance
            filtered_result = result & not_consumed
            consumed = result.sum() - filtered_result.sum()
            side = 'support' if is_support else 'resistance'
            if result.sum() > 0:
                print(f"  {self.name} {side}: {signal_cond.sum()} raw, {result.sum()} confirmed, {consumed} consumed@{consume_ratio} ({consumed/result.sum():.0%})")
            result = filtered_result
        return result

    @abstractmethod
    def indicator_calculate(self, df: pd.DataFrame, params: Dict) -> pd.Series | Tuple[pd.Series]:
        """计算指标"""
        pass

class KD(Indicator):
    """KD随机指标"""
    
    def get_space(self):
        return {
            'k_period': hp.quniform('k_period', 9, 21, 1),
            'd_period': hp.quniform('d_period', 3, 7, 1),
            'overbought': hp.quniform('overbought', 50, 90, 1),
            'oversold': hp.quniform('oversold', 10, 50, 1),
        }
    
    def calculate(self, df: pd.DataFrame, params: Dict, mode='train', atr_period=None, target_multiplier=None, consume_ratio=None) -> pd.DataFrame:
        params = self.get_params(params)
        df = df.copy()

        # 计算KD
        k, d = self.indicator_calculate(df, params)
        df['k'], df['d'] = k, d

        support_cond = (k > d) & (k.shift(1) <= d.shift(1)) & (d < params['oversold'])
        resistance_cond = (k < d) & (k.shift(1) >= d.shift(1)) & (d > params['overbought'])

        # 未来确认
        if mode == 'check':
            support_cond = self._check_confirmation(df, True, support_cond, atr_period, target_multiplier, consume_ratio)
            resistance_cond = self._check_confirmation(df, False, resistance_cond, atr_period, target_multiplier, consume_ratio)

        # 生成信号
        df['reversal'] = np.select([support_cond, resistance_cond],
                                  ['support reversal', 'resistance reversal'], 'none')
        df['is_strong'] = ((df['reversal'] != 'none')).astype(int)
        return df

    def calculate_score(self, result: Dict, signal_count_target: float) -> float:
        support_f1 = 2 * (result['strong_support_win_rate'] * result['support_recall']) / \
                    (result['strong_support_win_rate'] + result['support_recall']) if \
                    (result['strong_support_win_rate'] + result['support_recall']) > 0 else 0
        resistance_f1 = 2 * (result['strong_resistance_win_rate'] * result['resistance_recall']) / \
                       (result['strong_resistance_win_rate'] + result['resistance_recall']) if \
                       (result['strong_resistance_win_rate'] + result['resistance_recall']) > 0 else 0
        
        if support_f1 > 0 and resistance_f1 > 0:
            score = 2 / (1/support_f1 + 1/resistance_f1)
        else:
            score = 0
        
        # 信号数量惩罚
        signal_count_penalty = min(1.0, min(result['strong_support_signals_count'], 
                                           result['strong_resistance_signals_count']) / signal_count_target)
        
        return score * signal_count_penalty

    def indicator_calculate(self, df: pd.DataFrame, params: Dict) -> Tuple[pd.Series]: # -> Tuple[k, d]
        k_period = int(params['k_period'])
        d_period = int(params['d_period'])
        return self._stochastic(df['high'], df['low'], df['close'], k_period, d_period)

    def _stochastic(self, high, low, close, k_period, d_period):
        low_min = low.rolling(window=k_period).min()
        high_max = high.rolling(window=k_period).max()
        k = 100 * (close - low_min) / (high_max - low_min)
        d = k.rolling(window=d_period).mean()
        return k, d

    def _future_confirmation(self, df, is_support):
        if is_support:
            return ((df['close'].shift(-1) > df['close']) & (df['open'].shift(-1) <= df['close'].shift(-1))) | \
                   ((df['close'].shift(-1) > df['high']) & (df['open'].shift(-1) >= df['high'])) | \
                   ((df['close'].shift(-2) > df['high']) & (df['close'].shift(-2) > df['high'].shift(-1)))
        else:
            return ((df['close'].shift(-1) < df['close']) & (df['open'].shift(-1) >= df['close'].shift(-1))) | \
                   ((df['close'].shift(-1) < df['low']) & (df['open'].shift(-1) <= df['low'])) | \
                   ((df['close'].shift(-2) < df['low']) & (df['close'].shift(-2) < df['low'].shift(-1)))

class MACD(Indicator):
    """MACD指标"""
    
    def get_space(self):
        return {
            'fast_period': hp.quniform('fast_period', 10, 12, 1),
            'slow_period': hp.quniform('slow_period', 20, 26, 1),
            'signal_period': hp.quniform('signal_period', 7, 9, 1),
            'macd_extreme': hp.quniform('macd_extreme', 100, 200, 10),
        }

    def calculate(self, df: pd.DataFrame, params: Dict, mode='train', atr_period=None, target_multiplier=None, consume_ratio=None) -> pd.DataFrame:
        params = self.get_params(params)
        df = df.copy()

        # 计算MACD
        macd, signal = self.indicator_calculate(df, params)
        df['macd'], df['signal'] = macd, signal

        # 信号检测：histogram 拐点
        extreme = params.get('macd_extreme', 150)
        hist = macd - signal
        # histogram 止跌回升 + 回调期间 hist 曾为负 + 处于上升趋势
        support_cond = (hist > hist.shift(1)) & (hist.shift(1) <= hist.shift(2)) & (hist.shift(1) < 0) & (macd > 0) & (macd < extreme)
        # histogram 止涨回落 + 反弹期间 hist 曾为正 + 处于下降趋势
        resistance_cond = (hist < hist.shift(1)) & (hist.shift(1) >= hist.shift(2)) & (hist.shift(1) > 0) & (macd < 0) & (macd > -extreme)
        # 未来确认
        if mode == 'check':
            support_cond = self._check_confirmation(df, True, support_cond, atr_period, target_multiplier, consume_ratio)
            resistance_cond = self._check_confirmation(df, False, resistance_cond, atr_period, target_multiplier, consume_ratio)

        # 生成信号
        df['reversal'] = np.select([support_cond, resistance_cond],
                                  ['support reversal', 'resistance reversal'], 'none')
        df['is_strong'] = ((df['reversal'] != 'none')).astype(int)

        return df

    def calculate_score(self, result: Dict, signal_count_target: float) -> float:
        signal_count_target = signal_count_target / 3

        support_f1 = result['strong_support_win_rate']
        resistance_f1 = result['strong_resistance_win_rate']

        if support_f1 > 0 and resistance_f1 > 0:
            score = 2 / (1/support_f1 + 1/resistance_f1)
        else:
            score = 0

        # 信号数量惩罚
        signal_count_penalty = min(1.0, (max(result['strong_support_signals_count'], result['strong_resistance_signals_count']))/signal_count_target)
        
        return score * signal_count_penalty
    

    def indicator_calculate(self, df: pd.DataFrame, params: Dict) -> Tuple[pd.Series]: # -> Tuple[vmacd, signal]
        return self._macd_atr(df['close'], df['high'], df['low'], int(params['fast_period']), int(params['slow_period']), int(params['signal_period']))
    
    def _macd_atr(self, close, high, low, fast_period, slow_period, signal_period):
        ema_fast = close.ewm(span=fast_period,adjust=False).mean()
        ema_slow = close.ewm(span=slow_period,adjust=False).mean()
        vmacd = 100 * (ema_fast - ema_slow) / ATR(high, low, close, slow_period)
        signal = vmacd.ewm(span=signal_period,adjust=False).mean()
        return vmacd, signal

    def _future_confirmation(self, df, is_support):
        if is_support:
            return ((df['close'].shift(-1) > df['close']) & (df['open'].shift(-1) <= df['close'].shift(-1)))
        else:
            return ((df['close'].shift(-1) < df['close']) & (df['open'].shift(-1) >= df['close'].shift(-1)))


class RSI(Indicator):
    """RSI指标"""
    
    def get_space(self):
        return {
            'rsi_period': hp.quniform('rsi_period', 10, 25, 1),
            'oversold': hp.quniform('oversold', 10, 30, 1),
            'overbought': hp.quniform('overbought', 70, 90, 1),
        }
    
    def calculate(self, df: pd.DataFrame, params: Dict, mode='train', atr_period=None, target_multiplier=None, consume_ratio=None) -> pd.DataFrame:
        params = self.get_params(params)
        df = df.copy()

        # 计算RSI
        rsi = self.indicator_calculate(df, params)
        df['rsi'] = rsi

        # 信号检测
        support_cond = (df['rsi'] < params['oversold']) & (df['rsi'] < df['rsi'].shift(1)) & (df['rsi'].shift(1) < params['oversold'])
        resistance_cond = (df['rsi'] > params['overbought']) & (df['rsi'] > df['rsi'].shift(1)) & (df['rsi'].shift(1) > params['overbought'])

        # 未来确认
        if mode == 'check':
            support_cond = self._check_confirmation(df, True, support_cond, atr_period, target_multiplier, consume_ratio)
            resistance_cond = self._check_confirmation(df, False, resistance_cond, atr_period, target_multiplier, consume_ratio)

        # 生成信号
        df['reversal'] = np.select([support_cond, resistance_cond],
                                  ['support reversal', 'resistance reversal'], 'none')
        df['is_strong'] = ((df['reversal'] != 'none')).astype(int)

        return df

    def calculate_win_rate(self, df: pd.DataFrame, look_ahead=10, target_multiplier=1.1, atr_period=20) -> Dict:
        """计算胜率"""
        return calculate_win_rate(df, look_ahead, target_multiplier, atr_period, check_high_low=False)

    def calculate_score(self, result: Dict, signal_count_target: float) -> float:
        signal_count_target = signal_count_target / 3

        support_f1 = result['strong_support_win_rate']
        resistance_f1 = result['strong_resistance_win_rate']
        
        if support_f1 > 0 and resistance_f1 > 0:
            score = 2 / (1/support_f1 + 1/resistance_f1)
        else:
            score = 0
        
        # 信号数量惩罚
        signal_count_penalty = min(1.0, min(result['strong_support_signals_count'], 
                                           result['strong_resistance_signals_count']) / signal_count_target)
        
        return score * signal_count_penalty

    
    def indicator_calculate(self, df: pd.DataFrame, params: Dict) -> pd.Series: # -> rsi
        period = int(params['rsi_period'])
        return self._rsi(df['close'], period)
    
    def _rsi(self, close, period):
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def _future_confirmation(self, df, is_support):
        if is_support:
            return (df['close'].shift(-1) > df['high']) | (df['high'] < df['close'].shift(-2))
        else:
            return (df['close'].shift(-1) < df['low']) | (df['low'] > df['close'].shift(-2))

