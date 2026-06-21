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
SEPA 趋势模板条件选股（行情感知能力，策略无关，只产信号数字）。

两段漏斗（见 open-secretary/plan/market-sense-capability.md）：

  段 1   L1 服务端首筛（futu OpenD `get_stock_filter`，0 历史 K 线配额）
         收盘价>EMA50>EMA150>EMA200 & 距52周低≥30% & 距52周高≤30%
         & 市值≥150亿本币 & 年度 EPS≥20% & 年度营收≥15%
  段 1.5 L1.5 快照富集 + 排序（futu `get_market_snapshot`，0 历史 K 线配额）
         一次快照补 成交额/换手率/量比 → 按成交额降序（候选 ≤400 一批即可）
  段 2   L2 本地精算（**用 yfinance 拉 K 线绕开 futu 历史配额**，仅算流控）
        对全部 L1 候选逐只精算：200MA 斜率 / 精确 trend-template 复核 /
        RS-proxy(approx) / VCP(heuristic)

设计要点：
- **不改 data.py**：段 2 用 deepcopy 的 config 临时把 `DATA_SOURCE_{market}` 固定为
  yfinance，再调 `get_kline_data`，复用其三层缓存与流控 sleep。
- **串行 + 流控**：段 2 逐只串行，节流由 data.py 的 per-call sleep 保证；基准指数整轮只取一次。
- **脏数据三层防线**：①源头 `_clean_ohlcv` 剔除 NaN/Inf/≤0 非法行；②每只独立 try/except +
  每个指标对样本不足/除零返回 None；③输出 `_sanitize` 递归清残留 NaN/Inf → 严格 JSON。
