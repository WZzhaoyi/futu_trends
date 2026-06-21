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
指标默认参数 —— **全项目单一来源**。

放在 signal_analysis（最底层指标领域包，不向上依赖任何模块）里，让 trends.py / cli /
api.py 等都**向下**依赖它，避免「核心依赖前端」的反向依赖与潜在循环 import：
  trends.py / cli.* / gui.* ──▶ signal_analysis.defaults（叶子，永不反向 import）

ParamsDB 缺某标的参数时回退到此。值与历史生产默认一致。
"""

from typing import Any, Dict

# 指标计算 + 形态确认的默认参数（best_params 部分）
DEFAULT_PARAMS = {
    "MACD": {"fast_period": 12, "slow_period": 26, "signal_period": 9, "macd_extreme": 150},
    "KD": {"k_period": 15, "d_period": 5, "overbought": 50, "oversold": 50},
    "RSI": {"rsi_period": 7, "oversold": 30, "overbought": 70},
}

# 形态确认默认 meta（Indicator.calculate(mode='check') 用；只算指标序列时用不到）
DEFAULT_META = {"target_multiplier": 1.5, "atr_period": 60}


def default_stock_params(indicator_type: str) -> Dict[str, Any]:
    """完整默认参数记录 {best_params, meta_info, performance}，供 ParamsDB 缺记录时回退。"""
    return {
        "best_params": dict(DEFAULT_PARAMS[indicator_type]),
        "meta_info": dict(DEFAULT_META),
        "performance": {},
    }


__all__ = ["DEFAULT_PARAMS", "DEFAULT_META", "default_stock_params"]
