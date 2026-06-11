import logging
import pandas as pd
import requests
import json
import time
import random
import os
import shutil
import subprocess
import contextlib
import io
from collections import Counter
from datetime import datetime
from urllib.parse import urlencode
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import configparser
from futu_group import sync_futu_group
from notification_engine import NotificationEngine
from ft_config import get_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ================= 配置区域 =================
BLACKLIST_FILE = "./env/concept_blacklist.txt"
TOP_N = 20           # 分析竞价金额前多少名
MIN_BIDDING_AMOUNT = 1.0  # 最低竞价成交额(亿)
MIN_SLEEP = 0.5     # 最小随机延时(秒)
MAX_SLEEP = 3       # 最大随机延时(秒)
OUTPUT_JSON_DIR = "./output/concepts"  # 输出JSON文件路径
CONCEPT_BOARD_TOP_N = 20  # 输出东财概念板块排名前多少名
EASTMONEY_TIMEOUT = 5
EASTMONEY_HEADERS = {"User-Agent": "Mozilla/5.0"}
EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
EASTMONEY_STOCK_URLS = [
    "https://43.push2.eastmoney.com/api/qt/clist/get",
    "https://82.push2.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
]
EASTMONEY_CONCEPT_URLS = [
    "https://79.push2.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
]

# ================= 核心功能函数 =================

def to_float(value):
    """安全转换行情字段为 float，兼容 '-'、None、NaN。"""
    if value is None or pd.isna(value) or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def format_concept_ranking_line(item):
    """格式化题材排名消息，兼容东财涨幅榜和 F10 统计兜底榜。"""
    pct = item.get("pct")
    if pct is None:
        return f"{item['rank']}|{item['concept']}|出现:{item.get('up_count') or 0}次"
    return f"{item['rank']}|{item['concept']}|{pct}%|领涨:{item.get('leader', '')} {item.get('leader_pct')}%"