- 所有近似项带 `approx`/`heuristic` 标记，策略侧不得当真值。
"""

from __future__ import annotations

import argparse
import configparser
import copy
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# 允许 `python market_analysis/sepa_screener.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging  # noqa: E402
import futu as ft  # noqa: E402

# futu 的 FTConsoleLog 默认 INFO，会把 InitConnect/Disconnect 打到 stdout 污染 JSON。
# 必须在 import futu 之后压级（FTLog.__init__ 在 import 时已把它设回 INFO）。
logging.getLogger("FTConsoleLog").setLevel(logging.WARNING)

from data import get_kline_data  # noqa: E402


# ---------------------------------------------------------------------------
# 市场常量
# ---------------------------------------------------------------------------

# 一次查询覆盖的市场。A 股 SH/SZ 返回同一沪深全集，只查 SH 一次（避免重复计数）。
MARKETS = {
    "US": ft.Market.US,
    "HK": ft.Market.HK,
    "A": ft.Market.SH,
}

# 段 2 统一用 yfinance 拉 K 线（A 股 6xxxxx.SS/3xxxxx.SZ 覆盖良好）。
# 好处：免 futu 历史配额，且避免「yfinance 需代理 / akshare 需免代理」在同一次运行里冲突
# （本机代理环境下 akshare 直连东财常被 RemoteDisconnected）。固定数据源，不对外暴露切换。
_REFINE_SOURCE = "yfinance"

# RS-proxy 基准指数（yfinance 原生符号）。US=标普500 / HK=恒指 / A=中证A500。
BENCHMARK_YF = {"US": "^GSPC", "HK": "^HSI", "A": "000510.SS"}

# 删行占比超过此阈值 → RS 标 low_confidence（EMA/斜率稳健，仅 RS 对重度删行敏感）
_DROP_RATIO_WARN = 0.03

# get_stock_filter 单页上限
_PAGE = 200
# 段 2 取 K 线根数。需 >365 才让 yfinance 映射到 period='2y'(~490 根)，
# 从而覆盖 200MA + 252 日(12月)RS；<=365 会退到 '1y'(~247 根)，差几根算不出 12 月 RS。
_KLINE_COUNT = 400


@dataclass
class ScreenParams:
    """L1 首筛阈值（默认值 = 文档第 5 节定稿，Minervini SEPA 最低门槛）。"""

    ema_fast: int = 50
    ema_mid: int = 150
    ema_slow: int = 200
    low52_min: float = 30.0          # 距 52 周低 ≥ 30%
    high52_min: float = -30.0        # 距 52 周高 ≤ 30%（带符号，正数→命中0）
    eps_growth_min: float = 20.0     # 年度 EPS 增速 ≥ 20%
    rev_growth_min: float = 15.0     # 年度营收增速 ≥ 15%
    # 市值地板（本币）：US=US$15B；HK/A=150 亿本币
    market_val_min: dict = field(default_factory=lambda: {
        "US": 15e9, "HK": 15e9, "A": 15e9,
    })


# ---------------------------------------------------------------------------
# 段 1：L1 服务端首筛（futu，0 配额）
# ---------------------------------------------------------------------------

def _build_l1_filters(market: str, p: ScreenParams) -> list:
    """构造文档第 5 节的 filter_list。"""
    def cif(f1, p1, f2, p2):
        c = ft.CustomIndicatorFilter()
        c.ktype = ft.KLType.K_DAY
        c.stock_field1, c.stock_field1_para = f1, p1
        c.relative_position = ft.RelativePosition.MORE
        c.stock_field2, c.stock_field2_para = f2, p2
        c.is_no_filter = False
        return c

    def simple(field_, fmin):
        s = ft.SimpleFilter()
        s.stock_field = field_
        s.filter_min = fmin
        s.is_no_filter = False
        return s

    def financial(field_, fmin):
        f = ft.FinancialFilter()
        f.stock_field = field_
        f.filter_min = fmin
        f.is_no_filter = False
        f.quarter = ft.FinancialQuarter.ANNUAL  # 跨市场最齐全，无港股半年报缺口
        return f

    SF, EMA = ft.StockField, ft.StockField.EMA
    return [
        # 收盘价 > EMA50 > EMA150 > EMA200
        cif(SF.PRICE, [], EMA, [p.ema_fast]),
        cif(EMA, [p.ema_fast], EMA, [p.ema_mid]),
        cif(EMA, [p.ema_mid], EMA, [p.ema_slow]),
        # 距 52 周低 ≥ 30% / 距 52 周高 ≤ 30%（带符号 ≥ -30）
        simple(SF.CUR_PRICE_TO_LOWEST52_WEEKS_RATIO, p.low52_min),
        simple(SF.CUR_PRICE_TO_HIGHEST52_WEEKS_RATIO, p.high52_min),
        # 市值 ≥ 150 亿本币
        simple(SF.MARKET_VAL, p.market_val_min[market]),
        # 年度 EPS ≥ 20% / 营收 ≥ 15%
        financial(SF.EPS_GROWTH_RATE, p.eps_growth_min),
        financial(SF.SUM_OF_BUSINESS_GROWTH, p.rev_growth_min),
    ]


def run_l1(market: str, config, p: ScreenParams) -> list[dict]:
    """段 1：服务端首筛，翻页取全部候选。0 历史 K 线配额。"""
    if market not in MARKETS:
        raise ValueError(f"未支持的市场: {market}（可选 {list(MARKETS)}）")

    host = config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1")
    port = int(config.get("CONFIG", "FUTU_PORT", fallback=11111))
    filters = _build_l1_filters(market, p)

    ctx = ft.OpenQuoteContext(host=host, port=port)
    candidates: list[dict] = []
    try:
        begin = 0
        while True:
            ret, data = ctx.get_stock_filter(
                market=MARKETS[market], filter_list=filters, begin=begin, num=_PAGE,
            )
            if ret != ft.RET_OK:
                raise RuntimeError(f"get_stock_filter 失败: {data}")
            last_page, all_count, rows = data
            for s in rows:
                # 简单字段是普通属性；财务字段在 __dict__ 里以 (field, quarter) 元组为键。
                d = s.__dict__
                candidates.append({
                    "code": s.stock_code,
                    "name": s.stock_name,
                    "market": market,
                    "market_val": d.get("market_val"),
                    "dist_from_low52": d.get("cur_price_to_lowest52_weeks_ratio"),
                    "dist_from_high52": d.get("cur_price_to_highest52_weeks_ratio"),
                    "eps_growth": d.get(("eps_growth_rate", "annual")),
                    "rev_growth": d.get(("sum_of_business_growth", "annual")),
                })
            begin += len(rows)
            if last_page or not rows or begin >= all_count:
                break
    finally:
        ctx.close()
    return candidates


# ---------------------------------------------------------------------------
# 段 1.5：快照富集 + 排序（0 历史 K 线配额；快照走独立额度，已实测不计 K 线配额）
# ---------------------------------------------------------------------------

# get_market_snapshot 单次上限 400 标的
_SNAPSHOT_BATCH = 400


def _val(x):
    """快照字段净化：None / NaN / Inf → None，否则 float。"""
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return float(x)


def run_l15(candidates: list[dict], config) -> list[dict]:
    """
    L1.5：一次（或分批）`get_market_snapshot` 富集成交额/换手率/量比，按成交额降序。
    快照不计历史 K 线配额（已实测），故整步 0 配额。失败则降级：原样返回、不排序。
    """
    if not candidates:
        return candidates

    host = config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1")
    port = int(config.get("CONFIG", "FUTU_PORT", fallback=11111))
    codes = [c["code"] for c in candidates]

    snap_map: dict = {}
    ctx = ft.OpenQuoteContext(host=host, port=port)
    try:
        for i in range(0, len(codes), _SNAPSHOT_BATCH):  # ≤400/次，候选通常一批即可
            batch = codes[i:i + _SNAPSHOT_BATCH]
            ret, snap = ctx.get_market_snapshot(batch)
            if ret != ft.RET_OK:
                print(f"[warn] get_market_snapshot 失败，跳过 L1.5 富集排序: {snap}",
                      file=sys.stderr)
                return candidates
            for rec in snap[["code", "turnover", "turnover_rate", "volume_ratio"]].to_dict("records"):
                snap_map[rec["code"]] = rec
    finally:
        ctx.close()

    for c in candidates:
        rec = snap_map.get(c["code"], {})
        c["turnover"] = _val(rec.get("turnover"))
        c["turnover_rate"] = _val(rec.get("turnover_rate"))
        c["volume_ratio"] = _val(rec.get("volume_ratio"))

    # 按成交额降序，None 沉底
    candidates.sort(key=lambda c: c["turnover"] if c["turnover"] is not None else -1.0,
                    reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# 段 2：L2 本地精算（yfinance 绕开 futu 历史配额，仅算流控）
# ---------------------------------------------------------------------------

def _refine_config(config, market: str):
    """deepcopy config 并把该市场数据源固定为 yfinance；强制日 K。不污染共享 config。"""
    cfg = copy.deepcopy(config)
    cfg.set("CONFIG", "FUTU_PUSH_TYPE", "K_DAY")
    if market == "A":  # A 股候选含 SH./SZ. 两类前缀
        cfg.set("CONFIG", "DATA_SOURCE_SH", _REFINE_SOURCE)
        cfg.set("CONFIG", "DATA_SOURCE_SZ", _REFINE_SOURCE)
    else:
        cfg.set("CONFIG", f"DATA_SOURCE_{market}", _REFINE_SOURCE)
    return cfg


def _clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """
    脏数据第 1 层防线（源头）：剔除非法行。

    yfinance 偶发：停牌占位行(NaN)、坏行(价格 0 或负)、极少数 Inf。dropna 删不掉 0/负/Inf，
    而 0 价会让收益率/距高低点出现 Inf。这里把价格列做成「有限正数」硬约束、整行删除不合规者，
    成交量缺失/负值归零（量可以合法为 0）。
    """
    df = df.copy()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")  # 非数值→NaN
    mask = pd.Series(True, index=df.index)
    for c in ("open", "high", "low", "close"):
        mask &= (df[c] > 0) & (df[c] < float("inf"))   # 同时排除 NaN/±Inf/≤0
    df = df[mask]
    df["volume"] = df["volume"].fillna(0).clip(lower=0)
    return df


def _sanitize(obj):
    """
    脏数据第 3 层防线（输出）：递归把任何残留的 NaN/Inf 浮点转成 None，
    保证最终 JSON 严格合法（NaN/Inf 不是合法 JSON），即使上游漏算也不会污染。
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _ma_slope(ma: pd.Series, lookback: int = 21) -> float | None:
    """MA200 当前 vs lookback 根前的百分比变化（>0 即上行）。"""
    ma = ma.dropna()
    if len(ma) <= lookback or ma.iloc[-1 - lookback] == 0:
        return None
    return float((ma.iloc[-1] - ma.iloc[-1 - lookback]) / ma.iloc[-1 - lookback] * 100)


