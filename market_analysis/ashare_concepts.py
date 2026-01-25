import pandas as pd
import requests
import json
import time
import random
import os
from collections import Counter
from datetime import datetime
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import configparser
from notification_engine import NotificationEngine
from ft_config import get_config

# ================= 配置区域 =================
BLACKLIST_FILE = "./env/concept_blacklist.txt"
TOP_N = 30           # 分析竞价金额前多少名
MIN_SLEEP = 0.5     # 最小随机延时(秒)
MAX_SLEEP = 3       # 最大随机延时(秒)
OUTPUT_JSON_DIR = "./output"  # 输出JSON文件路径

# ================= 核心功能函数 =================

def load_blacklist(blacklist_file=None):
    """读取本地黑名单配置"""
    if blacklist_file is None:
        blacklist_file = BLACKLIST_FILE
    
    blacklist = set()
    if os.path.exists(blacklist_file):
        with open(blacklist_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                # 忽略空行和注释
                if line and not line.startswith("#"):
                    blacklist.add(line)
        print(f"[*] 已加载黑名单词汇: {len(blacklist)} 个")
    else:
        print(f"[!] 未找到 {blacklist_file}，使用默认空黑名单")
    return blacklist

def get_realtime_bidding_list(top_n=TOP_N):
    """获取9:25实时竞价排名 (使用东财直连API)"""
    print(f"[*] [{datetime.now().time()}] 正在请求东财直连 API...")
    
    url = "https://43.push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": top_n, "po": 1, "np": 1, 
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2, "invt": 2, "fid": "f6",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f6,f3"
    }
    
    try:
        res = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=3)
        data = res.json()
        stock_list = data['data']['diff']
        
        result = []
        for item in stock_list:
            # 过滤掉无成交额的（停牌等）
            if item['f6'] == '-': 
                continue
            
            result.append({
                "code": item['f12'],
                "name": item['f14'],
                "amount": round(float(item['f6']) / 100000000, 2),
                "pct": item['f3']
            })
        print(f"[*] 成功获取 Top {len(result)} 数据")
        return result
    except Exception as e:
        print(f"[!] API 请求失败: {e}")
        return []

def get_stock_concepts_eastmoney(stock_code, blacklist_set):
    """抓取单只股票的东财F10核心题材"""
    # 模拟人类随机延时
    time.sleep(random.uniform(MIN_SLEEP, MAX_SLEEP))
    
    # 格式转换
    if stock_code.startswith('6'):
        symbol = f"{stock_code}.SH"
    elif stock_code.startswith(('0', '3')):
        symbol = f"{stock_code}.SZ"
    else:
        return []

    url = "https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax"
    params = {"code": symbol}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://emweb.securities.eastmoney.com/"
    }

    try:
        res = requests.get(url, params=params, headers=headers, timeout=3)
        if res.status_code != 200:
            return []
        
        data = res.json()
        clean_concepts = []
        
        # 解析 'ssbk' 字段
        if 'ssbk' in data and data['ssbk']:
            for item in data['ssbk']:
                c_name = item.get('BOARD_NAME', '')
                # 过滤黑名单
                if c_name and c_name not in blacklist_set:
                    clean_concepts.append(c_name)
        
        return clean_concepts
    except Exception as e:
        # print(f"抓取失败 {stock_code}: {e}") # 调试时可开启
        return []

def analyze_ashare_concepts(blacklist_file=None, top_n=TOP_N, output_json_dir=None):
    """
    分析A股竞价主线概念
    
    参数:
        blacklist_file: 黑名单文件路径，默认使用配置中的路径
        top_n: 分析竞价金额前多少名，默认30
        output_json_file: 输出JSON文件路径，默认使用配置中的路径
    
    返回:
        pd.DataFrame: 包含股票代码、名称、金额、涨幅、概念列表的DataFrame
    """
    # 1. 加载黑名单
    blacklist = load_blacklist(blacklist_file)
    
    # 2. 获取竞价排名
    top_stocks = get_realtime_bidding_list(top_n)
    if not top_stocks:
        print("[!] 无数据，返回空DataFrame")
        return pd.DataFrame(columns=['code', 'name', 'amount', 'pct', 'concepts'])
    
    print(f"[*] 开始顺序抓取 Top {len(top_stocks)} 个股概念...")
    start_time = time.time()
    
    # 3. 顺序抓取概念
    results = []
    for idx, stock_info in enumerate(top_stocks, 1):
        print(f"[*] 正在处理 {idx}/{len(top_stocks)}: {stock_info['code']} {stock_info['name']}")
        concepts = get_stock_concepts_eastmoney(stock_info['code'], blacklist)
        stock_info['concepts'] = concepts  # 保留所有过滤后的概念
        results.append(stock_info)
    
    cost_time = time.time() - start_time
    print(f"[*] 概念抓取完成，耗时: {cost_time:.2f} 秒")
    
    # 4. 构建DataFrame
    df = pd.DataFrame(results)
    
    # 5. 统计热点概念
    all_concepts = []
    for stock in results:
        all_concepts.extend(stock['concepts'])
    
    concept_counter = Counter(all_concepts)
    hot_concepts = concept_counter.most_common(10)
    
    # 6. 保存JSON文件
    if output_json_dir is None:
        output_json_dir = OUTPUT_JSON_DIR
    if not os.path.exists(output_json_dir):
        os.makedirs(output_json_dir)
    output_json_file = os.path.join(output_json_dir, f"stock_concepts_db_{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
    
    output_data = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'time': datetime.now().strftime('%H:%M:%S'),
        'stocks': results,
        'hot_concepts': [{'concept': concept, 'count': count} for concept, count in hot_concepts]
    }
    
    with open(output_json_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print(f"[*] 结果已保存到: {output_json_file}")
    
    return df
    
if __name__ == "__main__":
    config = get_config()
    blacklist_file = config.get("CONFIG", "CONCEPT_BLACKLIST_FILE", fallback=BLACKLIST_FILE)
    
    now = datetime.now().strftime('%Y%m%d-%H:%M:%S')

    # 调用工具函数
    df = analyze_ashare_concepts(blacklist_file=blacklist_file)
    
    if df.empty:
        print("[!] 无数据，程序退出")
    
    # 输出报告
    msg = ''
    msg += "【金额排名详情】 {}".format(now)
    msg += "\n"
    msg += "代码|名称|金额(亿)|涨幅%|概念"
    msg += "\n"
    for _, row in df.iterrows():
        top3_concepts = row['concepts'][:3]
        msg += f"{row['code']}|{row['name']}|{row['amount']}|{row['pct']}|{','.join(top3_concepts)}"
        msg += "\n"
    print(msg)
    
    # 发送报告
    notification_engine = NotificationEngine(config)
    notification_engine.send_email("【金额排名详情】{}".format(now), msg)
    notification_engine.send_telegram_message(msg)