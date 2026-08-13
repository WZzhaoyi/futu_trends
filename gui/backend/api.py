# backend/api.py - FastAPI 后端服务（单文件实现）
"""
Futu Trends API 后端服务
提供股票列表、K线数据和技术指标计算接口
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import json
import re
import sys
import logging
import configparser
from pathlib import Path
from typing import Dict, Any, Optional
import pandas as pd

# 添加项目根目录到路径（复用现有代码）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT))

from data import get_kline_data, opend_alive
from tools import EMA, code_in_futu_group
from params_db import ParamsDB
# 选股结果的路径/格式协议与生产端共用单一来源
from fundamental_analysis.futu_fundamental_screener import (
    OUT_ROOT as SCREENER_OUT_ROOT,
    list_results as screener_list_results,
    resolve_date as screener_resolve_date,
    result_path as screener_result_path,
)
# 指标/detect 计算口径与 CLI 共用单一来源（项目根的共享服务，无 FastAPI 依赖）
from indicator_service import (
    get_db_paths as _get_db_paths_svc,
    read_detect as _read_detect_svc,
    calculate_indicator as _calculate_indicator_svc,
)
from gui.backend.futu_quote import (
    FUTU_FAVICON_URL,
    create_futu_launch_page,
    validate_futu_code,
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """未加载 config 即拒绝启动（替代已废弃的 on_event("startup")）。"""
    if config is None:
        raise RuntimeError("启动失败：config 未加载，请用 `python api.py --config <path>` 启动。")
    yield


app = FastAPI(title="Futu Trends API", version="1.0.0", lifespan=_lifespan)

# CORS 配置。服务只绑 127.0.0.1，外部流量经反向代理接入；
# 宽松 origin 即可，但不能与 allow_credentials 共存（浏览器会拒绝该组合）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 共享静态资源（theme.css / theme.js）
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# 启动时显式加载校验后赋值（见 __main__）；导入期为 None，不读 config/argv。
config: configparser.ConfigParser | None = None

SCREENER_ROOT = PROJECT_ROOT / SCREENER_OUT_ROOT

# 必需键 + 须为整数的键
_REQUIRED_KEYS = ("FUTU_HOST", "FUTU_PORT", "DATA_SOURCE")
_INT_KEYS = ("FUTU_PORT", "EMA_PERIOD")


def load_and_validate_config(path: str | None) -> configparser.ConfigParser:
    """加载并校验配置；任一不满足即报错退出。"""
    if not path:
        raise SystemExit("启动失败：必须通过 --config 指定配置文件")
    cfg = configparser.ConfigParser()
    if not cfg.read(path, encoding="utf-8"):
        raise SystemExit(f"启动失败：配置文件不存在或不可读: {path}")
    if not cfg.has_section("CONFIG"):
        raise SystemExit(f"启动失败：配置缺少 [CONFIG] 段: {path}")
    missing = [k for k in _REQUIRED_KEYS if not cfg.get("CONFIG", k, fallback="").strip()]
    if missing:
        raise SystemExit(f"启动失败：[CONFIG] 缺少必需键: {', '.join(missing)}")
    for k in _INT_KEYS:
        v = cfg.get("CONFIG", k, fallback="").strip()
        if v:
            try:
                int(v)
            except ValueError:
                raise SystemExit(f"启动失败：[CONFIG] {k} 必须为整数，当前为 {v!r}")
    return cfg


DEFAULT_MAX_COUNT = 1000
DEFAULT_EMA_PERIOD = 240

# ---- 共享数据函数 ----

# 指标/detect 计算口径统一收敛到根级 indicator_service；以下为薄封装，行为不变。

def _get_db_paths() -> Dict[str, Optional[str]]:
    """获取各指标的 ParamsDB 路径"""
    return _get_db_paths_svc(config)

def _fetch_kline(code: str, max_count: int = DEFAULT_MAX_COUNT) -> pd.DataFrame:
    """获取 K 线数据，返回带 time 列的 DataFrame。无数据时抛 HTTPException"""
    df = get_kline_data(code, config, max_count)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"K-line data not found for stock {code}")
    df['time'] = df.index.astype(str)
    return df

def _read_detect(code: str, db_paths: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """从 ParamsDB 读取各指标的 best_params / meta_info / performance"""
    return _read_detect_svc(code, db_paths)

def _calculate_indicator(indicator_type: str, df: pd.DataFrame, best_params: dict) -> Optional[Dict[str, Any]]:
    """根据类型和参数计算单个指标，返回前端所需格式"""
    return _calculate_indicator_svc(indicator_type, df, best_params)

# ---- API 路由 ----

@app.get("/api/stocks/list")
def get_stock_list():
    """获取股票列表"""
    try:
        stocks = []

        groups_str = config.get("CONFIG", "FUTU_GROUP", fallback='')
        host = config.get("CONFIG", "FUTU_HOST", fallback='127.0.0.1')
        port = config.getint("CONFIG", "FUTU_PORT", fallback=11111)

        if groups_str and host and port and not opend_alive(host, port):
            # OpenD 不可达时跳过分组拉取（OpenQuoteContext 会无限重连挂死），降级为仅 FUTU_CODE_LIST
            logger.warning(f"Futu OpenD 不可达（{host}:{port}），跳过 FUTU_GROUP 拉取")
            groups_str = ''

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
def get_kline(code: str, max_count: int = DEFAULT_MAX_COUNT):
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
def get_indicators(code: str):
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
                db = ParamsDB(db_path.split(',')[0])
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
def get_detect_result(code: str):
    """获取标的的 detect 结果（各指标的 best_params + performance）"""
    detect = _read_detect(code, _get_db_paths())
    if not detect:
        raise HTTPException(status_code=404, detail=f"No detect results found for {code}")
    return JSONResponse(content={'code': code, 'indicators': detect})

# ---- 富途个股跳转（与调用页面、选股结果解耦）----

@app.get("/api/futu/quote/{code}")
def futu_quote(code: str):
    """统一处理富途 App 唤起和官网兜底。"""
    try:
        normalized = validate_futu_code(code)
        host = config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1")
        port = config.getint("CONFIG", "FUTU_PORT", fallback=11111)
        return HTMLResponse(content=create_futu_launch_page(host, port, normalized))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

# ---- 条件选股结果（只读 cron 产出的静态 JSON，与选股任务解耦）----

def _screener_date(date: Optional[str]) -> Optional[str]:
    """带 date 时校验并精确使用；缺省回退到最近有结果的日期"""
    if date:
        if not re.fullmatch(r'\d{8}', date):
            raise HTTPException(status_code=400, detail="date must be YYYYMMDD")
        return date
    return screener_resolve_date(SCREENER_ROOT)

@app.get("/api/screener/list")
def screener_list(date: Optional[str] = None):
    """某日的选股结果概要；缺省日期时回退到最近一次结果"""
    resolved = _screener_date(date)
    results = screener_list_results(SCREENER_ROOT, resolved) if resolved else []
    if date and not results:
        raise HTTPException(status_code=404, detail=f"No screener results for {date}")
    return JSONResponse(content={'date': resolved if results else None, 'results': results})

@app.get("/api/screener/{strategy}/{market}")
def screener_result(strategy: str, market: str, date: Optional[str] = None):
    """单份选股结果原文"""
    if not re.fullmatch(r'[a-z0-9_]+', strategy) or not re.fullmatch(r'[A-Z]+', market):
        raise HTTPException(status_code=400, detail="invalid strategy or market")
    resolved = _screener_date(date)
    path = screener_result_path(SCREENER_ROOT, resolved, strategy, market) if resolved else None
    if not path or not path.is_file():
        raise HTTPException(status_code=404,
                            detail=f"No screener result: {strategy}/{market} @ {date or 'latest'}")
    return JSONResponse(content=json.loads(path.read_text(encoding='utf-8')))

# ---- 页面路由 ----

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """使用富途官网当前 favicon，避免各页面的默认图标请求返回 404。"""
    return RedirectResponse(FUTU_FAVICON_URL)

def _serve_page(name: str) -> HTMLResponse:
    html_path = Path(__file__).parent / name
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"{name} not found")
    return HTMLResponse(content=html_path.read_text(encoding='utf-8'))

@app.get("/detect/{code:path}")
@app.get("/detect")
def detect_page(code: str = ""):
    return _serve_page("detect.html")

@app.get("/stocks")
def stocks_page():
    return _serve_page("stocks.html")

@app.get("/screener")
def screener_page():
    return _serve_page("screener.html")

@app.get("/")
def root():
    return {"status": "ok", "message": "Futu Trends API is running"}

# ---- 启动 ----

def ensure_port_available(port: int) -> None:
    """端口被占用即报错退出（不自动漂移，保证部署后端口可预期）。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # 与 uvicorn 的 bind 语义一致，避免 TIME_WAIT 残留导致误报
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('127.0.0.1', port))
        except OSError:
            raise SystemExit(f"启动失败：端口 {port} 已被占用，请用 --port 指定其他端口")

if __name__ == "__main__":
    import uvicorn
    import argparse

    parser = argparse.ArgumentParser(description='Futu Trends API Server')
    parser.add_argument('--config', type=str, required=True, help='配置文件路径（必填）')
    parser.add_argument('--port', type=int, default=8001, help='服务端口（默认8001；被占用时报错退出）')
    args = parser.parse_args()

    # 显式加载校验配置（失败即退出）；设模块全局供各 handler 使用
    config = load_and_validate_config(args.config)
    ensure_port_available(args.port)

    logger.info(f"使用端口 {args.port}")
    print(f"API_PORT={args.port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port)