def request_eastmoney_clist(urls, params):
    """请求东财 clist 小分页接口；优先直连，失败后再尝试环境代理。"""
    errors = []

    for trust_env in (False, True):
        session = requests.Session()
        session.trust_env = trust_env
        for url in urls:
            try:
                response = session.get(
                    url,
                    params=params,
                    headers=EASTMONEY_HEADERS,
                    timeout=EASTMONEY_TIMEOUT,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("rc") == 0 and data.get("data"):
                    return data
                errors.append(f"{url} rc={data.get('rc')} data={data.get('data')}")
            except Exception as e:
                errors.append(f"{url} trust_env={trust_env}: {e}")

    curl_path = shutil.which("curl")
    if curl_path:
        for url in urls:
            try:
                query = urlencode(params, safe=":+,!")
                request_url = f"{url}?{query}"
                completed = subprocess.run(
                    [
                        curl_path,
                        "--noproxy",
                        "*",
                        "-A",
                        EASTMONEY_HEADERS["User-Agent"],
                        "-sS",
                        "--max-time",
                        str(EASTMONEY_TIMEOUT),
                        request_url,
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                data = json.loads(completed.stdout)
                if data.get("rc") == 0 and data.get("data"):
                    return data
                errors.append(f"curl {url} rc={data.get('rc')} data={data.get('data')}")
            except Exception as e:
                errors.append(f"curl {url}: {e}")

    raise RuntimeError("; ".join(errors[-4:]))

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

def get_realtime_bidding_list(top_n=TOP_N, min_amount=MIN_BIDDING_AMOUNT):
    """获取A股全市场实时成交额排名 (使用东财直连小分页 API)"""
    print(f"[*] [{datetime.now().time()}] 正在请求东财成交额小分页 API...")
    
    try:
        params = {
            "pn": 1,
            "pz": top_n,
            "po": 1,
            "np": 1,
            "ut": EASTMONEY_UT,
            "fltt": 2,
            "invt": 2,
            "fid": "f6",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f6,f3",
        }
        data = request_eastmoney_clist(EASTMONEY_STOCK_URLS, params)
        stock_list = data["data"].get("diff") or []

        result = []
        for item in stock_list:
            amount_raw = to_float(item.get("f6"))
            if amount_raw is None:
                continue
            amount = amount_raw / 100000000
            if amount <= min_amount:
                continue

            pct = to_float(item.get("f3"))
            result.append({
                "code": str(item.get("f12", "")).strip().zfill(6),
                "name": item.get("f14", ""),
                "amount": round(amount, 2),
                "pct": round(pct, 2) if pct is not None else None
            })
        print(f"[*] 成功获取成交额 Top {len(result)} 数据")
        return result
    except Exception as e:
        print(f"[!] 东财成交额小分页 API 请求失败: {e}")
        return get_realtime_bidding_list_sina(top_n, min_amount)

def get_realtime_bidding_list_sina(top_n=TOP_N, min_amount=MIN_BIDDING_AMOUNT):
    """获取A股全市场实时成交额排名 (新浪兜底源，较慢，避免高频调用)"""
    print(f"[*] [{datetime.now().time()}] 正在请求新浪 A 股实时行情兜底源...")

    try:
        import akshare as ak

        # AkShare 的新浪接口会打印 tqdm 进度条；脚本通知场景下静音即可。
        with contextlib.redirect_stderr(io.StringIO()):
            df = ak.stock_zh_a_spot()

        required_columns = {"代码", "名称", "成交额", "涨跌幅"}
        missing_columns = required_columns - set(df.columns)
        if missing_columns:
            print(f"[!] 新浪实时行情缺少字段: {sorted(missing_columns)}")
            return []

        df = df.copy()
        df["成交额"] = pd.to_numeric(df["成交额"], errors="coerce")
        df["涨跌幅"] = pd.to_numeric(df["涨跌幅"], errors="coerce")
        df = df.dropna(subset=["成交额"])
        df = df[df["成交额"] > min_amount * 100000000]
        df = df.sort_values("成交额", ascending=False)

        result = []
        for _, item in df.iterrows():
            raw_code = str(item["代码"]).strip().lower()
            if raw_code.startswith(("sh", "sz")):
                code = raw_code[2:]
            else:
                continue
            if not code.startswith(("6", "0", "3")):
                continue

            pct = to_float(item["涨跌幅"])
            result.append({
                "code": code.zfill(6),
                "name": item["名称"],
                "amount": round(float(item["成交额"]) / 100000000, 2),
                "pct": round(pct, 2) if pct is not None else None
            })
            if len(result) >= top_n:
                break

        print(f"[*] 新浪兜底源成功获取成交额 Top {len(result)} 数据")
        return result
    except Exception as e:
        print(f"[!] 新浪 A 股实时行情兜底源请求失败: {e}")
        return []

def get_concept_board_ranking(top_n=CONCEPT_BOARD_TOP_N, blacklist_set=None):
    """获取东财概念板块涨幅排名 (使用东财直连小分页 API)"""
    print(f"[*] [{datetime.now().time()}] 正在请求东财概念板块小分页 API...")

    try:
        params = {
            "pn": 1,
            "pz": top_n,
            "po": 1,
            "np": 1,
            "ut": EASTMONEY_UT,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:90+t:3+f:!50",
            "fields": "f2,f3,f4,f8,f12,f14,f20,f104,f105,f128,f136",
        }
        data = request_eastmoney_clist(EASTMONEY_CONCEPT_URLS, params)
        concept_list = data["data"].get("diff") or []

        rankings = []
        for rank, item in enumerate(concept_list, 1):
            concept = item.get("f14", "")
            if blacklist_set and concept in blacklist_set:
                continue

            pct = to_float(item.get("f3"))
            turnover = to_float(item.get("f8"))
            leader_pct = to_float(item.get("f136"))
            rankings.append({
                "rank": rank,
                "concept": concept,
                "code": item.get("f12", ""),
                "pct": round(pct, 2) if pct is not None else None,
                "turnover": round(turnover, 2) if turnover is not None else None,
                "up_count": int(item["f104"]) if to_float(item.get("f104")) is not None else None,
                "down_count": int(item["f105"]) if to_float(item.get("f105")) is not None else None,
                "leader": item.get("f128", ""),
                "leader_pct": round(leader_pct, 2) if leader_pct is not None else None,
            })

        print(f"[*] 成功获取概念板块 Top {len(rankings)} 数据")
        return rankings
    except Exception as e:
        print(f"[!] 东财概念板块小分页 API 请求失败: {e}")
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

def ashare_code_to_futu_code(stock_code):
    """转换A股代码为Futu代码格式"""
    if stock_code is None:
        return None
    stock_code = str(stock_code).strip()
    if not stock_code.isdigit():
        return None
    stock_code = stock_code.zfill(6)
    if len(stock_code) != 6:
        return None
    if stock_code.startswith('6'):
        return f"SH.{stock_code}"
    if stock_code.startswith(('0', '3')):
        return f"SZ.{stock_code}"
    return None

def select_futu_group(config, now=None):
    """
    从FUTU_GROUP选择A股概念同步分组。
    配置一个分组时直接使用；配置多个分组时，上午使用第一个，下午使用第二个。
    """
    group = config.get("CONFIG", "FUTU_GROUP", fallback="")
    groups = [g.strip() for g in group.split(',') if g.strip()]
    if not groups:
        return ""
    if len(groups) == 1:
        return groups[0]

    now = now or datetime.now()
    return groups[0] if now.hour < 12 else groups[1]

def sync_ashare_concepts_to_futu_group(df, config):
    """将本次获得的A股标的同步到FUTU_GROUP目标分组"""
    if df.empty:
        print("[!] 无竞价数据，跳过同步futu group")
        return

    group_name = select_futu_group(config)
    if not group_name:
        print("[!] 未配置FUTU_GROUP，跳过同步futu group")
        return

    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    codes = [ashare_code_to_futu_code(code) for code in df['code'].tolist()]
    codes = [code for code in codes if code]
    if not codes:
        print("[!] 无可同步的A股futu代码，跳过同步futu group")
        return

    print(f"[*] 同步 {len(codes)} 个标的到 futu group: {group_name}")
    sync_futu_group(group_name, codes, host=host, port=port, overwrite=True)

def analyze_ashare_concepts(blacklist_file=None, top_n=TOP_N, min_amount=MIN_BIDDING_AMOUNT, output_json_dir=None):
    """
    分析A股竞价主线概念
    
    参数:
        blacklist_file: 黑名单文件路径，默认使用配置中的路径
        top_n: 分析竞价金额前多少名，默认30
        min_amount: 最低竞价成交额(亿)，默认1亿
        output_json_file: 输出JSON文件路径，默认使用配置中的路径
    
    返回:
        pd.DataFrame: 包含股票代码、名称、金额、涨幅、概念列表的DataFrame
    """
    # 1. 加载黑名单
    blacklist = load_blacklist(blacklist_file)

    # 2. 获取东财概念板块实时排名
    concept_board_rankings = get_concept_board_ranking(blacklist_set=blacklist)
    
    # 3. 获取成交额排名
    top_stocks = get_realtime_bidding_list(top_n, min_amount)
    if not top_stocks:
        print("[!] 无数据，返回空DataFrame")
        df = pd.DataFrame(columns=['code', 'name', 'amount', 'pct', 'concepts'])
        df.attrs["concept_board_rankings"] = concept_board_rankings
        return df
    
    print(f"[*] 开始顺序抓取 Top {len(top_stocks)} 个股概念...")
    start_time = time.time()
    
    # 4. 顺序抓取概念
    results = []
    for idx, stock_info in enumerate(top_stocks, 1):
        print(f"[*] 正在处理 {idx}/{len(top_stocks)}: {stock_info['code']} {stock_info['name']}")
        concepts = get_stock_concepts_eastmoney(stock_info['code'], blacklist)
        stock_info['concepts'] = concepts  # 保留所有过滤后的概念
        results.append(stock_info)
    
    cost_time = time.time() - start_time
    print(f"[*] 概念抓取完成，耗时: {cost_time:.2f} 秒")
    
    # 5. 构建DataFrame
    df = pd.DataFrame(results)
    df.attrs["concept_board_rankings"] = concept_board_rankings
    
    # 6. 统计热点概念
    all_concepts = []
    for stock in results:
        all_concepts.extend(stock['concepts'])
    
    concept_counter = Counter(all_concepts)
    hot_concepts = concept_counter.most_common(10)
    if not concept_board_rankings:
        concept_board_rankings = [
            {
                "rank": idx,
                "concept": concept,
                "code": "",
                "pct": None,
                "turnover": None,
                "up_count": count,
                "down_count": None,
                "leader": "",
                "leader_pct": None,
            }
            for idx, (concept, count) in enumerate(hot_concepts, 1)
        ]
        df.attrs["concept_board_rankings"] = concept_board_rankings
    
    # 7. 保存JSON文件
    if output_json_dir is None:
        output_json_dir = OUTPUT_JSON_DIR
    if not os.path.exists(output_json_dir):
        os.makedirs(output_json_dir)
    output_json_file = os.path.join(output_json_dir, f"stock_concepts_db_{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
    
    output_data = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'time': datetime.now().strftime('%H:%M:%S'),
        'stocks': results,
        'concept_board_rankings': concept_board_rankings,
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
    msg += "【题材排名】"
    msg += "\n"
    concept_rankings = df.attrs.get("concept_board_rankings", [])
    for item in concept_rankings[:10]:
        msg += format_concept_ranking_line(item)
        msg += "\n"
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
    sync_ashare_concepts_to_futu_group(df, config)
    notification_engine.send_email("【金额排名详情】{}".format(now), msg)
    notification_engine.send_telegram_message(msg)
    notification_engine.send_webhook(msg)
