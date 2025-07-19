import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Union
import logging
import time
from pathlib import Path
import re

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RSRatingCalculator:
    def __init__(self, cache_dir: str = 'data/rs_rating'):
        self.periods = ['12mo', '9mo', '6mo', '3mo']
        self.days_map = {'3mo': 90, '6mo': 180, '9mo': 270, '12mo': 365}
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True, parents=True)
        self.cache_expiry_days = 1
        self.market_index_map = {
            'A500': '000905.SH',  # 中证500指数
            'HSI': '159920.SZ',         # 恒生指数
            'SP500': 'SPY'      # 标普500指数
        }
        
    def _get_us_stocks(self) -> pd.DataFrame:
        """获取美股代码列表，带缓存功能"""
        # 检查文件缓存
        cache_path = self.cache_dir / "us_stocks.csv"
        if cache_path.exists():
            cache_time = datetime.fromtimestamp(cache_path.stat().st_mtime)
            if (datetime.now() - cache_time).days < self.cache_expiry_days*30:
                try:
                    return pd.read_csv(cache_path)
                except Exception as e:
                    logger.warning(f"读取美股代码缓存失败: {str(e)}")
        
        # 获取新的美股代码列表
        try:
            us_stocks = ak.stock_us_spot_em()
            if not us_stocks.empty:
                # 保存到文件缓存
                us_stocks.to_csv(cache_path, index=False)
            return us_stocks
        except Exception as e:
            logger.error(f"获取美股代码列表失败: {str(e)}")
            return pd.DataFrame()
        
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
        else:
            raise ValueError(f"不支持的市场类型: {market}")
            
    def _get_stock_data(self, ticker: str, market = 'SP500') -> pd.DataFrame:
        """获取股票12个月的历史数据"""
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
        
        # 检查文件缓存
        if cache_path.exists():
            cache_time = datetime.fromtimestamp(cache_path.stat().st_mtime)
            if (datetime.now() - cache_time).days < self.cache_expiry_days:
                try:
                    return pd.read_csv(cache_path, index_col=0, parse_dates=True)
                except Exception as e:
                    logger.warning(f"读取缓存失败 {ak_ticker}: {str(e)}")
        
        # 从akshare获取数据
        try:
            if ak_ticker.startswith('SH.') or ak_ticker.startswith('SZ.'):
                raw_code = ak_ticker.split('.')[1]
                if re.match(r'^(51|15|56|58)', raw_code):
                    # A股ETF数据
                    df = ak.fund_etf_hist_em(symbol=raw_code, period="daily", 
                                           start_date=(datetime.now() - timedelta(days=365)).strftime('%Y%m%d'),
                                           end_date=datetime.now().strftime('%Y%m%d'))
                elif raw_code.startswith('16'):
                    # A股LOF数据
                    df = ak.fund_lof_hist_em(symbol=raw_code, period="daily",
                                           start_date=(datetime.now() - timedelta(days=365)).strftime('%Y%m%d'),
                                           end_date=datetime.now().strftime('%Y%m%d'))
                else:
                    # A股数据
                    df = ak.stock_zh_a_hist(symbol=raw_code, period="daily",
                                          start_date=(datetime.now() - timedelta(days=365)).strftime('%Y%m%d'),
                                          end_date=datetime.now().strftime('%Y%m%d'))
            elif ak_ticker.startswith('HK.'):
                # 港股数据
                raw_code = ak_ticker.split('.')[1]
                df = ak.stock_hk_hist(symbol=raw_code, period="daily",
                                    start_date=(datetime.now() - timedelta(days=365)).strftime('%Y%m%d'),
                                    end_date=datetime.now().strftime('%Y%m%d'))
            else:
                # 美股数据
                df = ak.stock_us_hist(symbol=ak_ticker, period="daily",
                                    start_date=(datetime.now() - timedelta(days=365)).strftime('%Y%m%d'),
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
            return df
            
        except Exception as e:
            logger.error(f"获取股票数据失败 {ak_ticker}: {str(e)}")
            return pd.DataFrame()
    
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
                try:
                    return pd.read_csv(cache_path)['ticker'].tolist()
                except Exception as e:
                    logger.warning(f"读取基准个股缓存失败 {market}: {str(e)}")
        
        # 获取新的基准个股列表
        try:
            if market == 'A500':
                from utility import zz500_generator
                tickers = list(zz500_generator())
            elif market == 'HSI':
                from utility import hsi_ticker_generator
                tickers = list(hsi_ticker_generator())
            elif market == 'SP500':
                from utility import sp_500_generator
                tickers = list(sp_500_generator())
            else:
                raise ValueError(f"不支持的市场类型: {market}")
                
            # 保存到文件缓存
            pd.DataFrame({'ticker': tickers}).to_csv(cache_path, index=False)
            
            return tickers
            
        except Exception as e:
            logger.error(f"获取基准个股列表失败 {market}: {str(e)}")
            return []
    
    def _get_benchmark_returns(self, market: str) -> Dict[str, List[float]]:
        """获取基准指数成分股收益率"""
        # 使用新的缓存方法获取基准个股列表
        tickers = self._get_benchmark_tickers(market)
        
        # 计算所有成分股的收益率
        benchmark_returns = {period: [] for period in self.periods}
        
        for ticker in tickers:
            data = self._get_stock_data(ticker, market)
            if not data.empty:
                for period in self.periods:
                    benchmark_returns[period].append(
                        self._calculate_period_return(data, period)
                    )
        
        return benchmark_returns
    
    def calculate_rs_rating(self, ticker: str, market: str = 'A500') -> Dict[str, float]:
        """计算股票的RS Rating"""
        if market not in ['A500', 'HSI', 'SP500']:
            raise ValueError("市场类型必须是 'A500', 'HSI' 或 'SP500'")
        
        # 获取基准指数成分股收益率
        benchmark_returns = self._get_benchmark_returns(market)
        
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
        
    def calculate_rs_rating_and_beta(self, ticker: str, market: str = 'A500') -> Dict[str, Union[Dict[str, float], float]]:
        """计算股票的RS Rating和Beta值
        
        Args:
            ticker: 股票代码
            market: 市场类型 ('A500', 'HSI', 'SP500')
            
        Returns:
            Dict: 包含RS Rating和Beta值的字典
        """
        if market not in ['A500', 'HSI', 'SP500']:
            raise ValueError("市场类型必须是 'A500', 'HSI' 或 'SP500'")
            
        # 获取股票数据
        stock_data = self._get_stock_data(ticker, market)
        if stock_data.empty:
            return {'rs_rating': {}, 'beta': 0.0}
            
        # 获取大盘指数数据
        market_index = self.market_index_map[market]
        market_data = self._get_stock_data(market_index, market)
        if market_data.empty:
            return {'rs_rating': {}, 'beta': 0.0}
            
        # 计算RS Rating
        rs_ratings = self.calculate_rs_rating(ticker, market)
        
        # 计算Beta值
        beta = self._calculate_beta(stock_data, market_data)
        
        return {
            'rs_rating': rs_ratings,
            'beta': beta
        }

def main():
    calculator = RSRatingCalculator()
    
    # A股
    a_stock = '603129.SS'
    a_result = calculator.calculate_rs_rating_and_beta(a_stock, 'A500')
    print(f"\nA股 {a_stock} 的分析结果:")
    print(f"RS Rating: {a_result['rs_rating']}")
    print(f"Beta值: {a_result['beta']:.2f}")
    
    # 港股
    hk_stock = '9899.HK'
    hk_result = calculator.calculate_rs_rating_and_beta(hk_stock, 'HSI')
    print(f"\n港股 {hk_stock} 的分析结果:")
    print(f"RS Rating: {hk_result['rs_rating']}")
    print(f"Beta值: {hk_result['beta']:.2f}")
    
    # 美股
    us_stock = 'ESLT'
    us_result = calculator.calculate_rs_rating_and_beta(us_stock, 'SP500')
    print(f"\n美股 {us_stock} 的分析结果:")
    print(f"RS Rating: {us_result['rs_rating']}")
    print(f"Beta值: {us_result['beta']:.2f}")

if __name__ == "__main__":
    main() 