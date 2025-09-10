from os import cpu_count
import os
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict
import logging
import time
from pathlib import Path
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import argparse
from tqdm import tqdm
from trend_emotion_timing import anchored_trend_score, trend_score, emotion_score

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class RSRatingCalculator:
    def __init__(self,code_list: List[str],market: str, cache_dir: str = 'data/rs_rating'):
        self.periods = ['12mo', '9mo', '6mo', '3mo']
        self.days_map = {'3mo': 90, '6mo': 180, '9mo': 270, '12mo': 365}
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True, parents=True)
        self.cache_expiry_days = 1
        self.market_index_map = {
            'A500': '000905.SH',  # 中证a500指数
            'CSI300': '000300.SH',  # 沪深300指数
            'HSI': '159920.SZ',         # 恒生指数
            'SP500': 'SPY'      # 标普500指数
        }
        self.code_list = code_list
        self.market = market.upper()
        self.cpu_core = cpu_count()
        # 全局网络请求锁，确保同一时间只有一个网络请求
        self._network_lock = threading.Lock()
        self.memory_cache = {}

    def _get_us_stocks(self) -> pd.DataFrame:
        """获取美股代码列表，带缓存功能"""
        # 检查文件缓存
        cache_path = self.cache_dir / "us_stocks.csv"
        if cache_path.exists():
            cache_time = datetime.fromtimestamp(cache_path.stat().st_mtime)
            if (datetime.now() - cache_time).days < self.cache_expiry_days*30:
                return pd.read_csv(cache_path)
        
        # 获取新的美股代码列表
        us_stocks = ak.stock_us_spot_em()
        if not us_stocks.empty:
            # 保存到文件缓存
            us_stocks.to_csv(cache_path, index=False)
        return us_stocks
        
        
    def _convert_ticker_format(self, ticker: str) -> str:
        """将yfinance格式的股票代码转换为akshare格式"""
        code, market = ticker.split('.')
        if market == 'SS':
            return f'SH.{code}'
        elif market == 'SZ':
            return f'SZ.{code}'
        elif market == 'HK':
            # 港股代码需要补零
            return f'HK.{code.zfill(5)}'
        elif market == 'SH':
            return f'SH.{code}'
        elif market == 'SZ':
            return f'SZ.{code}'
        else:
            raise ValueError(f"不支持的市场类型: {market}")
            
    def _get_stock_data(self, ticker: str, market = 'SP500') -> pd.DataFrame:
        """获取股票历史数据"""
        # 转换股票代码格式
        if market == 'SP500':
            # 美股代码需要转换为东财格式
            us_stocks = self._get_us_stocks()
            if not us_stocks.empty:
                matched_stock = us_stocks[us_stocks['代码'].str.split('.').str[1] == ticker.replace('.', '_')]
                if not matched_stock.empty:
                    ak_ticker = matched_stock.iloc[0]['代码']
                else:
                    raise ValueError(f"无法找到股票代码: {ticker}")
        else:
            ak_ticker = self._convert_ticker_format(ticker)
        
        cache_path = self.cache_dir / f"{ak_ticker}.csv"

        # 内存数据过期？
        if ak_ticker in self.memory_cache:
            logger.debug(f"Successfully load cache {ak_ticker} from memory")
            return self.memory_cache[ak_ticker]
        
        # 网络请求加锁
        with self._network_lock:
            # 检查文件缓存
            if cache_path.exists():
                cache_time = datetime.fromtimestamp(cache_path.stat().st_mtime)
                if (datetime.now() - cache_time).days < self.cache_expiry_days:
                    logger.debug(f"Successfully load cache {ak_ticker} from file")
                    df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
                    self.memory_cache[ak_ticker] = df
                    return df

            delta_days = 365 * 5
            # 从akshare获取数据
            if ak_ticker.startswith('SH.') or ak_ticker.startswith('SZ.'):
                raw_code = ak_ticker.split('.')[1]
                if re.match(r'^(51|15|56|58)', raw_code):
                    # A股ETF数据
                    df = ak.fund_etf_hist_em(symbol=raw_code, period="daily", 
                                            start_date=(datetime.now() - timedelta(days=delta_days)).strftime('%Y%m%d'),
                                            end_date=datetime.now().strftime('%Y%m%d'))
                elif raw_code.startswith('16'):
                    # A股LOF数据
                    df = ak.fund_lof_hist_em(symbol=raw_code, period="daily",
                                            start_date=(datetime.now() - timedelta(days=delta_days)).strftime('%Y%m%d'),
                                            end_date=datetime.now().strftime('%Y%m%d'))
                else:
                    # A股数据
                    df = ak.stock_zh_a_hist(symbol=raw_code, period="daily",
                                            start_date=(datetime.now() - timedelta(days=delta_days)).strftime('%Y%m%d'),
                                            end_date=datetime.now().strftime('%Y%m%d'))
            elif ak_ticker.startswith('HK.'):
                # 港股数据
                raw_code = ak_ticker.split('.')[1]
                df = ak.stock_hk_hist(symbol=raw_code, period="daily",
                                    start_date=(datetime.now() - timedelta(days=delta_days)).strftime('%Y%m%d'),
                                    end_date=datetime.now().strftime('%Y%m%d'))
            else:
                # 美股数据
                df = ak.stock_us_hist(symbol=ak_ticker, period="daily",
                                    start_date=(datetime.now() - timedelta(days=delta_days)).strftime('%Y%m%d'),
                                    end_date=datetime.now().strftime('%Y%m%d'))
            
            if df.empty:
                return pd.DataFrame()
                
            # 重命名列以匹配统一格式
            df = df.rename(columns={
                '日期': 'time_key',
                '开盘': 'open',
                '收盘': 'close',
                '最高': 'high',
                '最低': 'low',
                '成交量': 'volume'
            })
            
            # 设置时间索引
            df['time_key'] = pd.to_datetime(df['time_key'])
            df = df.set_index('time_key')
            
            # 确保数据按时间排序
            df = df.sort_index()
            
            if not df.empty:
                df.to_csv(cache_path)
                
            time.sleep(0.5)  # 避免请求过于频繁
            logger.debug(f"Successfully download data {ak_ticker} {df.shape}")
            self.memory_cache[ak_ticker] = df
            return df
    
    def _calculate_period_return(self, data: pd.DataFrame, period: str) -> float:
        """计算指定周期的收益率"""
        if data.empty or len(data) < 2:
            return 0.0
            
        days = self.days_map[period]
        start_date = data.index[-1] - pd.Timedelta(days=days)
        period_data = data[data.index >= start_date]
        
        if len(period_data) < 2:
            return 0.0
            
        # 使用 iloc 进行位置索引，避免 FutureWarning
        return (period_data['close'].iloc[-1] / period_data['close'].iloc[0] - 1) * 100
    
    def _get_benchmark_tickers(self, market: str) -> List[str]:
        """获取基准指数成分股列表，带缓存功能"""
        # 检查文件缓存
        cache_path = self.cache_dir / f"benchmark_{market}.csv"
        if cache_path.exists():
            cache_time = datetime.fromtimestamp(cache_path.stat().st_mtime)
            if (datetime.now() - cache_time).days < self.cache_expiry_days*30:
                return pd.read_csv(cache_path)['ticker'].tolist()
        
        # 获取新的基准个股列表
        if market == 'A500':
            from utility import a500_generator
            tickers = list(a500_generator())
        elif market == 'CSI300':
            from utility import hs300_generator
            tickers = list(hs300_generator())
        elif market == 'HSI':
            from utility import hsi_ticker_generator
            tickers = list(hsi_ticker_generator())
        elif market == 'GGT':
            from utility import ggt_generator
            tickers = list(ggt_generator())
        elif market == 'SP500':
            from utility import sp_500_generator
            tickers = list(sp_500_generator())
        else:
            raise ValueError(f"不支持的市场类型: {market}")
            
        # 保存到文件缓存
        pd.DataFrame({'ticker': tickers}).to_csv(cache_path, index=False)
        
        return tickers
    
    def _get_benchmark_returns(self, market: str) -> Dict[str, List[float]]:
        """获取基准指数成分股收益率"""
        if f'benchmark_{market}_returns' in self.memory_cache:
            return self.memory_cache[f'benchmark_{market}_returns']
        tickers = self._get_benchmark_tickers(market)
        
        # 计算所有成分股的收益率
        benchmark_returns = {period: [] for period in self.periods}
        
        for ticker in tqdm(tickers, desc="load benchmark data"):
            data = self._get_stock_data(ticker, market)
            if not data.empty:
                for period in self.periods:
                    benchmark_returns[period].append(
                        self._calculate_period_return(data, period)
                    )
        self.memory_cache[f'benchmark_{market}_returns'] = benchmark_returns
        return benchmark_returns
    
    def _calculate_rs_rating(self, ticker: str, benchmark_returns: Dict[str, List[float]], market: str) -> Dict[str, float]:
        """计算股票的RS Rating"""
        if market not in self.market_index_map.keys():
            raise ValueError(f"市场类型必须是 {self.market_index_map.keys()}")
        
        # 获取基准指数成分股收益率
        # benchmark_returns = self._get_benchmark_returns(market)
        
        # 获取目标股票数据
        data = self._get_stock_data(ticker, market)
        
        # 计算各周期的RS Rating
        rs_ratings = {}
        for period in self.periods:
            if not data.empty and period in benchmark_returns:
                stock_return = self._calculate_period_return(data, period)
                benchmark_returns_list = benchmark_returns[period]
                
                # 计算百分位数
                percentile = np.percentile(benchmark_returns_list, 100)
                if percentile != 0:
                    rs_rating = (stock_return / percentile) * 100
                else:
                    rs_rating = 0
                    
                # 转换为0-100的评分
                rs_ratings[period] = min(max(rs_rating, 0), 100)
            else:
                rs_ratings[period] = 0
        
        return rs_ratings

    def _calculate_beta(self, stock_data: pd.DataFrame, market_data: pd.DataFrame, period: str = '12mo') -> float:
        """计算股票的beta值
        
        Args:
            stock_data: 股票历史数据
            market_data: 大盘指数历史数据
            period: 计算周期，默认12个月
            
        Returns:
            float: beta值
        """
        if stock_data.empty or market_data.empty:
            return 0.0
            
        days = self.days_map[period]
        start_date = stock_data.index[-1] - pd.Timedelta(days=days)
        
        # 获取指定周期的数据
        stock_period = stock_data[stock_data.index >= start_date]
        market_period = market_data[market_data.index >= start_date]
        
        # 确保数据对齐
        common_dates = stock_period.index.intersection(market_period.index)
        if len(common_dates) < 2:
            return 0.0
            
        stock_returns = stock_period.loc[common_dates, 'close'].pct_change().dropna()
        market_returns = market_period.loc[common_dates, 'close'].pct_change().dropna()
        
        # 计算beta
        covariance = np.cov(stock_returns, market_returns)[0][1]
        market_variance = np.var(market_returns)
        
        if market_variance == 0:
            return 0.0
            
        return covariance / market_variance

    def _calculate_trend_emotion_timing(self, code: str, market: str) -> int:
        """
        计算股票的Trend Emotion Timing
        交易信号：
        多头：Timing_Indicator > 1.0（上升趋势+超卖）-> 1
        空头：Timing_Indicator < -1.0（下降趋势+超买）-> -1
        中性：Timing_Indicator > -1.0 and Timing_Indicator < 1.0 -> 0
        """
        if market not in self.market_index_map.keys():
            raise ValueError(f"market type must be one of {self.market_index_map.keys()}")
        # df_benchmark = self._get_stock_data(self.market_index_map[market], market)[-1000:]
        df_stock = self._get_stock_data(code, market)
        if df_stock.empty or len(df_stock) < 500:
            return 0
        df_stock = df_stock[-1000:]
        # close_ratio = df_stock['close'] / df_benchmark['close']

            
        score_trend = trend_score(df_stock['close'])
        score_emotion = emotion_score(df_stock['close'])
        anchored_score = anchored_trend_score(score_trend, score_emotion)
        timing_indicator = anchored_score - score_emotion
        if timing_indicator.empty:
            return 0
        # 筛选上升趋势中超卖的股票 金色对角线​​：|Timing_Indicator| > 1.0（择时机会）
        if timing_indicator.iloc[-1] > 1:
            return 1
        elif timing_indicator.iloc[-1] < -1:
            return -1
        else:
            return 0
    
    def _process_single_stock(self, ticker: str, benchmark_returns: Dict[str, List[float]], stock_data: pd.DataFrame, market_data: pd.DataFrame) -> tuple:
        """处理单个股票的计算，用于并行执行"""
        try:
            if stock_data.empty:
                raise ValueError(f"Failed to get stock data {ticker}")
                
            if market_data.empty:
                raise ValueError(f"Failed to get market index data {self.market}")
                
            # 相对大盘表现、beta值、趋势情绪择时
            rs_ratings = self._calculate_rs_rating(ticker, benchmark_returns, self.market)
            beta = self._calculate_beta(stock_data, market_data)
            trend_emotion_timing = self._calculate_trend_emotion_timing(ticker, self.market)
            
            result = {
                'beta': beta,
                'trend_emotion_timing': trend_emotion_timing
            }
            result.update(rs_ratings)
            
            return ticker, result
            
        except Exception as e:
            raise e

    def calculate(self) -> Dict[str, Dict[str, float]]:
        """计算股票的RS Rating、Beta值、Trend Emotion Timing
        
        Args:
            ticker: 股票代码
            market: 市场类型 ('A500', 'CSI300', 'HSI', 'SP500')
            
        Returns:
            Dict: 包含RS Rating、Beta值、Trend Emotion Timing的字典
        """
        market = self.market
        if market not in self.market_index_map.keys():
            raise ValueError(f"Market type must be one of {self.market_index_map.keys()}")
        
        results = {}
        print(f"get benchmark returns for {market}")
        benchmark_returns = self._get_benchmark_returns(market)
        
        # 预加载数据
        stock_data = {}
        for ticker in tqdm(self.code_list, desc="load stock data"):
            stock_data[ticker] = self._get_stock_data(ticker, market)
        market_data = self._get_stock_data(self.market_index_map[market], market)
        
        # 使用线程池进行并行处理
        assert self.cpu_core is not None and self.cpu_core >= 1
        with ThreadPoolExecutor(max_workers=min(self.cpu_core, len(self.code_list))) as executor:
            # 提交所有任务
            future_to_ticker = {
                executor.submit(self._process_single_stock, ticker, benchmark_returns, stock_data[ticker], market_data): ticker 
                for ticker in self.code_list
            }
            
            # 收集结果
            for future in tqdm(as_completed(future_to_ticker), total=len(future_to_ticker), desc="calculate rating/trend/emotion/timing"):
                ticker = future_to_ticker[future]
                try:
                    ticker, result = future.result()
                    if result is not None:
                        results[ticker] = result
                        logger.debug(f"Completed calculation for {ticker}")
                except Exception as e:
                    logger.error(f"Exception occurred while processing stock {ticker}: {str(e)}")
        
        return results
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Rating calculator')
    parser.add_argument('--market', type=str, default='HSI',
                       help='Market type: A500, CSI300, HSI, SP500, GGT')
    parser.add_argument('--code_list', type=str, default='GGT',
                       help='Code list file path or name')
    parser.add_argument('--output_dir', type=str, default='./output',
                       help='Output directory')
    args = parser.parse_args()
    market = args.market
    output_dir = args.output_dir
    os.makedirs(output_dir,exist_ok=True)

    rs = RSRatingCalculator([],market)
    code_list = []
    str_code_list = args.code_list.upper()
    if str_code_list in ['GGT','SP500','A500','CSI300','HSI']:
        code_list = rs._get_benchmark_tickers(str_code_list)
    elif os.path.exists(args.code_list):
        code_list = pd.read_csv(args.code_list,header=None)[0].tolist()
    else:
        code_list = args.code_list.split(',')
    assert len(code_list) > 0
    
    print(f'{market} stock count for rating: {len(code_list)}')
    rs = RSRatingCalculator(code_list,market)
    results = rs.calculate()
    for ticker, result in results.items():
        print(f'{ticker}:')
        for k,v in result.items():
            print(f'{k}: {v:.2f}',end=' ')
        print('-'*50)
    # 保存结果到csv
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    code_result = pd.DataFrame(index=pd.Index(results.keys()),columns=pd.Index(results[list(results.keys())[0]].keys()))
    for ticker, result in results.items():
        code_result.loc[ticker] = pd.Series(result,index=code_result.columns)
    code_result.to_csv(os.path.join(output_dir,f'rating_{market}_{timestamp}.csv'), index=True)