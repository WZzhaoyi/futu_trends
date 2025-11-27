"""
港股烟蒂股筛选脚本
基于格雷厄姆的烟蒂股投资策略，筛选符合以下条件的港股：
一等：现金及等价物减去总负债大于总市值
二等：现金及等价物减去有息负债大于总负债
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import time
import logging
from typing import List, Tuple, Dict, Optional
import os

from utility import hk_all_ticker_generator

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class HKCigarButtScreener:
    """港股烟蒂股筛选器"""
    
    def __init__(self, output_dir: str = "output"):
        """
        初始化筛选器
        
        Args:
            output_dir: 输出目录路径
        """
        self.output_dir = output_dir
        self.ensure_output_dir()
        
    def ensure_output_dir(self):
        """确保输出目录存在"""
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
            logger.info(f"创建输出目录: {self.output_dir}")
    
    def get_financial_data(self, ticker: str) -> Optional[Dict]:
        """
        获取股票的财务数据
        
        Args:
            ticker: 股票代码
            
        Returns:
            包含财务数据的字典，如果获取失败返回None
        """
        try:

            proxy = 'http://127.0.0.1:10802'
            yf.set_config(proxy=proxy)

            stock = yf.Ticker(ticker)
            
            # 获取基本信息
            info = stock.info
            if not info:
                logger.warning(f"无法获取 {ticker} 的基本信息")
                return None
            
            # 获取资产负债表
            balance_sheet = stock.quarterly_balance_sheet
            if balance_sheet is None or balance_sheet.empty:
                logger.warning(f"无法获取 {ticker} 的资产负债表")
                return None
            
            # 获取最新财报数据（第一列是最新的）
            latest_balance = balance_sheet.iloc[:, 0]

            income_statement = stock.income_stmt
            if income_statement is None or income_statement.empty:
                logger.warning(f"无法获取 {ticker} 的利润表")
                return None
            
            latest_income = income_statement.iloc[:, 0]
            
            # 调试信息：显示可用的属性名称
            # print(f"{ticker} 可用的balance-sheet属性: {list(balance_sheet.index)}")
            
            # 提取关键财务指标（严格使用yfinance标准属性名称）
            market_cap = info.get('marketCap', 0)
            current_price = info.get('currentPrice', 0)
            shares_outstanding = info.get('sharesOutstanding', 0)
            net_income = latest_income.get('Net Income', 0)
            shareholders_equity = latest_balance.get('Stockholders Equity', 0)
            
            # 计算市盈率和市净率
            pe_ratio = 0
            pb_ratio = 0
            
            if net_income > 0 and shares_outstanding > 0:
                eps = net_income / shares_outstanding  # 每股收益
                if eps > 0:
                    pe_ratio = current_price / eps  # 市盈率
            
            if shareholders_equity > 0 and shares_outstanding > 0:
                book_value_per_share = shareholders_equity / shares_outstanding  # 每股净资产
                if book_value_per_share > 0:
                    pb_ratio = current_price / book_value_per_share  # 市净率
            
            financial_data = {
                'ticker': ticker,
                'company_name': info.get('longName', ''),
                'market_cap': market_cap,  # 总市值
                'current_price': current_price,  # 当前价格
                'shares_outstanding': shares_outstanding,  # 流通股数
                'cash_and_equivalents': latest_balance.get('Cash And Cash Equivalents', 0),  # 现金及等价物
                'total_debt': latest_balance.get('Total Debt', 0),  # 总负债
                'total_liabilities': latest_balance.get('Total Liabilities Net Minority Interest', 0),  # 总负债 少数股东权益净额
                'current_assets': latest_balance.get('Current Assets', 0),  # 流动资产
                'total_assets': latest_balance.get('Total Assets', 0),  # 总资产
                'shareholders_equity': shareholders_equity,  # 股东权益
                'net_income': net_income,  # 净利润
                'pe_ratio': pe_ratio,  # 市盈率
                'pb_ratio': pb_ratio,  # 市净率
            }
            
            # 计算有息负债 简化为短期负债
            current_liabilities = latest_balance.get('Current Liabilities', 0)
            financial_data['interest_bearing_debt'] = current_liabilities  # 简化处理
            
            return financial_data
            
        except Exception as e:
            logger.error(f"获取 {ticker} 财务数据时出错: {str(e)}")
            return None
    
    def calculate_cigar_butt_metrics(self, financial_data: Dict) -> Dict:
        """
        计算烟蒂股指标和理想条件
        
        Args:
            financial_data: 财务数据字典
            
        Returns:
            包含计算结果的字典
        """
        try:
            cash = financial_data.get('cash_and_equivalents', 0)
            total_debt = financial_data.get('total_liabilities', 0)
            market_cap = financial_data.get('market_cap', 0)
            interest_bearing_debt = financial_data.get('interest_bearing_debt', 0)
            net_income = financial_data.get('net_income', 0)
            pe_ratio = financial_data.get('pe_ratio', 0)
            pb_ratio = financial_data.get('pb_ratio', 0)
            
            # 一等条件：现金及等价物减去总负债大于总市值
            condition_1 = (cash - total_debt) > market_cap if market_cap > 0 else False
            condition_1_value = (cash - total_debt) - market_cap if market_cap > 0 else 0
            
            # 二等条件：现金及等价物减去有息负债大于总负债
            condition_2 = (cash - interest_bearing_debt) > total_debt if total_debt > 0 else False
            condition_2_value = (cash - interest_bearing_debt) - total_debt if total_debt > 0 else 0
            
            # 理想条件判断
            ideal_pe = pe_ratio < 13 and pe_ratio > 0  # 市盈率小于13且大于0
            ideal_pb = pb_ratio < 1 and pb_ratio > 0   # 市净率小于1且大于0
            ideal_cash_coverage = cash >= total_debt    # 现金足够支付总负债
            ideal_cash_market_cap = cash >= market_cap  # 现金足够支付市值
            
            # 计算其他有用指标
            net_cash = cash - total_debt
            cash_to_market_cap_ratio = cash / market_cap if market_cap > 0 else 0
            cash_to_debt_ratio = cash / total_debt if total_debt > 0 else 0
            debt_to_equity_ratio = total_debt / financial_data.get('shareholders_equity', 1) if financial_data.get('shareholders_equity', 0) > 0 else 0
            
            metrics = {
                # 基本条件
                'condition_1_met': condition_1,
                'condition_1_value': condition_1_value,
                'condition_2_met': condition_2,
                'condition_2_value': condition_2_value,
                
                # 理想条件
                'ideal_pe': ideal_pe,
                'ideal_pb': ideal_pb,
                'ideal_cash_coverage': ideal_cash_coverage,
                'ideal_cash_market_cap': ideal_cash_market_cap,
                
                # 关键指标
                'net_cash': net_cash,
                'net_income': net_income,
                'market_cap': market_cap,
                'pe_ratio': pe_ratio,
                'pb_ratio': pb_ratio,
                'cash_to_market_cap_ratio': cash_to_market_cap_ratio,
                'cash_to_debt_ratio': cash_to_debt_ratio,
                'debt_to_equity_ratio': debt_to_equity_ratio,
                
                # 理想条件汇总
                'meets_ideal_conditions': ideal_pe and ideal_pb and ideal_cash_coverage
            }
            
            return metrics
            
        except Exception as e:
            logger.error(f"计算烟蒂股指标时出错: {str(e)}")
            return {}
    
    def screen_hk_stocks(self, max_stocks: int = 1000, delay: float = 0.5) -> List[Dict]:
        """
        筛选港股烟蒂股
        
        Args:
            max_stocks: 最大检查股票数量
            delay: 请求间隔时间（秒）
            
        Returns:
            符合条件的股票列表
        """
        logger.info("开始筛选港股烟蒂股...")
        
        cigar_butt_stocks = []
        checked_count = 0
        
        for ticker in hk_all_ticker_generator():
            if checked_count >= max_stocks:
                break
                
            checked_count += 1
            logger.info(f"检查第 {checked_count} 只股票: {ticker}")
            
            # 获取财务数据
            financial_data = self.get_financial_data(ticker)
            if not financial_data:
                time.sleep(delay)
                continue
            
            # 计算烟蒂股指标
            metrics = self.calculate_cigar_butt_metrics(financial_data)
            if not metrics:
                time.sleep(delay)
                continue
            
            # 检查是否满足盈利条件
            if metrics.get('net_income', 0) <= 0:
                continue
            
            # 满足任一现金条件和理想条件即可入选
            basic_conditions = metrics.get('condition_1_met', False) or metrics.get('condition_2_met', False)
            ideal_conditions = metrics.get('meets_ideal_conditions', False)
            
            if basic_conditions and ideal_conditions:
                stock_info = {**financial_data, **metrics}
                cigar_butt_stocks.append(stock_info)
                
                condition_type = "理想条件" if metrics.get('condition_1_met', False) else "基本条件"
                logger.info(f"发现烟蒂股({condition_type}): {ticker} - {financial_data.get('company_name', '')}")
            
            time.sleep(delay)
        
        logger.info(f"筛选完成，共检查 {checked_count} 只股票，发现 {len(cigar_butt_stocks)} 只烟蒂股")
        return cigar_butt_stocks
    
    def save_results(self, stocks: List[Dict], filename: Optional[str] = None) -> str:
        """
        保存筛选结果到CSV文件
        
        Args:
            stocks: 股票数据列表
            filename: 文件名，如果为None则自动生成
            
        Returns:
            保存的文件路径
        """
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"hk_cigar_butt_stocks_{timestamp}.csv"
        
        filepath = os.path.join(self.output_dir, filename)
        
        if not stocks:
            logger.warning("没有找到符合条件的股票")
            return filepath
        
        # 转换为DataFrame
        df = pd.DataFrame(stocks)
        
        # 选择要保存的列
        columns_to_save = [
            # 基本信息
            'ticker', 'company_name', 'current_price', 'market_cap', 'shares_outstanding',
            
            # 财务数据
            'cash_and_equivalents', 'total_liabilities', 'total_debt', 'interest_bearing_debt',
            'current_assets', 'total_assets', 'shareholders_equity', 'net_income',
            
            # 估值指标
            'pe_ratio', 'pb_ratio',
            
            # 基本条件
            'condition_1_met', 'condition_1_value', 'condition_2_met', 'condition_2_value',
            
            # 理想条件
            'ideal_pe', 'ideal_pb', 'ideal_cash_coverage', 'ideal_cash_market_cap', 'meets_ideal_conditions',
            
            # 其他指标
            'net_cash', 'cash_to_market_cap_ratio', 'cash_to_debt_ratio', 'debt_to_equity_ratio'
        ]
        
        # 确保所有列都存在
        available_columns = [col for col in columns_to_save if col in df.columns]
        df_save = df[available_columns]
        
        # 按理想条件优先排序
        df_save = df_save.sort_values(['meets_ideal_conditions', 'ideal_pe', 'ideal_pb', 'cash_to_debt_ratio'], 
                                    ascending=[False, True, True, False])
        
        # 保存到CSV
        df_save.to_csv(filepath, index=False, encoding='utf-8-sig')
        
        logger.info(f"筛选结果已保存到: {filepath}")
        logger.info(f"共找到 {len(stocks)} 只烟蒂股")
        
        return filepath
    
    def run_screening(self, max_stocks: int = 1000, delay: float = 0.5) -> str:
        """
        运行完整的筛选流程
        
        Args:
            max_stocks: 最大检查股票数量
            delay: 请求间隔时间（秒）
            
        Returns:
            保存的文件路径
        """
        # 执行筛选
        cigar_butt_stocks = self.screen_hk_stocks(max_stocks, delay)
        
        # 保存结果
        filepath = self.save_results(cigar_butt_stocks)
        
        return filepath


def main():
    """主函数"""
    screener = HKCigarButtScreener()
    
    # 运行筛选（限制检查数量以避免API限制）
    result_file = screener.run_screening(max_stocks=10000, delay=1.5)
    
    print(f"筛选完成，结果保存在: {result_file}")


if __name__ == "__main__":
    main()
