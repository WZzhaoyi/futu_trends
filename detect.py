import sys
import os

from matplotlib import pyplot as plt

from signal_analysis import KD_analysis
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from configparser import ConfigParser
import pandas as pd
from data import get_kline
from ft_config import get_config
import json
from datetime import datetime
from tools import code_in_futu_group

def run_analysis(code_list:list[str], config:ConfigParser, output_dir='./output', data_dir='./data'):
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    
    results = {}
    name_list = code_list['name']

    for idx, code in enumerate(code_list['code'].values):
        print(f"\n---- Analyzing {name_list[idx]} ----\n")
        
        # 读取数据
        df = get_kline(code, config, max_count=1000)
        timestamp = datetime.now().strftime('%Y%m%d')
        file_name = f'data_{code.replace(".", "_")}_{timestamp}.csv'
        output_file = os.path.join(data_dir, file_name)
        df.to_csv(output_file)

        result = KD_analysis(df, code, evals=500, pl=True)

        # 保存详细信号数据
        output_file = os.path.join(output_dir, f'signals_{code.replace(".", "_")}_{timestamp}.csv')
        result['signal'].to_csv(output_file)
        # 信号图保存到文件
        if result['plot'] is not None:
            pic_file = os.path.join(output_dir, f'signals_{code.replace(".", "_")}_{timestamp}.png')
            with open(pic_file, 'wb') as f:
                f.write(result['plot'].getvalue())

        # 删除detailed_df详细信号数据
        del result["signal"]
        del result["plot"]
        results[code] = result
        
    # 保存参数优化结果
    timestamp = datetime.now().strftime('%Y%m%d')
    summary_file = os.path.join(output_dir, f'analysis_params_{timestamp}.json')
    with open(summary_file, 'w') as f:
        json.dump(results, f, indent=4)
    
    print(f"\nAnalysis complete. Results saved to {output_dir}")
    return results

if __name__ == '__main__':
    config = get_config()

    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    group = config.get("CONFIG", "FUTU_GROUP")

    ls = code_in_futu_group(group,host,port)

    timestamp = datetime.now().strftime('%Y%m%d')
    output_dir = f'./output/detect_{timestamp}'
    results = run_analysis(ls, config, output_dir=output_dir)