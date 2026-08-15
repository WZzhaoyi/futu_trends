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
futu_trends 统一功能导出 CLI（只读，面向终端 / 上层 agent，输出结构化 JSON）。

这是项目对外的统一命令入口。随项目演进，各功能域以子命令形式挂载于此；
**market-sense（行情感知）是其中第一组功能**，后续可扩展更多功能域。

  运行：python -m cli <command> …   或   python cli/main.py <command> …

与 gui/backend/api.py 共用同一逻辑底座（data / signal_analysis / tools / params_db /
fundamental_analysis screeners / indicator_service），但不依赖其 HTTP/FastAPI 外壳。

== market-sense 子命令（输出 JSON）==
  kline   读 OHLCV（data.get_kline_data，源由 config 决定，缓存走绝对路径）
  screen  条件选股（策略脚本条件 + Futu OpenD L1 + snapshot；可选 yfinance L2）
  signals 单/多只 → 经典指标(EMA/MACD/KD/RSI，ParamsDB 最优参数，缺则回退默认)
          + detect(best_params/meta/performance) + L2(trend-template/200MA/RS/VCP)

== 运维子命令 ==
  web     子进程启动 gui/backend/api.py 的 Web 服务（页面 + /api/* 接口，默认 8001，
          占用报错；--forever 崩溃自动重启）
  pm2     管理 order-engine / signal-api / csi-flow / etf-premium /
          momentum-rotation 的 PM2 进程

约定：JSON 类子命令输出严格 JSON 到 stdout、进度/告警到 stderr；web/pm2 例外。
发布：现以仓库内脚本交付，已做到 CWD 无关（缓存绝对路径）+ OpenD 预检，
      可零改动加 pyproject console_scripts（如 ft = "cli.main:main"）。
"""

from __future__ import annotations

import argparse
import configparser
import copy
import json
import logging
import math
import sys
from pathlib import Path

# --- 路径引导：根 + fundamental_analysis + market_analysis + 本目录 ---
_ROOT = Path(__file__).resolve().parents[1]
for _p in (
    str(_ROOT),
    str(_ROOT / "fundamental_analysis"),
    str(_ROOT / "market_analysis"),
    str(Path(__file__).resolve().parent),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# PM2 管理只依赖标准库；在加载 futu/pandas 等行情依赖前快速分派。
if __name__ == "__main__" and sys.argv[1:2] == ["pm2"]:
    from pm2_service import main as _pm2_main
    raise SystemExit(_pm2_main(sys.argv[2:]))

import futu as ft  # noqa: E402

logging.getLogger("FTConsoleLog").setLevel(logging.WARNING)  # 防 InitConnect 日志污染 stdout

from data import get_kline_data, opend_alive  # noqa: E402
from tools import EMA  # noqa: E402
import futu_fundamental_screener as fs  # noqa: E402
import deep_value_screener as deep_value_strategy  # noqa: E402
import growth_value_screener as growth_value_strategy  # noqa: E402
import indicator_service as isvc  # noqa: E402
import quality_screener as quality_strategy  # noqa: E402
import sepa_screener as sepa_strategy  # noqa: E402

DEFAULT_COUNT = 400
DEFAULT_EMA_PERIOD = 240
_REFINE_SOURCE = "yfinance"
_KLINE_COUNT = 400
_DROP_RATIO_WARN = 0.03

# 代码前缀 → screen/benchmark 用的市场键
_MARKET_OF = {"US": "US", "HK": "HK", "SH": "A", "SZ": "A"}
_BENCHMARK_YF = {"US": "^GSPC", "HK": "^HSI", "A": "000510.SS"}
_SCREEN_STRATEGIES = {
    "sepa": sepa_strategy,
    "growth_value": growth_value_strategy,
    "quality": quality_strategy,
    "deep_value": deep_value_strategy,
}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _load_config(path: str) -> configparser.ConfigParser:
    """加载并校验配置；不满足即报错退出（不回退默认路径）。"""
    if not path:
        sys.exit("必须通过 --config 指定配置文件")
    cfg = configparser.ConfigParser()
    if not cfg.read(path, encoding="utf-8"):
        sys.exit(f"配置文件不存在或不可读: {path}")
    if not cfg.has_section("CONFIG"):
        sys.exit(f"配置缺少 [CONFIG] 段: {path}")
    return cfg


def _cache_dir(config) -> str:
    """缓存目录解析为**绝对路径**，与运行时 CWD 解耦。config 可用 CACHE_DIR 覆盖。"""
    raw = config.get("CONFIG", "CACHE_DIR", fallback=str(_ROOT / "data"))
    p = Path(raw)
    if not p.is_absolute():
        p = _ROOT / p
    return str(p)


def _check_opend(config, timeout: float = 2.0) -> bool:
    """OpenD 可达性预检；探测逻辑与缓存见 data.opend_alive（带 TTL 的 TCP 心跳）。"""
    host = config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1")
    port = int(config.get("CONFIG", "FUTU_PORT", fallback=11111))
    return opend_alive(host, port, timeout)


def _last(values) -> float | None:
    """取序列/列表最后一个有限值。"""
    import math
    if values is None:
        return None
    seq = list(values) if not isinstance(values, list) else values
    for v in reversed(seq):
        if isinstance(v, (int, float)) and math.isfinite(v):
            return float(v)
    return None


def _emit(result: dict, out_path: str | None, pretty: bool) -> None:
    """统一出口：sanitize → 写文件 / 打印 stdout。"""
    result = fs.sanitize(result)
    text = json.dumps(result, ensure_ascii=False, indent=2 if pretty else None)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
        print(f"已写入 {out_path}", file=sys.stderr)
    else:
        print(text)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_kline(args, config) -> dict:
    if args.ktype:
        config.set("CONFIG", "FUTU_PUSH_TYPE", args.ktype)
    df = get_kline_data(args.code, config, max_count=args.count, file_cache_dir=_cache_dir(config))
    if df is None or df.empty:
        return {"code": args.code, "bars": [], "error": "无数据或取数失败"}
    bars = [
        {
            "time": str(idx),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["volume"]),
        }
        for idx, r in df.iterrows()
    ]
    return {
        "code": args.code,
        "ktype": config.get("CONFIG", "FUTU_PUSH_TYPE", fallback="K_DAY"),
        "count": len(bars),
        "bars": bars,
    }


def cmd_screen(args, config) -> dict:
    if not _check_opend(config):
        host = config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1")
        port = config.get("CONFIG", "FUTU_PORT", fallback="11111")
        sys.exit(f"OpenD 不可达（{host}:{port}）。screen 需要 futu OpenD 运行。")
    strategy = _SCREEN_STRATEGIES[args.strategy]
    return fs.screen(
        strategy, args.market, config,
        snapshot=not args.no_snapshot,
        limit=args.limit,
        refine=args.refine,
        refine_limit=args.refine_limit,
        refine_sleep=args.refine_sleep,
    )


class _TrendParams:
    ema_fast = 50
    ema_mid = 150
    ema_slow = 200
    low52_min = 30.0
    high52_min = -30.0


def _refine_config(config, market: str):
    cfg = copy.deepcopy(config)
    cfg.set("CONFIG", "FUTU_PUSH_TYPE", "K_DAY")
    if market == "A":
        cfg.set("CONFIG", "DATA_SOURCE_SH", _REFINE_SOURCE)
        cfg.set("CONFIG", "DATA_SOURCE_SZ", _REFINE_SOURCE)
    else:
        cfg.set("CONFIG", f"DATA_SOURCE_{market}", _REFINE_SOURCE)
    return cfg


def _clean_ohlcv(df):
    import pandas as pd

    df = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    mask = pd.Series(True, index=df.index)
    for col in ("open", "high", "low", "close"):
        mask &= (df[col] > 0) & (df[col] < float("inf"))
    df = df[mask]
    df["volume"] = df["volume"].fillna(0).clip(lower=0)
    return df


def _ma_slope(ma, lookback: int = 21) -> float | None:
    ma = ma.dropna()
    if len(ma) <= lookback or ma.iloc[-1 - lookback] == 0:
        return None
    return float((ma.iloc[-1] - ma.iloc[-1 - lookback]) / ma.iloc[-1 - lookback] * 100)


def _period_return(close, bars: int) -> float | None:
    if close is None or len(close) <= bars:
        return None
    latest, base = close.iloc[-1], close.iloc[-1 - bars]
    if not math.isfinite(float(latest)) or not math.isfinite(float(base)) or base == 0:
        return None
    return float((latest / base - 1) * 100)


def _rs_proxy(close, bench) -> dict:
    out = {"approx": True, "note": "excess return vs single benchmark index"}
    for name, bars in {"3m": 63, "6m": 126, "12m": 252}.items():
        sret = _period_return(close, bars)
        bret = _period_return(bench, bars) if bench is not None else None
        out[f"excess_{name}"] = round(sret - bret, 2) if sret is not None and bret is not None else None
    return out


def _vcp_heuristic(df, window: int = 8) -> dict:
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    n = len(close)
    if n < window * 4:
        return {"heuristic": True, "ok": False, "note": "数据不足"}

    high_v = high.to_numpy(dtype=float)
    low_v = low.to_numpy(dtype=float)
    vol_v = vol.to_numpy(dtype=float)
    roll_max = high.rolling(window, center=True).max().to_numpy()
    roll_min = low.rolling(window, center=True).min().to_numpy()
    peaks = [i for i in range(n) if high_v[i] == roll_max[i]]
    troughs = [i for i in range(n) if low_v[i] == roll_min[i]]

    depths = []
    for peak in peaks[-5:]:
        later = [trough for trough in troughs if trough > peak]
        if later and high_v[peak] > 0:
            depth = (high_v[peak] - low_v[later[0]]) / high_v[peak] * 100
            if depth > 0:
                depths.append(round(float(depth), 2))
    depths = depths[-4:]

    base_vol = vol_v[-window * 4:-window].mean()
    recent_vol = vol_v[-window:].mean()
    return {
        "heuristic": True,
        "num_contractions": len(depths),
        "contraction_depths_pct": depths,
        "depths_decreasing": bool(
            len(depths) >= 2 and all(a >= b for a, b in zip(depths, depths[1:]))
        ),
        "volume_contracting": bool(base_vol and recent_vol < base_vol),
        "pivot": round(float(high_v[-window * 2:].max()), 4),
    }


def _fetch_benchmark(market: str, config):
    import pandas as pd
    import yfinance as yf
    from data import setup_global_proxy, _proxy_configured

    if not _proxy_configured:
        proxy = config.get("CONFIG", "PROXY", fallback=None)
        if proxy:
            setup_global_proxy(proxy)
    try:
        hist = yf.Ticker(_BENCHMARK_YF[market]).history(period="2y")
        if hist.empty:
            return None
        series = hist["Close"].dropna().copy()
        series.index = pd.to_datetime(series.index)
        if series.index.tz is not None:
            series.index = series.index.tz_localize(None)
        return series
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 基准 {_BENCHMARK_YF[market]} 取数失败，RS 降级: {exc}",
              file=sys.stderr)
        return None


def _refine_trend_one(code: str, market: str, cfg, bench) -> dict:
    p = _TrendParams()
    df = get_kline_data(code, cfg, max_count=_KLINE_COUNT)
    if df is None or df.empty:
        return {"ok": False, "note": "取数失败或无数据"}

    n_raw = len(df)
    df = _clean_ohlcv(df)
    dropped = n_raw - len(df)
    if len(df) < p.ema_slow:
        return {"ok": False, "note": f"清洗后有效K线不足({len(df)}<{p.ema_slow})"}

    close = df["close"]
    ema_f = close.ewm(span=p.ema_fast, adjust=False).mean()
    ema_m = close.ewm(span=p.ema_mid, adjust=False).mean()
    ema_s = close.ewm(span=p.ema_slow, adjust=False).mean()
    last = close.iloc[-1]
    hi52, lo52 = close.tail(252).max(), close.tail(252).min()
    slope = _ma_slope(ema_s)
    template_pass = bool(
        last > ema_f.iloc[-1] > ema_m.iloc[-1] > ema_s.iloc[-1]
        and slope is not None and slope > 0
        and lo52 and (last - lo52) / lo52 * 100 >= p.low52_min
        and hi52 and (last - hi52) / hi52 * 100 >= p.high52_min
    )

    rs = _rs_proxy(close, bench)
    if dropped and dropped / n_raw > _DROP_RATIO_WARN:
        rs["low_confidence"] = True
        rs["note"] += f"；删行 {dropped}/{n_raw} 较多，RS 可能漂移"

    return {
        "ok": True,
        "close": round(float(last), 4),
        "bars": len(df),
        "bars_dropped": dropped,
        "trend_template_pass": template_pass,
        "ema200_slope_pct": round(slope, 2) if slope is not None else None,
        "ema200_uptrend": bool(slope is not None and slope > 0),
        "dist_from_low52_pct": round(float((last - lo52) / lo52 * 100), 2) if lo52 else None,
        "dist_from_high52_pct": round(float((last - hi52) / hi52 * 100), 2) if hi52 else None,
        "rs_proxy": rs,
        "vcp": _vcp_heuristic(df),
    }


def _signals_one(code: str, config, bench_cache: dict, db_paths: dict,
                 ema_period: int, count: int) -> dict:
    """单只：经典指标(最优/默认参数) + detect + L2 信号。全程 yfinance（免 OpenD）。"""
    market = _MARKET_OF.get(code.split(".")[0].upper())
    out: dict = {"code": code, "market": market}
    if market is None:
        out["error"] = "未知市场前缀"
        return out

    rcfg = _refine_config(config, market)  # 固定 yfinance + 日 K
    if market not in bench_cache:
        bench_cache[market] = _fetch_benchmark(market, config)
    bench = bench_cache[market]

    # K 线（与 L2 同一内存缓存键，单次网络拉取复用）
    df = get_kline_data(code, rcfg, max_count=count)
    if df is not None and not df.empty:
        df = _clean_ohlcv(df)  # 与 L2 同源清洗：剔除 NaN/Inf/≤0 行

    indicators: dict = {}
    if df is not None and not df.empty:
        indicators["ema"] = {"period": ema_period, "value": _last(EMA(df["close"], ema_period))}
        for itype in ("MACD", "KD", "RSI"):
            dbp = db_paths.get(itype)
            params, source = isvc.DEFAULT_PARAMS[itype], "default"
            if dbp:
                try:
                    rec = isvc.ParamsDB(dbp.split(",")[0]).get_stock_params(code)
                    if rec and rec.get("best_params"):
                        params, source = rec["best_params"], "best_params"
                except Exception as e:  # noqa: BLE001
                    out.setdefault("warn", []).append(f"{itype} 取参失败: {e}")
            calc = isvc.calculate_indicator(itype, df, params)
            if itype == "MACD":
                indicators["macd"] = {"vmacd": _last(calc["vmacd"]), "signal": _last(calc["signal"]),
                                      "hist": _last(calc["hist"]), "params_source": source}
            elif itype == "KD":
                indicators["kd"] = {"k": _last(calc["k"]), "d": _last(calc["d"]),
                                    "oversold": calc["oversold"], "overbought": calc["overbought"],
                                    "params_source": source}
            else:
                indicators["rsi"] = {"value": _last(calc["values"]), "oversold": calc["oversold"],
                                     "overbought": calc["overbought"], "params_source": source}
    else:
        out["warn"] = (out.get("warn") or []) + ["K线取数失败，经典指标跳过"]

    out["indicators"] = indicators
    out["detect"] = isvc.read_detect(code, db_paths)
    out["l2"] = _refine_trend_one(code, market, rcfg, bench)
    return out


def cmd_signals(args, config) -> dict:
    ema_period = config.getint("CONFIG", "EMA_PERIOD", fallback=DEFAULT_EMA_PERIOD)
    db_paths = isvc.get_db_paths(config)
    bench_cache: dict = {}
    signals = []
    for i, code in enumerate(args.codes, 1):
        print(f"[signals {i}/{len(args.codes)}] {code}", file=sys.stderr)
        # 单只异常只记错并继续，不拖垮整批。
        try:
            signals.append(_signals_one(code, config, bench_cache, db_paths, ema_period, args.count))
        except Exception as e:  # noqa: BLE001
            signals.append({"code": code, "error": f"信号计算异常: {e}"})
    return {"count": len(signals), "signals": signals}


def cmd_web(args, config) -> None:
    """启动 gui/backend/api.py 的 Web 服务（页面 + /api/* 接口），前台运行至中断。
    端口默认 8001，被占用时报错退出（--port 指定其他端口）。
    --forever 为纯 Python 跨平台守护：异常退出自动重启（指数退避），
    正常退出/Ctrl-C 不重启；启动即失败视为配置错误，直接退出不循环。"""
    import subprocess
    import time
    api_py = _ROOT / "gui" / "backend" / "api.py"
    if not api_py.exists():
        sys.exit(f"未找到 Web 服务脚本: {api_py}")
    cmd = [sys.executable, str(api_py), "--config", str(Path(args.config).resolve())]
    if args.port:
        cmd += ["--port", str(args.port)]
    print(f"启动 Web 服务（Ctrl-C 停止）: {' '.join(cmd)}", file=sys.stderr)
    if not args.forever:
        sys.exit(subprocess.run(cmd).returncode)

    backoff, first = 1, True
    while True:
        start = time.monotonic()
        try:
            rc = subprocess.run(cmd).returncode
        except KeyboardInterrupt:
            sys.exit(130)
        ran = time.monotonic() - start
        if rc == 0:
            sys.exit(0)
        # api.py 冷启动（重依赖导入）约 4-5s，失败判定窗口须留足余量
        if first and ran < 15:
            sys.exit(rc)  # 启动即失败多为端口/配置问题，重启无意义
        first = False
        # 存活超过 60s 视为曾健康，退避归位；连续快速崩溃则指数退避封顶 60s
        backoff = 1 if ran >= 60 else min(backoff * 2, 60)
        print(f"Web 服务异常退出（rc={rc}，存活 {ran:.0f}s），{backoff}s 后重启",
              file=sys.stderr)
        time.sleep(backoff)


def cmd_pm2(args) -> None:
    """程序化调用 main() 时的 PM2 分派；常规脚本入口会在导入行情依赖前分派。"""
    from cli.pm2_service import main as pm2_main
    raise SystemExit(pm2_main(args.pm2_args))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    # 全局选项放 parent parser，使其在子命令前后均可用（如 `signals --code X --out Y`）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", required=True, help="配置文件路径（必填，不提供默认）")
    common.add_argument("--out", help="结果写入 JSON 文件；缺省打印 stdout")
    common.add_argument("--pretty", action="store_true", help="缩进美化 JSON")

    # 全局选项只挂子命令（放在子命令之后）；顶层不带 common，避免与 required --config 双定义冲突
    ap = argparse.ArgumentParser(prog="futu-trends",
                                 description="futu_trends 统一功能与本地进程管理 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pk = sub.add_parser("kline", parents=[common], help="读 OHLCV")
    pk.add_argument("--code", required=True, help="如 US.AAPL / HK.00700 / SH.600519")
    pk.add_argument("--count", type=int, default=DEFAULT_COUNT, help="K 线根数")
    pk.add_argument("--ktype", help="覆盖 K 线周期，如 K_DAY/K_60M（缺省用 config）")
    pk.set_defaults(func=cmd_kline)

    ps = sub.add_parser("screen", parents=[common], help="条件选股")
    ps.add_argument("--market", required=True, choices=list(fs.MARKETS), help="US / HK / A")
    ps.add_argument("--strategy", default="sepa", choices=list(_SCREEN_STRATEGIES),
                    help="筛选策略；默认 sepa")
    ps.add_argument("--limit", type=int, help="按 snapshot_score 排序后的输出数量")
    ps.add_argument("--no-snapshot", action="store_true", help="只跑 get_stock_filter")
    ps.add_argument("--refine", action="store_true", help="对候选运行 yfinance L2 精算")
    ps.add_argument("--refine-limit", type=int, default=fs.YFINANCE_REFINE_LIMIT,
                    help=f"最多精算前多少只，不截断返回列表；默认 {fs.YFINANCE_REFINE_LIMIT}")
    ps.add_argument("--refine-sleep", type=float, default=fs.YFINANCE_SLEEP_SEC,
                    help=f"yfinance 单只间隔秒数；默认 {fs.YFINANCE_SLEEP_SEC}")
    ps.set_defaults(func=cmd_screen)

    pg = sub.add_parser("signals", parents=[common], help="单/多只指标信号 + detect + L2")
    pg.add_argument("--code", dest="codes", action="append", required=True,
                    help="可重复传多只，如 --code US.AAPL --code US.NVDA")
    pg.add_argument("--count", type=int, default=DEFAULT_COUNT, help="K 线根数")
    pg.set_defaults(func=cmd_signals)

    pw = sub.add_parser("web", parents=[common], help="启动 Web UI（页面 + /api/* 接口）")
    pw.add_argument("--port", type=int, help="Web 服务端口（默认 8001；被占用时报错退出）")
    pw.add_argument("--forever", action="store_true",
                    help="崩溃自动重启（跨平台简易守护；正常退出/Ctrl-C 不重启）")
    pw.set_defaults(func=cmd_web)

    ppm2 = sub.add_parser(
        "pm2",
        help=(
            "管理 order-engine / signal-api / csi-flow / etf-premium 的 PM2 进程"
        ),
    )
    ppm2.add_argument("pm2_args", nargs=argparse.REMAINDER,
                      help="order-engine|signal-api|csi-flow|etf-premium|save 及其参数")
    ppm2.set_defaults(func=cmd_pm2, skip_config=True)
    return ap


def main():
    args = _build_parser().parse_args()
    if getattr(args, "skip_config", False):
        args.func(args)
        return
    config = _load_config(args.config)
    result = args.func(args, config)
    _emit(result, args.out, args.pretty)


if __name__ == "__main__":
    main()
