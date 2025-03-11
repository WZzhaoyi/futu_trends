from fundamental_analysis import StockAnalyzer
from ft_config import get_config

def main():
    # 获取配置
    config = get_config()
    
    # 初始化分析器
    analyzer = StockAnalyzer(config)
    
    # 运行分析
    analyzer.run_analysis()

if __name__ == "__main__":
    main() 