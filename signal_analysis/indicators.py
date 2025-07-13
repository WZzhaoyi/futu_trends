from abc import ABC, abstractmethod
import pandas as pd
import numpy as np
from typing import Dict, Any
from hyperopt import hp

class Indicator(ABC):
    """技术指标基类"""

    @property
    def name(self):
        return self.__class__.__name__
    
    @abstractmethod
    def calculate(self, df: pd.DataFrame, params: Dict, mode='train') -> pd.DataFrame:
        """计算指标并返回带信号的DataFrame"""
        pass
    
    @abstractmethod
    def get_space(self) -> Dict:
        """返回参数搜索空间"""
        pass

    def get_params(self, params: Dict) -> Dict:
        return {k: int(v) if k.endswith('_period') else round(v, 1) if k == 'strength_threshold' else v 
                  for k, v in params.items()}

    @abstractmethod
    def calculate_score(self, result: Dict, signal_count_target: float) -> float:
        """计算优化评分 - 每个指标可以有自己的评分策略"""
        pass

class KD(Indicator):
    """KD随机指标"""
    
    def get_space(self):
        return {
            'k_period': hp.quniform('k_period', 9, 21, 1),
            'd_period': hp.quniform('d_period', 3, 7, 1),
            'overbought': hp.quniform('overbought', 50, 90, 5),
            'oversold': hp.quniform('oversold', 10, 50, 5),
            'support_ma_period': hp.quniform('support_ma_period', 5, 60, 5),
            'resistance_ma_period': hp.quniform('resistance_ma_period', 5, 60, 5),
            'strength_threshold': hp.quniform('strength_threshold', 0.1, 4, 0.1)
        }
    
    def calculate(self, df: pd.DataFrame, params: Dict, mode='train') -> pd.DataFrame:
        params = {
            'k_period': int(params['k_period']),
            'd_period': int(params['d_period']),
            'overbought': int(params['overbought']),
            'oversold': int(params['oversold']),
            'support_ma_period': int(params['support_ma_period']),
            'resistance_ma_period': int(params['resistance_ma_period']),
            'strength_threshold': round(params['strength_threshold'], 1)
        }
        df = df.copy()
        
        # 计算KD
        k, d = self._stochastic(df['high'], df['low'], df['close'], 
                               int(params['k_period']), int(params['d_period']))
        df['k'], df['d'] = k, d
        df['signal_strength'] = abs(k - d)
        
        # 计算MA
        support_ma = df['close'].rolling(window=int(params['support_ma_period'])).mean()
        resistance_ma = df['close'].rolling(window=int(params['resistance_ma_period'])).mean()
        
        # 信号检测
        support_cond = (k > d) & (k.shift(1) <= d.shift(1)) & (k < params['oversold']) & (df['close'] < support_ma)
        resistance_cond = (k < d) & (k.shift(1) >= d.shift(1)) & (k > params['overbought']) & (df['close'] > resistance_ma)
        
        # 未来确认
        if mode == 'check':
            support_cond &= self._future_confirmation(df, True)
            resistance_cond &= self._future_confirmation(df, False)
        
        # 生成信号
        df['reversal'] = np.select([support_cond, resistance_cond], 
                                  ['support reversal', 'resistance reversal'], 'none')
        df['is_strong'] = ((df['reversal'] != 'none') & 
                          (df['signal_strength'] >= round(params['strength_threshold'], 1))).astype(int)
        
        return df
    
    def calculate_score(self, result: Dict, signal_count_target: float) -> float:
        support_f1 = 2 * (result['strong_support_win_rate'] * result['support_recall']) / \
                    (result['strong_support_win_rate'] + result['support_recall']) if \
                    (result['strong_support_win_rate'] + result['support_recall']) > 0 else 0
        resistance_f1 = 2 * (result['strong_resistance_win_rate'] * result['resistance_recall']) / \
                       (result['strong_resistance_win_rate'] + result['resistance_recall']) if \
                       (result['strong_resistance_win_rate'] + result['resistance_recall']) > 0 else 0
        
        score = (support_f1 + resistance_f1) / 2
        
        # 信号数量惩罚
        signal_count_penalty = min(1.0, min(result['strong_support_signals_count'], 
                                           result['strong_resistance_signals_count']) / signal_count_target)
        
        return score * signal_count_penalty

    def _stochastic(self, high, low, close, k_period, d_period):
        low_min = low.rolling(window=k_period).min()
        high_max = high.rolling(window=k_period).max()
        k = 100 * (close - low_min) / (high_max - low_min)
        d = k.rolling(window=d_period).mean()
        return k, d
    
    def _future_confirmation(self, df, is_support):
        if is_support:
            return ((df['close'].shift(-1) > df['close']) & (df['open'].shift(-1) < df['close'].shift(-1))) | \
                   ((df['close'] < df['close'].shift(-2)) & (df['open'].shift(-1) < df['close'].shift(-1)))
        else:
            return ((df['close'].shift(-1) < df['close']) & (df['open'].shift(-1) > df['close'].shift(-1))) | \
                   ((df['close'] > df['close'].shift(-2)) & (df['open'].shift(-1) > df['close'].shift(-1)))

