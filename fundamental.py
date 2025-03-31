from datetime import datetime
import os
from fundamental_analysis import StockAnalyzer, run_forest_analysis, process_stock_data
from ft_config import get_config

def main():
    # 获取配置
    config = get_config()
    
    try:
        # 获取基础目录路径
        base_dir = config.get("CONFIG", "TICKER_LIST_PATH")
        
        # 确保目录存在
        os.makedirs(base_dir, exist_ok=True)
        
        # 默认文件路径
        ticker_list_path = os.path.join(base_dir, f'stock_piotroski_list.csv')  # 股票列表文件
        database_path = os.path.join(base_dir, f'stock_data.db')               # 数据库文件
        output_dir = base_dir                                                 

        # 步骤1: 运行基本面分析获取股票列表
        if not os.path.exists(ticker_list_path):
            print("开始运行基本面分析...")
            analyzer = StockAnalyzer(config)
            stock_piotroski_list, stock_list_file, ticker_list_path = analyzer.run_analysis()
            print("基本面分析完成")
        else:
            print("股票列表文件已存在，跳过基本面分析")
        
        # 步骤2: 处理股票数据并存入数据库
        if not os.path.exists(database_path):
            print("开始处理股票数据...")
            data_engine, database_path = process_stock_data(
                ticker_list_path=ticker_list_path,
                database_path=database_path
            )
            print("股票数据处理完成")
        else:
            print("数据库文件已存在，跳过数据处理")
        
        # 步骤3: 运行森林分析
        print("开始运行森林分析...")
        # 设置分析日期范围
        start_date = datetime(2020, 12, 23)
        end_date = datetime(2024, 2, 1)
        
        # 设置Graphviz路径
        graphviz_path = "C:/Program Files/Graphviz/bin/"
        
        # 运行森林分析
        run_forest_analysis(
            ticker_list_path=ticker_list_path,
            database_path=database_path,
            start_date=start_date,
            end_date=end_date,
            graphviz_path=graphviz_path,
            output_dir=output_dir
        )
        print("森林分析完成")
        
        print("全部分析流程完成")
        
    except Exception as e:
        print(f"分析过程中发生错误: {str(e)}")
        raise

if __name__ == "__main__":
    main() 