import sys
import os

from matplotlib import pyplot as plt

from signal_analysis import technical_analysis
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from configparser import ConfigParser
import pandas as pd
from data import get_kline_data
from ft_config import get_config
from params_db import ParamsDB
import json
from datetime import datetime
from tools import code_in_futu_group, sanitize_path_component

"""
    运行分析
    code_list: 股票代码列表
    indicator_type: 指标类型
    config: 配置文件
    output_dir: 输出目录
    data_dir: 数据目录
    cache_expiry_days: 缓存过期时间
    return: 结果字典, 结果文件路径
"""
def run_analysis(code_list:pd.DataFrame, indicator_type:str, config:ConfigParser, output_dir='./output', data_dir='./data/detect', cache_expiry_days=1):
    if code_list.empty:
        print(f'warning: {indicator_type} code_list is empty')
        return {}
    
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    
    results = {}
    name_list = code_list['name'].values

    look_ahead = config.get("CONFIG", "KD_LOOK_AHEAD", fallback=0)
    look_ahead = int(look_ahead) if look_ahead else 0
    ktype = config.get("CONFIG", "FUTU_PUSH_TYPE")
    timestamp = datetime.now().strftime('%Y%m%d')

    for idx, code in enumerate(code_list['code'].values):
        print(f"\n---- Analyzing {idx+1}/{len(code_list)} {name_list[idx]} {code} {ktype}----\n")
        
        # 检查本地数据文件
        data_file_name = f'data_{code.replace(".", "_")}_{ktype}.csv'
        data_file = os.path.join(data_dir, data_file_name)
        if not os.path.exists(data_file) or (datetime.now() - datetime.fromtimestamp(os.path.getmtime(data_file))).days > cache_expiry_days:
            # 文件不存在，下载数据
            print(f"下载新数据: {data_file}")
            df = get_kline_data(code, config, max_count=1300)
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
    return results, summary_file

if __name__ == '__main__':
    config = get_config()

    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    group = config.get("CONFIG", "FUTU_GROUP", fallback='')
    # True->检查PARAMS_DB中是否存在参数 如果存在则不进行训练
    params_check = config.getboolean("CONFIG", "PARAMS_CHECK", fallback=False)
    # True->将训练结果更新到PARAMS_DB中
    params_update = config.getboolean("CONFIG", "PARAMS_UPDATE", fallback=False)

    code_list = config.get("CONFIG", "FUTU_CODE_LIST", fallback='').split(',')
    code_list = [code for code in code_list if code.strip()]

    ktype = config.get("CONFIG", "FUTU_PUSH_TYPE")
    if ktype not in ['K_DAY', 'K_WEEK', 'K_60M', 'K_HALF_DAY']:
        raise ValueError(f"Invalid ktype: {ktype}")

    # 获取股票列表
    code_pd = pd.DataFrame(columns=pd.Index(['code','name']))
    if group:
        ls = code_in_futu_group(group,host,port)
        if isinstance(ls, pd.DataFrame):
            code_pd = pd.concat([code_pd, ls[['code','name']]])
    if len(code_list) > 0:
        ls = pd.DataFrame({'code': code_list, 'name': code_list})
        code_pd = pd.concat([code_pd, ls])

    if code_pd.empty:
        print('warning: no code in config')
        exit()

    timestamp = datetime.now().strftime('%Y%m%d')
    trend_types = config.get("CONFIG", "TREND_TYPE").split(',')
    # 确保code_pd是DataFrame类型
    assert isinstance(code_pd, pd.DataFrame), "code_pd must be a DataFrame"
    for trend_type in trend_types:
        pd_code_list = code_pd.copy()
        indicator_dict = {
            'reverse': 'KD',
            'continue': 'MACD',
            'topdown': 'RSI'
        }
        params_db_dict = {
            'reverse': config.get("CONFIG", "KD_PARAMS_DB", fallback=None),
            'continue': config.get("CONFIG", "MACD_PARAMS_DB", fallback=None),
            'topdown': config.get("CONFIG", "RSI_PARAMS_DB", fallback=None)
        }
        if trend_type not in indicator_dict:
            raise ValueError(f"Invalid trend type: {trend_type}")
        indicator_type = indicator_dict[trend_type]
        params_db = params_db_dict[trend_type]
        
        # 参数数据库 支持mongodb和sqlite 多个数据库用逗号分隔 PARAMS_CHECK以首个数据库为准 PARAMS_UPDATE为所有数据库更新 
        db_list = []
        if params_db is not None and len(params_db.split(',')) >= 1:
            db_list = params_db.split(',')
            params_db = db_list[0]
        
        if params_db is not None and params_check is True:
            db = ParamsDB(params_db)
            # 逐个code查询是否存在参数 如果存在则剔除pd_code_list中的该行
            for code in pd_code_list['code'].values:
                data = db.get_stock_params(code)
                if data is not None and data['best_params']:
                    pd_code_list = pd_code_list[pd_code_list['code'] != code]
                    print(f"code {code} already has parameters in {trend_type} database, skipping training")
        
        output_dir = f'./output/detect_{timestamp}_{ktype}_{sanitize_path_component(group)}_{indicator_type}'
        results, result_file = run_analysis(pd_code_list, indicator_type, config, output_dir=output_dir)

        if params_db is not None and params_update is True and len(db_list) >= 1:
            for db_path in db_list:
                db = ParamsDB(db_path, init_db=True)
                db.import_params(result_file)