class MACD(Indicator):
    """MACD指标"""
    
    def get_space(self):
        return {
            'fast_period': hp.quniform('fast_period', 5, 20, 1),
            'slow_period': hp.quniform('slow_period', 10, 40, 1),
            'signal_period': hp.quniform('signal_period', 3, 15, 1),
            'ma_period': hp.quniform('ma_period', 10, 60, 5)
        }
    
    def calculate(self, df: pd.DataFrame, params: Dict, mode='train') -> pd.DataFrame:
        params = {
            'fast_period': int(params['fast_period']),
            'slow_period': int(params['slow_period']),
            'signal_period': int(params['signal_period']),
            'ma_period': int(params['ma_period'])
        }
        df = df.copy()
        
        # 计算MACD
        macd, signal = self._macd(df['close'], 
                                            params['fast_period'], 
                                            params['slow_period'], 
                                            params['signal_period'])
        df['macd'], df['signal'] = macd, signal
        
        # 计算MA
        ma = df['close'].rolling(window=params['ma_period']).mean()
        
        # 信号检测
        support_cond = (macd > signal) & (macd.shift(1) <= signal.shift(1)) & \
                      (df['close'] > ma)
        resistance_cond = (macd < signal) & (macd.shift(1) >= signal.shift(1)) & \
                         (df['close'] < ma)
        
        # 未来确认
        if mode == 'check':
            support_cond &= self._future_confirmation(df, True)
            resistance_cond &= self._future_confirmation(df, False)
        
        # 生成信号
        df['reversal'] = np.select([support_cond, resistance_cond], 
                                  ['support reversal', 'resistance reversal'], 'none')
        df['is_strong'] = ((df['reversal'] != 'none')).astype(int)
        
        return df
    
    def calculate_score(self, result: Dict, signal_count_target: float) -> float:
        signal_count_target = signal_count_target / 2

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
    
    def _macd(self, close, fast_period, slow_period, signal_period):
        ema_fast = close.ewm(span=fast_period).mean()
        ema_slow = close.ewm(span=slow_period).mean()
        macd = ema_fast - ema_slow
        signal = macd.ewm(span=signal_period).mean()
        return macd, signal
    
    def _future_confirmation(self, df, is_support):
        if is_support:
            return ((df['close'].shift(-1) > df['close']) & (df['open'].shift(-1) < df['close'].shift(-1))) | \
                   ((df['close'] < df['close'].shift(-2)) & (df['open'].shift(-1) < df['close'].shift(-2)))
        else:
            return ((df['close'].shift(-1) < df['close']) & (df['open'].shift(-1) > df['close'].shift(-1))) | \
                   ((df['close'] > df['close'].shift(-2)) & (df['open'].shift(-1) > df['close'].shift(-2)))


class RSI(Indicator):
    """RSI指标"""
    
    def get_space(self):
        return {
            'rsi_period': hp.quniform('rsi_period', 5, 25, 1),
            'oversold': hp.quniform('oversold', 10, 30, 5),
            'overbought': hp.quniform('overbought', 70, 90, 5),
        }
    
    def calculate(self, df: pd.DataFrame, params: Dict, mode='train') -> pd.DataFrame:
        params = {
            'rsi_period': int(params['rsi_period']),
            'oversold': int(params['oversold']),
            'overbought': int(params['overbought']),
        }
        df = df.copy()

        # 计算RSI
        rsi = self._rsi(df['close'], params['rsi_period'])

        # 信号检测
        support_cond = (rsi > params['oversold']) & (rsi > rsi.shift(1)) & (rsi.shift(1) < params['oversold'])
        resistance_cond = (rsi < params['overbought']) & (rsi < rsi.shift(1)) & (rsi.shift(1) > params['overbought'])

        # 未来确认
        if mode == 'check':
            support_cond &= self._future_confirmation(df, True)
            resistance_cond &= self._future_confirmation(df, False)

        # 生成信号
        df['reversal'] = np.select([support_cond, resistance_cond], 
                                  ['support reversal', 'resistance reversal'], 'none')
        df['is_strong'] = ((df['reversal'] != 'none')).astype(int)

        return df

    def calculate_score(self, result: Dict, signal_count_target: float) -> float:
        signal_count_target = signal_count_target / 4

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

    def _rsi(self, close, period):
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def _future_confirmation(self, df, is_support):
        if is_support:
            return ((df['close'].shift(-1) > df['close']) & (df['open'].shift(-1) < df['close'].shift(-1))) | \
                   ((df['close'] < df['close'].shift(-2)) & (df['open'].shift(-1) < df['close'].shift(-2)))
        else:
            return ((df['close'].shift(-1) < df['close']) & (df['open'].shift(-1) > df['close'].shift(-1))) | \
                   ((df['close'] > df['close'].shift(-2)) & (df['open'].shift(-1) > df['close'].shift(-2)))

