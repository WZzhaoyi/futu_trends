import sys
import os

from matplotlib import pyplot as plt

from signal_analysis import KD_analysis
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from configparser import ConfigParser
import pandas as pd
from data import get_kline_data
from ft_config import get_config
import json
from datetime import datetime
from tools import code_in_futu_group

def run_analysis(code_list:list[str], config:ConfigParser, output_dir='./output', data_dir='./data'):
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    
    results = {}
    name_list = code_list['name']

    look_ahead = config.get("CONFIG", "KD_LOOK_AHEAD")
    look_ahead = int(look_ahead) if look_ahead else 0
    ktype = config.get("CONFIG", "FUTU_PUSH_TYPE")

    for idx, code in enumerate(code_list['code'].values):
        print(f"\n---- Analyzing {name_list[idx]} ----\n")
        
        # 检查本地数据文件
        timestamp = datetime.now().strftime('%Y%m%d')
        file_name = f'data_{code.replace(".", "_")}_{timestamp}_{ktype}.csv'
        output_file = os.path.join(data_dir, file_name)
        
        if os.path.exists(output_file):
            print(f"使用本地数据文件: {output_file}")
            df = pd.read_csv(output_file, index_col=0, parse_dates=True)
        else:
            # 文件不存在，下载数据
            print(f"下载新数据: {output_file}")
            df = get_kline_data(code, config, max_count=1000)
            df.to_csv(output_file)

        result = KD_analysis(df, code, evals=500, pl=True, look_ahead=look_ahead)

        # 保存详细信号数据
        output_file = os.path.join(output_dir, f'signals_{code.replace(".", "_")}_{timestamp}_{ktype}.csv')
        result['signal'].to_csv(output_file)
        # 信号图保存到文件
        if result['plot'] is not None:
            pic_file = os.path.join(output_dir, f'signals_{code.replace(".", "_")}_{timestamp}_{ktype}.png')
            with open(pic_file, 'wb') as f:
                f.write(result['plot'].getvalue())

        # 删除detailed_df详细信号数据
        del result["signal"]
        del result["plot"]
        results[code] = result
        
    # 保存参数优化结果
    timestamp = datetime.now().strftime('%Y%m%d')
    summary_file = os.path.join(output_dir, f'analysis_params_{timestamp}_{ktype}.json')
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