def _period_return(close: pd.Series, bars: int) -> float | None:
    if len(close) <= bars:
        return None
    a, b = close.iloc[-1], close.iloc[-1 - bars]
    if pd.isna(a) or pd.isna(b) or b == 0:
        return None
    return float((a / b - 1) * 100)


def _rs_proxy(close: pd.Series, bench: pd.Series | None) -> dict:
    """标的 vs 单一基准的 3/6/12 月超额收益。approx，非 IBD 百分位。"""
    periods = {"3m": 63, "6m": 126, "12m": 252}
    out: dict = {"approx": True, "note": "excess return vs single benchmark index"}
    for name, bars in periods.items():
        sret = _period_return(close, bars)
        bret = _period_return(bench, bars) if bench is not None else None
        out[f"excess_{name}"] = (
            round(sret - bret, 2) if (sret is not None and bret is not None) else None
        )
    return out


def _vcp_heuristic(df: pd.DataFrame, window: int = 8) -> dict:
    """
    VCP 启发式：取近段摆动高低点，看回调深度是否递减、量能是否同步收缩。
    仅给结构化测量，heuristic，非精确 VCP 判定。
    """
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    n = len(close)
    if n < window * 4:
        return {"heuristic": True, "ok": False, "note": "数据不足"}

    # 滚动局部极值定位摆动点
    is_peak = (high == high.rolling(window, center=True).max())
    is_trough = (low == low.rolling(window, center=True).min())
    peaks = list(high[is_peak].index)
    troughs = list(low[is_trough].index)

    # 由相邻 peak→trough 计回调深度序列（取最近 ≤4 段）
    depths: list[float] = []
    for pk in peaks[-5:]:
        later = [t for t in troughs if t > pk]
        if later:
            depth = (high[pk] - low[later[0]]) / high[pk] * 100
            if depth > 0:
                depths.append(round(float(depth), 2))
    depths = depths[-4:]

    depths_decreasing = (
        len(depths) >= 2 and all(a >= b for a, b in zip(depths, depths[1:]))
    )
    recent_vol = vol.tail(window).mean()
    base_vol = vol.tail(window * 4).head(window * 3).mean()
    vol_contracting = bool(base_vol and recent_vol < base_vol)

    return {
        "heuristic": True,
        "num_contractions": len(depths),
        "contraction_depths_pct": depths,
        "depths_decreasing": bool(depths_decreasing),
        "volume_contracting": vol_contracting,
        "pivot": round(float(high.tail(window * 2).max()), 4),
    }


