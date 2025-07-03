import sys
import os

from matplotlib import pyplot as plt

from signal_analysis import technical_analysis
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from configparser import ConfigParser
import pandas as pd
from data import get_kline_data
from ft_config import get_config
import json
from datetime import datetime
from tools import code_in_futu_group

def run_analysis(code_list:pd.DataFrame, config:ConfigParser, output_dir='./output', data_dir='./data/detect', cache_expiry_days=1):
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    
    results = {}
    name_list = code_list['name']

    trend_type = config.get("CONFIG", "TREND_TYPE")
    look_ahead = config.get("CONFIG", "KD_LOOK_AHEAD")
    look_ahead = int(look_ahead) if look_ahead else 0
    ktype = config.get("CONFIG", "FUTU_PUSH_TYPE")
    timestamp = datetime.now().strftime('%Y%m%d')

    indicator_dict = {
        'reverse': 'KD',
        'continue': 'MACD'
    }
    if trend_type not in indicator_dict:
        raise ValueError(f"Invalid trend type: {trend_type}")
    indicator_type = indicator_dict[trend_type]

    for idx, code in enumerate(code_list['code'].values):
        print(f"\n---- Analyzing {name_list[idx]} ----\n")
        
        # 检查本地数据文件
        data_file_name = f'data_{code.replace(".", "_")}_{ktype}.csv'
        data_file = os.path.join(data_dir, data_file_name)
        if not os.path.exists(data_file) or (datetime.now() - datetime.fromtimestamp(os.path.getmtime(data_file))).days > cache_expiry_days:
            # 文件不存在，下载数据
            print(f"下载新数据: {data_file}")
            df = get_kline_data(code, config, max_count=1100)
            df.to_csv(data_file)
        else:
            print(f"使用本地数据文件: {data_file}")
            df = pd.read_csv(data_file, index_col=0, parse_dates=True)

        result_file_name = os.path.join(output_dir, f'signals_{code.replace(".", "_")}_{timestamp}_{ktype}.json')
        if os.path.exists(result_file_name):
            print(f"使用本地结果文件: {result_file_name}")
            with open(result_file_name, 'r') as f:
                result = json.load(f)
        else:
            print(f"训练新结果: {result_file_name}")
            result = technical_analysis(df, code, indicator_type, evals=500, look_ahead=look_ahead)

            # 保存详细信号数据
            singal_file_name = os.path.join(output_dir, f'signals_{code.replace(".", "_")}_{timestamp}_{ktype}.csv')
            result['signal'].to_csv(singal_file_name)
            # 信号图保存到文件
            if result['plot'] is not None:
                pic_file = os.path.join(output_dir, f'signals_{code.replace(".", "_")}_{timestamp}_{ktype}.png')
                with open(pic_file, 'wb') as f:
                    f.write(result['plot'].getvalue())

            if result['checked_plot'] is not None:
                pic_file = os.path.join(output_dir, f'signals_{code.replace(".", "_")}_{timestamp}_checked_{ktype}.png')
                with open(pic_file, 'wb') as f:
                    f.write(result['checked_plot'].getvalue())

            del result["checked_plot"]
            del result["plot"]
            del result["signal"]
            with open(result_file_name, 'w') as f:
                json.dump(result, f, indent=4)
        results[code] = result
        
    # 保存参数优化结果
    summary_file = os.path.join(output_dir, f'analysis_params_{timestamp}_{ktype}.json')
    with open(summary_file, 'w') as f:
        json.dump(results, f, indent=4)
    
    print(f"\nAnalysis complete. Results saved to {output_dir}")
    return results

if __name__ == '__main__':
    config = get_config()

    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    group = config.get("CONFIG", "FUTU_GROUP", fallback='')
    code_list = config.get("CONFIG", "FUTU_CODE_LIST", fallback='').split(',')
    code_list = [code for code in code_list if code.strip()]

    # 获取股票列表
    code_pd = pd.DataFrame(columns=['code','name'])
    if group:
        ls = code_in_futu_group(group,host,port)
        if type(ls) == pd.DataFrame:
            ls = ls[['code','name']]
            code_pd = pd.concat([code_pd,ls])
    if len(code_list) > 0:
        ls = pd.DataFrame(columns=['code','name'])
        ls['code'] = code_list
        ls['name'] = code_list
        code_pd = pd.concat([code_pd,ls], ignore_index=True)

    if code_pd.empty:
        print('warning: no code in config')
        exit()

    timestamp = datetime.now().strftime('%Y%m%d')
    output_dir = f'./output/detect_{timestamp}'
    results = run_analysis(code_pd, config, output_dir=output_dir)