# backend/api.py - FastAPI 后端服务（单文件实现）
"""
Futu Trends API 后端服务
提供股票列表、K线数据和技术指标计算接口
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
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

DEFAULT_MAX_COUNT = 1000
DEFAULT_EMA_PERIOD = 240

# ---- 共享数据函数 ----

def _get_db_paths() -> Dict[str, Optional[str]]:
    """获取各指标的 ParamsDB 路径"""
    return {
        'MACD': config.get("CONFIG", "MACD_PARAMS_DB", fallback=None),
        'KD': config.get("CONFIG", "KD_PARAMS_DB", fallback=None),
        'RSI': config.get("CONFIG", "RSI_PARAMS_DB", fallback=None),
    }

def _fetch_kline(code: str, max_count: int = DEFAULT_MAX_COUNT) -> pd.DataFrame:
    """获取 K 线数据，返回带 time 列的 DataFrame。无数据时抛 HTTPException"""
    df = get_kline_data(code, config, max_count)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"K-line data not found for stock {code}")
    df['time'] = df.index.astype(str)
    return df

def _read_detect(code: str, db_paths: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """从 ParamsDB 读取各指标的 best_params / meta_info / performance"""
    result = {}
    for indicator_type, db_path in db_paths.items():
        if not db_path:
            continue
        try:
            db = ParamsDB(db_path.split(',')[0])
            data = db.get_stock_params(code)
            if data and data.get('best_params'):
                result[indicator_type] = {
                    'best_params': data['best_params'],
                    'meta_info': data['meta_info'],
                    'performance': data['performance'],
                }
        except Exception as e:
            logger.warning(f"Failed to get {indicator_type} params for {code}: {e}")
    return result

INDICATOR_CLASSES = {'MACD': MACD, 'KD': KD, 'RSI': RSI}
INDICATOR_DEFAULTS = {
    'MACD': (0, 0),
    'KD': (20, 80),
    'RSI': (30, 70),
}

def _calculate_indicator(indicator_type: str, df: pd.DataFrame, best_params: dict) -> Optional[Dict[str, Any]]:
    """根据类型和参数计算单个指标，返回前端所需格式"""
    indicator = INDICATOR_CLASSES[indicator_type]()
    default_oversold, default_overbought = INDICATOR_DEFAULTS[indicator_type]

    if indicator_type == 'MACD':
        vmacd, signal = indicator.indicator_calculate(df.copy(), best_params)
        return {
            'vmacd': vmacd.fillna(0).tolist(),
            'signal': signal.fillna(0).tolist(),
            'hist': (2*(vmacd - signal)).fillna(0).tolist(),
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
    return None

# ---- API 路由 ----

@app.get("/api/stocks/list")
async def get_stock_list():
    """获取股票列表"""
    try:
        stocks = []

        groups_str = config.get("CONFIG", "FUTU_GROUP", fallback='')
        host = config.get("CONFIG", "FUTU_HOST", fallback='127.0.0.1')
        port = config.getint("CONFIG", "FUTU_PORT", fallback=11111)

        if groups_str and host and port:
            seen_codes = set()
            for group in groups_str.split(','):
                group = group.strip()
                if not group:
                    continue
                try:
                    df = code_in_futu_group(group, host, port)
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        for rec in df[['code', 'name']].to_dict('records'):
                            if rec['code'] not in seen_codes:
                                seen_codes.add(rec['code'])
                                stocks.append(rec)
                        logger.info(f"Retrieved stocks from Futu group '{group}', total unique: {len(stocks)}")
                except Exception as e:
                    logger.warning(f"Failed to get stock list from Futu group '{group}': {e}")

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
    """获取 K 线数据"""
    try:
        df = _fetch_kline(code, max_count)
        result = df[['time', 'open', 'high', 'low', 'close', 'volume']].to_dict('records')
        logger.info(f"Returning K-line data for {code}, {len(result)} records")
        return JSONResponse(content={'data': result})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting K-line data: {code}, {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/indicators/{code}")
async def get_indicators(code: str):
    """
    聚合接口：K 线 + 技术指标 + detect 结果

    Returns: { time, ema, macd?, kd?, rsi?, kline, detect? }
    """
    try:
        df = _fetch_kline(code)
        db_paths = _get_db_paths()

        # EMA
        ema_period = config.getint("CONFIG", "EMA_PERIOD", fallback=DEFAULT_EMA_PERIOD)
        df[f'EMA_{ema_period}'] = EMA(df['close'], ema_period)

        result: Dict[str, Any] = {
            'time': df['time'].tolist(),
            'ema': df[f'EMA_{ema_period}'].fillna(0).tolist(),
            'kline': df[['time', 'open', 'high', 'low', 'close', 'volume']].to_dict('records'),
        }

        # 读取参数并计算各指标
        for indicator_type, db_path in db_paths.items():
            if not db_path:
                continue
            try:
                db = ParamsDB(db_path)
                params = db.get_stock_params(code)
                if not params or not params.get('best_params'):
                    logger.warning(f"{indicator_type} parameters not found: {code}")
                    continue
                ind_result = _calculate_indicator(indicator_type, df, params['best_params'])
                if ind_result:
                    result[indicator_type.lower()] = ind_result
            except Exception as e:
                logger.error(f"Error calculating {indicator_type}: {e}", exc_info=True)

        # detect 结果
        detect = _read_detect(code, db_paths)
        if detect:
            result['detect'] = {'code': code, 'indicators': detect}

        logger.info(f"Returning indicator data for {code}")
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting indicators: {code}, {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/detect/{code}")
async def get_detect_result(code: str):
    """获取标的的 detect 结果（各指标的 best_params + performance）"""
    detect = _read_detect(code, _get_db_paths())
    if not detect:
        raise HTTPException(status_code=404, detail=f"No detect results found for {code}")
    return JSONResponse(content={'code': code, 'indicators': detect})

# ---- 页面路由 ----

@app.get("/detect/{code:path}")
@app.get("/detect")
async def detect_page(code: str = ""):
    html_path = Path(__file__).parent / "detect.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="detect.html not found")
    return HTMLResponse(content=html_path.read_text(encoding='utf-8'))

@app.get("/stocks")
async def stocks_page():
    html_path = Path(__file__).parent / "stocks.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="stocks.html not found")
    return HTMLResponse(content=html_path.read_text(encoding='utf-8'))

@app.get("/")
async def root():
    return {"status": "ok", "message": "Futu Trends API is running"}

# ---- 启动 ----

def find_available_port(start_port: int = 8001, max_attempts: int = 100) -> int:
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

    default_port = args.port
    try:
        actual_port = find_available_port(default_port)
        if actual_port != default_port:
            logger.warning(f"端口 {default_port} 被占用，自动切换到端口 {actual_port}")
        else:
            logger.info(f"使用端口 {actual_port}")

        print(f"API_PORT={actual_port}", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=actual_port)
    except Exception as e:
        logger.error(f"启动服务失败: {e}", exc_info=True)
        raise
