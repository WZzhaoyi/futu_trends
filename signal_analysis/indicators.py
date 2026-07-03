from abc import ABC, abstractmethod
import pandas as pd
import numpy as np
from typing import Dict, Tuple
from hyperopt import hp

from signal_analysis.tool import ATR, calculate_win_rate, calculate_objective_win_rate

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

    def calculate_objective_result(self, df: pd.DataFrame) -> Dict:
        """训练阶段的轻量胜率结果；默认与 calculate_win_rate 的 check_high_low=True 口径一致。"""
        return calculate_objective_win_rate(df, check_high_low=True)

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
        d = k.ewm(span=d_period, adjust=False).mean()
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
            return ((df['close'].shift(-1) > df['close']) & (df['open']<= df['close']))
        else:
            return ((df['close'].shift(-1) < df['close']) & (df['open'] >= df['close']))


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

    def calculate_objective_result(self, df: pd.DataFrame) -> Dict:
        """RSI 训练口径不使用 recent high/low 约束，保持与 calculate_win_rate 一致。"""
        return calculate_objective_win_rate(df, check_high_low=False)

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


class SupportResistance(Indicator):
    """水平支撑阻力指标：pivot 聚类 + ATR 容差 + 突破/跌破/触位确认"""

    # check 阶段的量能确认阈值（× 20 日均量 × volume_ratio）。固定启发式，不参与参数优化：
    # 穿越与企稳类要求显著放量（USE/CNE 双市场胜率随阈值单调改善）；
    # resistance reject 仅要求不缩量——A 股阻力位放巨量滞涨多为对倒，更高阈值反而失效。
    VOLUME_CONFIRM_RATIO = {
        'breakout resistance': 1.2,
        'breakdown support': 1.2,
        'support hold': 1.2,
        'resistance reject': 1.0,
    }

    def get_space(self):
        return {
            'lookback': hp.quniform('lookback', 80, 240, 10),
            'pivot_window': hp.quniform('pivot_window', 3, 8, 1),
            'min_touches': hp.quniform('min_touches', 3, 5, 1),
            'tolerance_atr': hp.quniform('tolerance_atr', 0.0, 1.5, 0.1),
            'breakout_buffer_atr': hp.quniform('breakout_buffer_atr', 0.0, 1.0, 0.1),
        }

    def get_params(self, params: Dict) -> Dict:
        defaults = {
            'lookback': 80,
            'pivot_window': 3,
            'min_touches': 2,
            'tolerance_atr': 0.35,
            'breakout_buffer_atr': 0.2,
            'volume_ratio': 1.0,
        }
        params = {**defaults, **params}
        return {
            'lookback': int(params['lookback']),
            'pivot_window': int(params['pivot_window']),
            'min_touches': int(params['min_touches']),
            'tolerance_atr': round(float(params['tolerance_atr']), 2),
            'breakout_buffer_atr': round(float(params['breakout_buffer_atr']), 2),
            'volume_ratio': round(float(params['volume_ratio']), 2),
        }

    def calculate(self, df: pd.DataFrame, params: Dict, mode='train', atr_period=None, target_multiplier=None, consume_ratio=None) -> pd.DataFrame:
        params = self.get_params(params)
        df = df.copy()

        (
            support_cond,
            resistance_cond,
            sr_signal,
            support_level,
            resistance_level,
            support_touches,
            resistance_touches,
            support_strength,
            resistance_strength,
            near_support,
            near_resistance,
        ) = self.indicator_calculate(df, params)

        if mode == 'check':
            df['sr_signal'] = sr_signal
            df['support_level'] = support_level
            df['resistance_level'] = resistance_level
            df['_sr_volume_ratio'] = params['volume_ratio']
            support_cond = self._check_confirmation(df, True, support_cond, atr_period, target_multiplier, consume_ratio)
            resistance_cond = self._check_confirmation(df, False, resistance_cond, atr_period, target_multiplier, consume_ratio)
            sr_signal = sr_signal.where(support_cond | resistance_cond, 'none')
            df = df.drop(columns=['_sr_volume_ratio'])

        df['support_level'] = support_level
        df['resistance_level'] = resistance_level
        df['support_touches'] = support_touches
        df['resistance_touches'] = resistance_touches
        df['support_strength'] = support_strength
        df['resistance_strength'] = resistance_strength
        df['near_support'] = near_support
        df['near_resistance'] = near_resistance
        df['sr_signal'] = sr_signal
        df['reversal'] = np.select([support_cond, resistance_cond],
                                  ['support reversal', 'resistance reversal'], 'none')
        df['is_strong'] = ((df['reversal'] != 'none')).astype(int)
        return df

    def calculate_score(self, result: Dict, signal_count_target: float) -> float:
        support_f1 = result['strong_support_win_rate']
        resistance_f1 = result['strong_resistance_win_rate']

        if support_f1 > 0 and resistance_f1 > 0:
            score = 2 / (1 / support_f1 + 1 / resistance_f1)
        else:
            score = 0

        signal_count_penalty = min(1.0, min(result['strong_support_signals_count'],
                                           result['strong_resistance_signals_count']) / signal_count_target)
        return score * signal_count_penalty

    def indicator_calculate(self, df: pd.DataFrame, params: Dict) -> Tuple[pd.Series, ...]:
        lookback = int(params['lookback'])
        pivot_window = int(params['pivot_window'])
        min_touches = int(params['min_touches'])
        tolerance_atr = float(params['tolerance_atr'])
        breakout_buffer_atr = float(params['breakout_buffer_atr'])

        high = df['high']
        low = df['low']
        close = df['close']
        open_ = df['open']
        high_values = high.to_numpy(dtype=float)
        low_values = low.to_numpy(dtype=float)
        close_values = close.to_numpy(dtype=float)
        open_values = open_.to_numpy(dtype=float)
        atr_period = max(lookback // 4, 10)
        cache = self._sr_cache(df)
        atr_cache = cache['atr']
        if atr_period not in atr_cache:
            atr_cache[atr_period] = ATR(high, low, close, period=atr_period)
        atr = atr_cache[atr_period]

        pivot_cache = cache['pivots']
        if pivot_window not in pivot_cache:
            confirmed_highs, confirmed_lows = self._confirmed_pivots(df, pivot_window)
            pivot_cache[pivot_window] = (
                self._pivot_arrays(confirmed_highs),
                self._pivot_arrays(confirmed_lows),
            )
        (high_positions, high_prices), (low_positions, low_prices) = pivot_cache[pivot_window]

        n = len(df)
        support_cond = np.zeros(n, dtype=bool)
        resistance_cond = np.zeros(n, dtype=bool)
        sr_signal = np.full(n, 'none', dtype=object)
        support_level = np.full(n, np.nan, dtype=float)
        resistance_level = np.full(n, np.nan, dtype=float)
        support_touches = np.zeros(n, dtype=float)
        resistance_touches = np.zeros(n, dtype=float)
        support_strength = np.zeros(n, dtype=float)
        resistance_strength = np.zeros(n, dtype=float)
        near_support = np.zeros(n, dtype=bool)
        near_resistance = np.zeros(n, dtype=bool)
        atr_values = atr.to_numpy(dtype=float)

        min_index = max(lookback, pivot_window * 2 + 2)
        for i in range(min_index, len(df)):
            curr_atr = atr_values[i]
            if np.isnan(curr_atr) or curr_atr <= 0:
                continue

            start = max(0, i - lookback)
            tolerance = curr_atr * tolerance_atr
            buffer = curr_atr * breakout_buffer_atr

            resistance_levels = self._cluster_levels_in_window(high_positions, high_prices, start, i, tolerance, min_touches)
            support_levels = self._cluster_levels_in_window(low_positions, low_prices, start, i, tolerance, min_touches)
            if not resistance_levels and not support_levels:
                continue

            prev_close = close_values[i - 1]
            last_close = close_values[i]
            last_high = high_values[i]
            last_low = low_values[i]
            last_open = open_values[i]

            resistance = self._nearest_resistance(resistance_levels, prev_close, last_high, tolerance)
            support = self._nearest_support(support_levels, prev_close, last_low, tolerance)
            if resistance is not None:
                resistance_price = resistance['level']
                resistance_level[i] = resistance_price
                resistance_touches[i] = resistance['touches']
                resistance_strength[i] = resistance['touches'] / min_touches
                near_resistance[i] = last_close <= resistance_price and resistance_price - last_close <= tolerance
            if support is not None:
                support_price = support['level']
                support_level[i] = support_price
                support_touches[i] = support['touches']
                support_strength[i] = support['touches'] / min_touches
                near_support[i] = last_close >= support_price and last_close - support_price <= tolerance

            if resistance is not None and prev_close <= resistance_price + buffer and last_close > resistance_price + buffer:
                support_cond[i] = True
                sr_signal[i] = 'breakout resistance'
            elif support is not None and prev_close >= support_price - buffer and last_close < support_price - buffer:
                resistance_cond[i] = True
                sr_signal[i] = 'breakdown support'
            elif support is not None and last_low <= support_price + tolerance and last_close > support_price + buffer and last_close > last_open:
                support_cond[i] = True
                sr_signal[i] = 'support hold'
            elif resistance is not None and last_high >= resistance_price - tolerance and last_close < resistance_price - buffer and last_close < last_open:
                resistance_cond[i] = True
                sr_signal[i] = 'resistance reject'

        return (
            pd.Series(support_cond, index=df.index),
            pd.Series(resistance_cond, index=df.index),
            pd.Series(sr_signal, index=df.index, dtype=object),
            pd.Series(support_level, index=df.index, dtype=float),
            pd.Series(resistance_level, index=df.index, dtype=float),
            pd.Series(support_touches, index=df.index, dtype=float),
            pd.Series(resistance_touches, index=df.index, dtype=float),
            pd.Series(support_strength, index=df.index, dtype=float),
            pd.Series(resistance_strength, index=df.index, dtype=float),
            pd.Series(near_support, index=df.index, dtype=bool),
            pd.Series(near_resistance, index=df.index, dtype=bool),
        )

    def _future_confirmation(self, df, is_support):
        """SR 的确认定义为信号触发 K 线量能达到该信号类型的阈值；名称沿用通用 check 钩子。"""
        if 'volume' not in df.columns:
            return pd.Series(True, index=df.index)

        volume_ratio = float(df['_sr_volume_ratio'].iloc[0]) if '_sr_volume_ratio' in df.columns else 1.0
        cache = self._sr_cache(df)
        if cache['volume_ma20'] is None:
            cache['volume_ma20'] = df['volume'].rolling(20, min_periods=20).mean()
        volume_ma = cache['volume_ma20']
        thresholds = df['sr_signal'].map(self.VOLUME_CONFIRM_RATIO).fillna(1.0)
        return (df['volume'] >= volume_ma * thresholds * volume_ratio).fillna(False)

    def _sr_cache(self, df: pd.DataFrame) -> dict:
        sample_positions = sorted({0, len(df) // 2, len(df) - 1}) if len(df) else []
        value_signature = []
        for column in ('open', 'high', 'low', 'close', 'volume'):
            if column not in df.columns:
                continue
            values = df[column].iloc[sample_positions].to_numpy(dtype=float) if sample_positions else []
            value_signature.append((column, tuple(np.round(values, 8))))
        signature = (
            len(df),
            df.index[0] if len(df.index) else None,
            df.index[-1] if len(df.index) else None,
            tuple(value_signature),
        )
        if getattr(self, '_sr_cache_signature', None) != signature:
            self._sr_cache_signature = signature
            self._sr_cache_data = {'atr': {}, 'pivots': {}, 'volume_ma20': None}
        return self._sr_cache_data

    def _confirmed_pivots(self, df: pd.DataFrame, pivot_window: int) -> Tuple[pd.Series, pd.Series]:
        window = pivot_window * 2 + 1
        high = df['high']
        low = df['low']
        rolling_high = high.rolling(window, center=True, min_periods=window).max()
        rolling_low = low.rolling(window, center=True, min_periods=window).min()
        pivot_high = high.where((high == rolling_high) & rolling_high.notna())
        pivot_low = low.where((low == rolling_low) & rolling_low.notna())

        # pivot 需要右侧 K 线确认；再多 shift 1 根，避免当前 K 线参与本根信号。
        confirmed_highs = pivot_high.shift(pivot_window + 1)
        confirmed_lows = pivot_low.shift(pivot_window + 1)
        return confirmed_highs, confirmed_lows

    def _pivot_arrays(self, pivots: pd.Series) -> Tuple[np.ndarray, np.ndarray]:
        mask = pivots.notna().to_numpy()
        return np.flatnonzero(mask), pivots.to_numpy(dtype=float)[mask]

    def _cluster_levels_in_window(
        self,
        positions: np.ndarray,
        prices: np.ndarray,
        start: int,
        end: int,
        tolerance: float,
        min_touches: int,
    ) -> list[dict[str, float]]:
        left = np.searchsorted(positions, start, side='left')
        right = np.searchsorted(positions, end, side='left')
        if right <= left:
            return []

        window_prices = prices[left:right]
        if window_prices.size > 1:
            window_prices = np.sort(window_prices)

        clusters = []
        for price in window_prices:
            price = float(price)
            if not clusters or abs(price - clusters[-1]['level']) > tolerance:
                clusters.append({'level': price, 'touches': 1})
                continue

            cluster = clusters[-1]
            cluster['level'] = (cluster['level'] * cluster['touches'] + price) / (cluster['touches'] + 1)
            cluster['touches'] += 1

        return [cluster for cluster in clusters if cluster['touches'] >= min_touches]

    def _nearest_resistance(self, levels: list[dict[str, float]], prev_close: float, last_high: float, tolerance: float) -> dict[str, float] | None:
        candidates = [level for level in levels if level['level'] >= prev_close - tolerance or abs(last_high - level['level']) <= tolerance]
        if not candidates:
            return None
        return min(candidates, key=lambda level: abs(level['level'] - max(prev_close, last_high)))

    def _nearest_support(self, levels: list[dict[str, float]], prev_close: float, last_low: float, tolerance: float) -> dict[str, float] | None:
        candidates = [level for level in levels if level['level'] <= prev_close + tolerance or abs(last_low - level['level']) <= tolerance]
        if not candidates:
            return None
        return min(candidates, key=lambda level: abs(level['level'] - min(prev_close, last_low)))
