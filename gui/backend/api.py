# backend/api.py - FastAPI 后端服务（单文件实现）
"""
Futu Trends API 后端服务
提供股票列表、K线数据和技术指标计算接口
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import sys
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import pandas as pd

# 添加项目根目录到路径（复用现有代码）
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from ft_config import get_config
from data import get_kline_data
from signal_analysis import KD, MACD, RSI
from tools import EMA, code_in_futu_group
from params_db import ParamsDB

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Futu Trends API", version="1.0.0")

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

config = get_config()

# 常量
DEFAULT_MAX_COUNT = 1000
DEFAULT_EMA_PERIOD = 240

@app.get("/api/stocks/list")
async def get_stock_list():
    """
    获取股票列表
    
    Returns:
        JSONResponse: 包含股票列表的响应
        {
            "stocks": [
                {"code": "SH.510300", "name": "沪深300ETF"},
                ...
            ]
        }
    """
    try:
        stocks = []
        
        # 从富途分组获取
        group = config.get("CONFIG", "FUTU_GROUP", fallback='')
        host = config.get("CONFIG", "FUTU_HOST", fallback='127.0.0.1')
        port = config.getint("CONFIG", "FUTU_PORT", fallback=11111)
        
        if group and host and port:
            try:
                df = code_in_futu_group(group, host, port)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    stocks.extend(df[['code', 'name']].to_dict('records'))
                    logger.info(f"Retrieved {len(stocks)} stocks from Futu group")
            except Exception as e:
                logger.warning(f"Failed to get stock list from Futu group: {e}")
        
        # 从配置获取
        code_list = config.get("CONFIG", "FUTU_CODE_LIST", fallback='').split(',')
        for code in code_list:
            if code.strip():
                stocks.append({'code': code.strip(), 'name': code.strip()})
        
        logger.info(f"Stock list loaded successfully, total {len(stocks)} stocks")
        return JSONResponse(content={'stocks': stocks})
    except Exception as e:
        logger.error(f"Error getting stock list: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/kline/{code}")
async def get_kline(code: str, max_count: int = DEFAULT_MAX_COUNT):
    """
    获取K线数据
    
    Args:
        code: 股票代码
        max_count: 最大K线数量，默认1000
    
    Returns:
        JSONResponse: 包含K线数据的响应
        {
            "data": [
                {
                    "time": "2024-01-01",
                    "open": 1.0,
                    "high": 1.1,
                    "low": 0.9,
                    "close": 1.05,
                    "volume": 1000000
                },
                ...
            ]
        }
    """
    try:
        df = get_kline_data(code, config, max_count)
        if df is None or df.empty:
            logger.warning(f"K-line data not found: {code}")
            raise HTTPException(status_code=404, detail=f"K-line data not found for stock {code}")
        
        # 转换为前端需要的格式
        df['time'] = df.index.astype(str)
        result = df[['time', 'open', 'high', 'low', 'close', 'volume']].to_dict('records')
        logger.info(f"Returning K-line data for {code}, total {len(result)} records")
        return JSONResponse(content={'data': result})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting K-line data: {code}, {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/indicators/{code}")
async def get_indicators(code: str):
    """
    获取所有技术指标
    
    Args:
        code: 股票代码
    
    Returns:
        JSONResponse: 包含技术指标的响应
        {
            "time": ["2024-01-01", ...],
            "ema": [1.0, ...],
            "macd": {
                "vmacd": [0.1, ...],
                "signal": [0.05, ...],
                "hist": [0.05, ...]
            },
            "kd": {...},
            "rsi": {...}
        }
    """
    try:
        # 获取K线数据
        df = get_kline_data(code, config, max_count=DEFAULT_MAX_COUNT)
        if df is None or df.empty:
            logger.warning(f"K-line data not found: {code}")
            raise HTTPException(status_code=404, detail=f"K-line data not found for stock {code}")
        
        df['time'] = df.index.astype(str)
        
        # 计算 EMA
        ema_period = config.getint("CONFIG", "EMA_PERIOD", fallback=DEFAULT_EMA_PERIOD)
        df[f'EMA_{ema_period}'] = EMA(df['close'], ema_period)
        
        result: Dict[str, Any] = {
            'time': df['time'].tolist(),
            'ema': df[f'EMA_{ema_period}'].fillna(0).tolist(),
        }
        
        # 读取参数数据库路径
        db_paths = {
            'MACD': config.get("CONFIG", "MACD_PARAMS_DB", fallback=None),
            'KD': config.get("CONFIG", "KD_PARAMS_DB", fallback=None),
            'RSI': config.get("CONFIG", "RSI_PARAMS_DB", fallback=None),
        }
        
        # 计算技术指标（提取重复逻辑）
        def calculate_indicator(
            indicator_type: str,
            db_path: Optional[str],
            indicator_class,
            default_oversold: int,
            default_overbought: int
        ) -> Optional[Dict[str, Any]]:
            """计算技术指标的通用函数"""
            if not db_path:
                return None
            
            try:
                db = ParamsDB(db_path)
                params = db.get_stock_params(code)
                
                if not params or not params.get('best_params'):
                    logger.warning(f"{indicator_type} parameters not found: {code}")
                    return None
                
                indicator = indicator_class()
                best_params = params['best_params']
                
                if indicator_type == 'MACD':
                    vmacd, signal = indicator.indicator_calculate(df.copy(), best_params)
                    return {
                        'vmacd': vmacd.fillna(0).tolist(),
                        'signal': signal.fillna(0).tolist(),
                        'hist': (vmacd - signal).fillna(0).tolist(),
                    }
                elif indicator_type == 'KD':
                    k, d = indicator.indicator_calculate(df.copy(), best_params)
                    return {
                        'k': k.fillna(0).tolist(),
                        'd': d.fillna(0).tolist(),
                        'oversold': best_params.get('oversold', default_oversold),
                        'overbought': best_params.get('overbought', default_overbought),
                    }
                elif indicator_type == 'RSI':
                    values = indicator.indicator_calculate(df.copy(), best_params)
                    return {
                        'values': values.fillna(0).tolist(),
                        'oversold': best_params.get('oversold', default_oversold),
                        'overbought': best_params.get('overbought', default_overbought),
                    }
            except Exception as e:
                logger.error(f"Error calculating {indicator_type} indicator: {e}", exc_info=True)
                return None
        
        # 计算各指标
        macd_result = calculate_indicator('MACD', db_paths['MACD'], MACD, 0, 0)
        if macd_result:
            result['macd'] = macd_result
        
        kd_result = calculate_indicator('KD', db_paths['KD'], KD, 20, 80)
        if kd_result:
            result['kd'] = kd_result
        
        rsi_result = calculate_indicator('RSI', db_paths['RSI'], RSI, 30, 70)
        if rsi_result:
            result['rsi'] = rsi_result
        
        logger.info(f"Returning indicator data for {code}")
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting indicators: {code}, {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    """健康检查"""
    return {"status": "ok", "message": "Futu Trends API is running"}

def find_available_port(start_port: int = 8001, max_attempts: int = 100) -> int:
    """
    查找可用端口
    
    Args:
        start_port: 起始端口号
        max_attempts: 最大尝试次数
    
    Returns:
        可用端口号
    """
    import socket
    
    for port in range(start_port, start_port + max_attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            continue
    
    raise RuntimeError(f"无法找到可用端口，已尝试 {max_attempts} 个端口")

if __name__ == "__main__":
    import uvicorn
    import argparse
    
    parser = argparse.ArgumentParser(description='Futu Trends API Server')
    parser.add_argument('--config', type=str, help='配置文件路径')
    parser.add_argument('--port', type=int, default=8001, help='服务端口（默认8001，如果被占用会自动切换）')
    args = parser.parse_args()
    
    # 尝试使用指定端口，如果被占用则自动切换
    default_port = args.port
    try:
        actual_port = find_available_port(default_port)
        if actual_port != default_port:
            logger.warning(f"端口 {default_port} 被占用，自动切换到端口 {actual_port}")
        else:
            logger.info(f"使用端口 {actual_port}")
        
        # 输出端口信息到标准输出（供 Electron 读取）
        print(f"API_PORT={actual_port}", flush=True)
        
        uvicorn.run(app, host="127.0.0.1", port=actual_port)
    except Exception as e:
        logger.error(f"启动服务失败: {e}", exc_info=True)
        raise