def _fetch_benchmark(market: str, config) -> pd.Series | None:
    """整轮只取一次的基准指数收盘序列（yfinance 直取，失败则降级 RS=None）。"""
    import yfinance as yf
    from data import setup_global_proxy, _proxy_configured

    if not _proxy_configured:
        proxy = config.get("CONFIG", "PROXY", fallback=None)
        if proxy:
            setup_global_proxy(proxy)
    try:
        hist = yf.Ticker(BENCHMARK_YF[market]).history(period="2y")
        if hist.empty:
            return None
        s = hist["Close"].dropna().copy()
        s.index = pd.to_datetime(s.index)
        if s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        return s
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 基准 {BENCHMARK_YF[market]} 取数失败，RS-proxy 降级: {e}", file=sys.stderr)
        return None


def refine_one(cand: dict, cfg, bench: pd.Series | None, p: ScreenParams) -> dict:
    """对单只候选做段 2 精算，原地补信号字段。"""
    out = dict(cand)
    df = get_kline_data(cand["code"], cfg, max_count=_KLINE_COUNT)
    if df is None or df.empty:
        out["l2"] = {"ok": False, "note": "取数失败或无数据"}
        return out

    n_raw = len(df)
    df = _clean_ohlcv(df)  # 第 1 层：剔除 NaN/Inf/≤0 非法行
    n = len(df)
    dropped = n_raw - n
    if n < p.ema_slow:
        out["l2"] = {"ok": False, "note": f"清洗后有效K线不足({n}<{p.ema_slow})"}
        return out

    close = df["close"]
    ema_f = close.ewm(span=p.ema_fast, adjust=False).mean()
    ema_m = close.ewm(span=p.ema_mid, adjust=False).mean()
    ema_s = close.ewm(span=p.ema_slow, adjust=False).mean()

    hi52, lo52 = close.tail(252).max(), close.tail(252).min()
    last = close.iloc[-1]
    slope = _ma_slope(ema_s)

    template_pass = bool(
        last > ema_f.iloc[-1] > ema_m.iloc[-1] > ema_s.iloc[-1]
        and (slope is not None and slope > 0)
        and (lo52 and (last - lo52) / lo52 * 100 >= p.low52_min)
        and (hi52 and (last - hi52) / hi52 * 100 >= p.high52_min)
    )

    rs = _rs_proxy(close, bench)
    # 删行透明化：EMA/斜率对删行稳健，但 RS 是「N 根前」点对点比值，大量删行会让
    # 「252 根前 ≠ 12 月前」而漂移（实测删 15% → RS12m 漂 ~3.5pp）。删行占比偏高时标注低置信。
    if dropped and dropped / n_raw > _DROP_RATIO_WARN:
        rs["low_confidence"] = True
        rs["note"] += f"；删行 {dropped}/{n_raw} 较多，RS 可能漂移"

    out["l2"] = {
        "ok": True,
        "close": round(float(last), 4),
        "bars": n,
        "bars_dropped": dropped,
        "trend_template_pass": template_pass,
        "ma200_slope_pct": round(slope, 2) if slope is not None else None,
        "ma200_uptrend": bool(slope is not None and slope > 0),
        "dist_from_low52_pct": round(float((last - lo52) / lo52 * 100), 2) if lo52 else None,
        "dist_from_high52_pct": round(float((last - hi52) / hi52 * 100), 2) if hi52 else None,
        "rs_proxy": rs,
        "vcp": _vcp_heuristic(df),
    }
    return out


