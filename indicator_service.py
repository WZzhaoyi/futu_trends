#  Futu Trends
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#  Written by Joey <wzzhaoyi@outlook.com>, 2026
#  Copyright (c)  Joey - All Rights Reserved

"""
指标计算共享服务（无框架依赖）：指标序列计算 + detect 读取 + ParamsDB 路径解析。
供 HTTP 前端(gui/backend/api.py) 与终端前端(cli/main.py) 共用，保证口径一致。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from signal_analysis import KD, MACD, RSI  # noqa: E402
from params_db import ParamsDB  # noqa: E402
from signal_analysis.defaults import (  # noqa: E402,F401  默认参数单一来源，re-export 供调用方复用
    DEFAULT_PARAMS, DEFAULT_META, default_stock_params,
)

logger = logging.getLogger(__name__)

INDICATOR_CLASSES = {"MACD": MACD, "KD": KD, "RSI": RSI}

# 超买/超卖默认水平
INDICATOR_DEFAULTS = {"MACD": (0, 0), "KD": (20, 80), "RSI": (30, 70)}


def get_db_paths(config) -> Dict[str, Optional[str]]:
    """从 config 读取各指标的 ParamsDB 路径（None 表示未配置）。"""
    return {
        "MACD": config.get("CONFIG", "MACD_PARAMS_DB", fallback=None),
        "KD": config.get("CONFIG", "KD_PARAMS_DB", fallback=None),
        "RSI": config.get("CONFIG", "RSI_PARAMS_DB", fallback=None),
    }


def read_detect(code: str, db_paths: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """读取各指标的 best_params / meta_info / performance（detect 结果）。"""
    result: Dict[str, Any] = {}
    for indicator_type, db_path in db_paths.items():
        if not db_path:
            continue
        try:
            db = ParamsDB(db_path.split(",")[0])
            data = db.get_stock_params(code)
            if data and data.get("best_params"):
                result[indicator_type] = {
                    "best_params": data["best_params"],
                    "meta_info": data["meta_info"],
                    "performance": data["performance"],
                }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"读取 {indicator_type} 参数失败 {code}: {e}")
    return result


def calculate_indicator(indicator_type: str, df: pd.DataFrame, params: dict) -> Optional[Dict[str, Any]]:
    """按类型与参数计算单个指标，返回完整序列（list 化、NaN→0）。"""
    indicator = INDICATOR_CLASSES[indicator_type]()
    default_oversold, default_overbought = INDICATOR_DEFAULTS[indicator_type]

    if indicator_type == "MACD":
        vmacd, signal = indicator.indicator_calculate(df.copy(), params)
        return {
            "vmacd": vmacd.fillna(0).tolist(),
            "signal": signal.fillna(0).tolist(),
            "hist": (2 * (vmacd - signal)).fillna(0).tolist(),
        }
    if indicator_type == "KD":
        k, d = indicator.indicator_calculate(df.copy(), params)
        return {
            "k": k.fillna(0).tolist(),
            "d": d.fillna(0).tolist(),
            "oversold": params.get("oversold", default_oversold),
            "overbought": params.get("overbought", default_overbought),
        }
    if indicator_type == "RSI":
        values = indicator.indicator_calculate(df.copy(), params)
        return {
            "values": values.fillna(0).tolist(),
            "oversold": params.get("oversold", default_oversold),
            "overbought": params.get("overbought", default_overbought),
        }
    return None
