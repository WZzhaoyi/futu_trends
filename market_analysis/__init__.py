"""market_analysis 包标记。

这里以前是 `from .ashare_concepts import *`：既没人 `import market_analysis`，
又会在包式导入时连坐（ashare_concepts 里是同级顶层导入），
于是 `from market_analysis.trading_calendar import ...` 直接 ModuleNotFoundError。
"""
