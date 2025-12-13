import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from ft_config import get_config
from data import get_kline_data
from signal_analysis import KD, MACD, RSI
from tools import EMA
from params_db import ParamsDB
from lightweight_charts import Chart
from configparser import ConfigParser
import pandas as pd
import logging
import os
import asyncio
import time
from datetime import datetime

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class SignalWindow:
    """
    日线图窗口
    """
    def __init__(self, config: ConfigParser, code: str):
        self.config = config
        self.code = code
        self.chart = None
        self.dark_mode = config.getboolean("CONFIG", "DARK_MODE", fallback=True)
        self.data_dir = config.get("CONFIG", "DATA_DIR", fallback="./data/detect")
        asyncio.run(self.load_chart(code, cache_expiry_days=1))
    
    async def _load_single_param(self, code: str, param_type: str):
        """
        异步读取单个指标参数
        
        Args:
            code: 股票代码
            param_type: 参数类型 ('KD', 'MACD', 'RSI')
            
        Returns:
            dict: 参数字典，如果不存在则为None
        """
        db_path = self.config.get("CONFIG", f"{param_type}_PARAMS_DB", fallback=None)
        if db_path is None:
            return None
        
        def _load():
            db = ParamsDB(db_path)
            data = db.get_stock_params(code)
            return data['best_params'] if data is not None else None
        
        return await asyncio.to_thread(_load)
    
    async def _load_historical_data(self, code: str, cache_expiry_days: int = 1):
        """
        异步读取历史数据
        从本地缓存或API加载K线数据，并计算EMA指标
        
        Args:
            code: 股票代码
            cache_expiry_days: 缓存过期天数
            
        Returns:
            pd.DataFrame: 包含K线数据和EMA指标的数据框
        """
        ktype = self.config.get("CONFIG", "FUTU_PUSH_TYPE", fallback="K_DAY")
        if ktype != 'K_DAY':
            logger.error(f"Only K_DAY is supported for signal window currently")
            exit()
        
        def _load_data():
            data_dir = self.data_dir
            os.makedirs(data_dir, exist_ok=True)
            data_file_name = f'data_{code.replace(".", "_")}_{ktype}.csv'
            data_file = os.path.join(data_dir, data_file_name)
            
            if not os.path.exists(data_file) or (datetime.now() - datetime.fromtimestamp(os.path.getmtime(data_file))).days > cache_expiry_days:
                logger.info(f"download new data: {data_file}")
                df = get_kline_data(code, self.config, max_count=1000)
                df.to_csv(data_file)
            else:
                logger.info(f"use local data file: {data_file}")
                df = pd.read_csv(data_file, index_col=0, parse_dates=True)
            
            # 确保索引是 datetime 类型，并处理时区信息
            if not isinstance(df.index, pd.DatetimeIndex):
                # 使用 utc=True 来处理时区感知的 datetime 对象
                df.index = pd.to_datetime(df.index, utc=True)
            
            # 如果是时区感知的 datetime，转换为 UTC 后移除时区信息
            if df.index.tz is not None:
                df.index = df.index.tz_convert('UTC').tz_localize(None)
            
            df['time'] = df.index
            ema_period = self.config.getint("CONFIG", "EMA_PERIOD", fallback=240)
            df[f'EMA_{ema_period}'] = EMA(df['close'], ema_period)
            return df
        
        return await asyncio.to_thread(_load_data)
    
    def _draw_main_chart(self, code: str, df: pd.DataFrame):
        """
        绘制主图（K线+EMA）
        
        Args:
            code: 股票代码
            df: 包含K线数据和EMA指标的数据框
        """
        self.chart.watermark(code)
        self.chart.set(df[['time', 'open', 'high', 'low', 'close', 'volume']])
        self.chart.candle_style(
            up_color='rgba(255, 255, 255, 0)',
            down_color='rgba(0, 168, 67, 1)',
            border_up_color='rgba(255, 82, 82, 1)',
            border_down_color='rgba(0, 168, 67, 1)',
            border_visible=True,
            wick_up_color='rgba(255, 82, 82, 1)',
            wick_down_color='rgba(0, 168, 67, 1)'
        )
        self.chart.volume_config(
            up_color='rgba(255, 82, 82, 1)',
            down_color='rgba(0, 168, 67, 1)'
        )
        self.chart.legend(visible=True)
        ema_period = self.config.getint("CONFIG", "EMA_PERIOD", fallback=240)
        line = self.chart.create_line(f'EMA_{ema_period}', color='rgba(224,82,211,0.8)', width=1)
        line.set(df[['time', f'EMA_{ema_period}']])
    
    async def _add_macd_subchart(self, df: pd.DataFrame, code: str):
        """异步添加MACD子图（内部读取参数）"""
        # 读取MACD参数
        macd_params = await self._load_single_param(code, "MACD")
        if macd_params is None:
            return
        
        def _calculate():
            df_copy = df.copy()
            macd_vmacd, macd_signal = MACD().indicator_calculate(df_copy, macd_params)
            df_copy['macd_vmacd'] = macd_vmacd
            df_copy['macd_signal'] = macd_signal
            df_copy['macd_hist'] = macd_vmacd - macd_signal
            return df_copy
        
        df = await asyncio.to_thread(_calculate)
        
        background_color = 'rgb(25, 25, 25)' if self.dark_mode else 'rgb(255, 255, 255)'
        text_color = 'rgb(255, 255, 255)' if self.dark_mode else 'rgb(0, 0, 0)'
        
        macd_chart = self.chart.create_subchart(position='bottom', width=1, height=0.13, sync=True)
        macd_chart.layout(background_color=background_color, text_color=text_color)
        macd_chart.legend(visible=True)
        macd_chart.time_scale(visible=False)
        
        hist_series = macd_chart.create_histogram(name='macd_hist')
        hist_data = []
        for idx in range(len(df)):
            row = df.iloc[idx]
            hist_value = row['macd_hist'] if pd.notna(row['macd_hist']) else 0
            prev_value = df['macd_hist'].iloc[idx-1] if idx > 0 and pd.notna(df['macd_hist'].iloc[idx-1]) else 0
            if hist_value>=0 and hist_value>=prev_value:
                color = 'rgba(255,82,82,1)'
            elif hist_value>0 and hist_value<prev_value:
                color = 'rgba(255,205,210,1)'
            elif hist_value<=0 and hist_value>=prev_value:
                color = 'rgba(178,223,219,1)'
            else:
                color = 'rgba(38,166,154,1)'
            hist_data.append({'time': row['time'], 'macd_hist': hist_value, 'color': color})
        hist_df = pd.DataFrame(hist_data)
        hist_series.set(hist_df)
        
        macd_series = macd_chart.create_line('macd_vmacd', color='rgb(255,141,30)', width=1.5)
        signal_series = macd_chart.create_line('macd_signal', color='rgb(12,174,230)', width=1.5)
        macd_series.set(df[['time', 'macd_vmacd']])
        signal_series.set(df[['time', 'macd_signal']])
    
    async def _add_kd_subchart(self, df: pd.DataFrame, code: str):
        """异步添加KD子图（内部读取参数）"""
        # 读取KD参数
        kd_params = await self._load_single_param(code, "KD")
        if kd_params is None:
            return
        
        def _calculate():
            df_copy = df.copy()
            kd_k, kd_d = KD().indicator_calculate(df_copy, kd_params)
            df_copy['kd_k'] = kd_k
            df_copy['kd_d'] = kd_d
            return df_copy
        
        df = await asyncio.to_thread(_calculate)
        
        background_color = 'rgb(25, 25, 25)' if self.dark_mode else 'rgb(255, 255, 255)'
        text_color = 'rgb(255, 255, 255)' if self.dark_mode else 'rgb(0, 0, 0)'
        
        kd_chart = self.chart.create_subchart(position='bottom', width=1, height=0.13, sync=True)
        kd_chart.layout(background_color=background_color, text_color=text_color)
        kd_chart.legend(visible=True)
        kd_chart.time_scale(visible=False)
        kd_k_series = kd_chart.create_line('kd_k', color='rgb(255,141,30)')
        kd_d_series = kd_chart.create_line('kd_d', color='rgb(12,174,230)')
        kd_k_series.set(df[['time', 'kd_k']])
        kd_d_series.set(df[['time', 'kd_d']])
        kd_oversold_series = kd_chart.create_line('kd_oversold', color='gray', style='dashed', width=1)
        df['kd_oversold'] = kd_params['oversold']
        kd_oversold_series.set(df[['time', 'kd_oversold']])
        kd_overbought_series = kd_chart.create_line('kd_overbought', color='gray', style='dashed', width=1)
        df['kd_overbought'] = kd_params['overbought']
        kd_overbought_series.set(df[['time', 'kd_overbought']])
    
    async def _add_rsi_subchart(self, df: pd.DataFrame, code: str):
        """异步添加RSI子图（内部读取参数）"""
        # 读取RSI参数
        rsi_params = await self._load_single_param(code, "RSI")
        if rsi_params is None:
            return
        
        def _calculate():
            df_copy = df.copy()
            rsi = RSI().indicator_calculate(df_copy, rsi_params)
            df_copy['rsi'] = rsi
            return df_copy
        
        df = await asyncio.to_thread(_calculate)
        
        background_color = 'rgb(25, 25, 25)' if self.dark_mode else 'rgb(255, 255, 255)'
        text_color = 'rgb(255, 255, 255)' if self.dark_mode else 'rgb(0, 0, 0)'
        
        rsi_chart = self.chart.create_subchart(position='bottom', width=1, height=0.13, sync=True)
        rsi_chart.layout(background_color=background_color, text_color=text_color)
        rsi_chart.legend(visible=True)
        rsi_chart.time_scale(visible=False)
        rsi_series = rsi_chart.create_line('rsi', color='rgba(255,141,30,1)')
        rsi_series.set(df[['time', 'rsi']])
        rsi_oversold_series = rsi_chart.create_line('rsi_oversold', color='gray', style='dashed', width=1)
        df['rsi_oversold'] = rsi_params['oversold']
        rsi_oversold_series.set(df[['time', 'rsi_oversold']])
        rsi_overbought_series = rsi_chart.create_line('rsi_overbought', color='gray', style='dashed', width=1)
        df['rsi_overbought'] = rsi_params['overbought']
        rsi_overbought_series.set(df[['time', 'rsi_overbought']])
    
    async def load_chart(self, code: str, cache_expiry_days: int = 1):
        """
        异步加载图表（统一对外接口）
        先加载历史数据并显示主图，然后异步按需加载参数并添加子图
        
        Args:
            code: 股票代码
            cache_expiry_days: 缓存过期天数，默认为1天
            
        Returns:
            Chart: 图表对象
        """
        start_time = time.time()
        logger.info(f"start loading chart: {code}")
        background_color = 'rgb(25, 25, 25)' if self.dark_mode else 'rgb(255, 255, 255)'
        text_color = 'rgb(255, 255, 255)' if self.dark_mode else 'rgb(0, 0, 0)'
        
        self.chart = Chart(toolbox=True, width=1200, height=900, title=f"{code}", inner_height=0.6)
        self.chart.layout(background_color=background_color, text_color=text_color)
        
        # 只读取历史数据（不读取参数，加快主图显示速度）
        df = await self._load_historical_data(code, cache_expiry_days)
        
        # 先绘制主图
        self._draw_main_chart(code, df)
        
        # 异步并行添加子图（每个子图函数内部会读取对应的参数）
        tasks = [
            self._add_macd_subchart(df, code),
            # self._add_kd_subchart(df, code),
            # self._add_rsi_subchart(df, code)
        ]
        
        await asyncio.gather(*tasks)

        end_time = time.time()
        logger.info(f"before show chart: {code}, time: {end_time - start_time} seconds")
        
        # 保持窗口打开（阻塞直到窗口关闭）
        await self.chart.show_async()
        
        return self.chart

if __name__ == '__main__':
    config = get_config()

    code = 'SH.510300'
    signal_window = SignalWindow(config, code)
    print('done')