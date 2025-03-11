from datetime import datetime
import time
from asyncio.log import logger
from typing import Generator, List, Tuple
import os
import pandas as pd

from .stock_info import Stock_Info
from .utility import (
    hk_ticker_generator,
    shanghai_ticker_generator,
    shenzhen_ticker_generator,
    b_ticker_generator,
    techboard_ticker_generator,
    sp_500_generator,
    nasdaq_100_generator,
    kc50_generator,
    a500_generator,
    hstech_ticker_generator,
    hsi_ticker_generator,
    yfinance_to_tdx_ebk
)

class StockAnalyzer:
    def __init__(self, config):
        self.config = config
        self.ticker_list = config.get("CONFIG", "TICKER_LIST").split(',')
        self.output_path = self._get_absolute_path(config.get("CONFIG", "TICKER_LIST_PATH"))
        self.ticker_generators = self._initialize_generators()

    def _get_absolute_path(self, path: str) -> str:
        """处理路径，转换为绝对路径"""
        if os.path.isabs(path):
            return path
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.abspath(os.path.join(base_dir, path))

    def _initialize_generators(self) -> List[Generator]:
        """初始化所有股票代码生成器"""
        generator_map = {
            'shanghai': shanghai_ticker_generator,
            'shenzhen': shenzhen_ticker_generator,
            'b': b_ticker_generator,
            'techboard': techboard_ticker_generator,
            'hk': hk_ticker_generator,
            'sp500': sp_500_generator,
            'nasdaq100': nasdaq_100_generator,
            'kc50': kc50_generator,
            'a500': a500_generator,
            'hstech': hstech_ticker_generator,
            'hsi': hsi_ticker_generator
        }
        
        generators = []
        for ticker_type in self.ticker_list:
            if ticker_type not in generator_map:
                raise ValueError(f"不支持的 ticker_type: {ticker_type}")
            generators.append(generator_map[ticker_type])
        
        return generators

    def roe_filter(self) -> List[Tuple[str, float]]:
        """执行ROE过滤"""
        stock_watch_list = []
        unique_tickers = set()

        for generator in self.ticker_generators:
            for ticker in generator():
                if ticker in unique_tickers:
                    continue
                unique_tickers.add(ticker)
                
                try:
                    stock = Stock_Info(ticker)
                    roe = stock.roe_filter(0.15, 0.09)
                    if roe[0]:
                        average_roe = roe[1]
                        stock_watch_list.append((ticker, average_roe))
                        logger.info(f"ticker {ticker} with roe = {average_roe} has been appended to stock watch list")
                    time.sleep(1)
                except Exception as e:
                    logger.error(f"处理 {ticker} 时出错: {str(e)}")
        
        return stock_watch_list

    def piotroski_score_filter(self, stock_list: List[Tuple[str, float]], percent=1, score_limit=4) -> List[Tuple[str, int]]:
        """执行 Piotroski 评分过滤"""
        stock_watch_list_pscore = []
        for ticker, _ in stock_list:
            try:
                stock = Stock_Info(ticker)
                score = stock.piotroski_score()
                if score > score_limit:
                    stock_watch_list_pscore.append((ticker, score))
            except Exception as e:
                logger.error(f"计算 {ticker} 的 Piotroski 评分时出错: {str(e)}")
        
        stock_watch_list_pscore.sort(key=lambda a: a[1])
        list_length = len(stock_watch_list_pscore)
        top_twenty_percent = int(list_length * percent)
        return stock_watch_list_pscore[-top_twenty_percent:]

    def save_results(self, stock_watch_list: List[Tuple[str, float]], 
                    stock_piotroski_list: List[Tuple[str, int]]):
        """保存分析结果到CSV文件"""
        os.makedirs(self.output_path, exist_ok=True)
        
        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        tickers = "_".join(self.ticker_list)
        
        # 转换为DataFrame并保存
        df_stock_watch = pd.DataFrame(stock_watch_list, columns=['Ticker', 'Average ROE'])
        df_piotroski = pd.DataFrame(stock_piotroski_list, columns=['Ticker', 'Piotroski Score'])
        
        stock_list_file = os.path.join(self.output_path, f"stock_list_{current_time}_{tickers}.ebk")
        stock_piotroski_file = os.path.join(self.output_path, f"stock_piotroski_list_{current_time}_{tickers}.csv")
        
        # df_stock_watch.to_csv(stock_list_file, index=False)
        yfinance_to_tdx_ebk(df_piotroski['Ticker'].tolist(), stock_list_file)
        df_piotroski.to_csv(stock_piotroski_file, index=False)
        
        logger.info(f"分析结果已保存到: {self.output_path}")

    def run_analysis(self):
        """运行完整的分析流程"""
        # 步骤1: ROE过滤
        stock_watch_list = self.roe_filter()
        stock_watch_list.sort(key=lambda a: a[1])
        
        # 步骤2: Piotroski评分过滤
        stock_piotroski_list = self.piotroski_score_filter(stock_watch_list)
        
        # 保存结果
        self.save_results(stock_watch_list, stock_piotroski_list)
        
        logger.info(f"最终目标股票及其评分: {stock_piotroski_list}")
        return stock_piotroski_list

"""
Rank from lowest score to highest score for further analysis: 
remove the ones less than score 5
"""