def run_l2(candidates: list[dict], market: str, config, p: ScreenParams) -> list[dict]:
    """段 2：对全部候选用 yfinance 精算。串行 + 流控由 data.py 的 sleep 保证。
    每只独立 try/except——单只坏票不影响整批（第 2 层防线）。"""
    cfg = _refine_config(config, market)
    bench = _fetch_benchmark(market, config)

    refined: list[dict] = []
    total = len(candidates)
    for i, cand in enumerate(candidates, 1):
        print(f"[L2 {i}/{total}] {cand['code']}", file=sys.stderr)
        try:
            refined.append(refine_one(cand, cfg, bench, p))
        except Exception as e:  # noqa: BLE001
            cand = dict(cand)
            cand["l2"] = {"ok": False, "note": f"精算异常: {e}"}
            refined.append(cand)
    return refined


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------

def screen(market: str, config, p: ScreenParams | None = None,
           refine: bool = True) -> dict:
    """完整两段漏斗。返回结构化结果（可被 CLI / 上层 agent 直接消费）。"""
    p = p or ScreenParams()
    l1 = run_l1(market, config, p)
    l1 = run_l15(l1, config)  # 快照富集 turnover/换手率/量比 + 按成交额排序（0 K线配额）
    result = {
        "market": market,
        "l1_count": len(l1),
        "quota_used": 0,  # L1 选股 + L1.5 快照均 0 历史 K 线配额；L2 走 yfinance（免 futu 配额）
    }
    if refine and l1:
        result["candidates"] = run_l2(l1, market, config, p)
        result["l2_refined"] = len(l1)
    else:
        result["candidates"] = l1
        result["l2_refined"] = 0
    # 第 3 层防线：递归清掉任何残留 NaN/Inf，保证严格 JSON
    return _sanitize(result)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="SEPA 趋势模板条件选股")
    ap.add_argument("--market", required=True, choices=list(MARKETS), help="US / HK / A")
    ap.add_argument("--config", default="config_template.ini", help="配置文件路径")
    ap.add_argument("--no-refine", action="store_true", help="只跑 L1 首筛（0 配额）")
    ap.add_argument("--out", help="结果写入 JSON 文件；缺省打印到 stdout")
    return ap


def main():
    args = _build_parser().parse_args()
    config = configparser.ConfigParser()
    if not config.read(args.config, encoding="utf-8"):
        sys.exit(f"配置文件读取失败: {args.config}")
    result = screen(args.market, config, refine=not args.no_refine)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}（L1={result['l1_count']}, L2精算={result['l2_refined']}）",
              file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
