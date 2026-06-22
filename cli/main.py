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
sepa_screener / indicator_service），但不依赖其 HTTP/FastAPI 外壳。

== market-sense 子命令（输出 JSON）==
  kline   读 OHLCV（data.get_kline_data，源由 config 决定，缓存走绝对路径）
  screen  条件选股 L1+L1.5+L2（sepa_screener.screen；启动预检 OpenD）
  signals 单/多只 → 经典指标(EMA/MACD/KD/RSI，ParamsDB 最优参数，缺则回退默认)
          + detect(best_params/meta/performance) + L2(trend-template/200MA/RS/VCP)

== gui 子命令 ==
  web     子进程启动 gui/backend/api.py 的 Web 服务（页面 + /api/* 接口，端口自动选）

约定：JSON 类子命令输出严格 JSON 到 stdout、进度/告警到 stderr；web 例外（前台运行服务）。
发布：现以仓库内脚本交付，已做到 CWD 无关（缓存绝对路径）+ OpenD 预检，
      可零改动加 pyproject console_scripts（如 ft = "cli.main:main"）。
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import sys
from pathlib import Path

# --- 路径引导：根 + market_analysis（绕过其包 __init__）+ 本目录 ---
_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "market_analysis"), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import futu as ft  # noqa: E402

logging.getLogger("FTConsoleLog").setLevel(logging.WARNING)  # 防 InitConnect 日志污染 stdout

from data import get_kline_data  # noqa: E402
from tools import EMA  # noqa: E402
import sepa_screener as sc  # noqa: E402  (直 import，绕过 market_analysis/__init__ 的无关加载)
import indicator_service as isvc  # noqa: E402

DEFAULT_COUNT = 400
DEFAULT_EMA_PERIOD = 240

# 代码前缀 → screen/benchmark 用的市场键
_MARKET_OF = {"US": "US", "HK": "HK", "SH": "A", "SZ": "A"}


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
    """
    OpenD 可达性预检：TCP 探测端口（带超时）。

    不能用 OpenQuoteContext —— 端口不通时 futu 会后台无限重连（不抛异常）导致挂死。
    TCP connect 成功即认为 OpenD 在监听；真正协议异常留给后续实际调用报错。
    """
    import socket
    host = config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1")
    port = int(config.get("CONFIG", "FUTU_PORT", fallback=11111))
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


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
    result = sc._sanitize(result)
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
    return sc.screen(args.market, config, refine=not args.no_refine)


def _signals_one(code: str, config, bench_cache: dict, db_paths: dict,
                 ema_period: int, count: int) -> dict:
    """单只：经典指标(最优/默认参数) + detect + L2 信号。全程 yfinance（免 OpenD）。"""
    market = _MARKET_OF.get(code.split(".")[0].upper())
    out: dict = {"code": code, "market": market}
    if market is None:
        out["error"] = "未知市场前缀"
        return out

    rcfg = sc._refine_config(config, market)  # 固定 yfinance + 日 K
    if market not in bench_cache:
        bench_cache[market] = sc._fetch_benchmark(market, config)
    bench = bench_cache[market]

    # K 线（与 L2 同一内存缓存键，单次网络拉取复用）
    df = get_kline_data(code, rcfg, max_count=count)
    if df is not None and not df.empty:
        df = sc._clean_ohlcv(df)  # 与段2同源清洗：剔除 NaN/Inf/≤0 行，经典指标口径对齐 L2

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
    out["l2"] = sc.refine_one({"code": code, "market": market}, rcfg, bench, sc.ScreenParams())["l2"]
    return out


def cmd_signals(args, config) -> dict:
    ema_period = config.getint("CONFIG", "EMA_PERIOD", fallback=DEFAULT_EMA_PERIOD)
    db_paths = isvc.get_db_paths(config)
    bench_cache: dict = {}
    signals = []
    for i, code in enumerate(args.codes, 1):
        print(f"[signals {i}/{len(args.codes)}] {code}", file=sys.stderr)
        # 单层逐只边界：任一只异常只记错并继续，不拖垮整批（与 sepa_screener.run_l2 同口径）
        try:
            signals.append(_signals_one(code, config, bench_cache, db_paths, ema_period, args.count))
        except Exception as e:  # noqa: BLE001
            signals.append({"code": code, "error": f"信号计算异常: {e}"})
    return {"count": len(signals), "signals": signals}


def cmd_web(args, config) -> None:
    """启动 gui/backend/api.py 的 Web 服务（页面 + /api/* 接口），前台运行至中断。
    缺 --port 时由 api.py 自动选（8001 起，占用递增）。"""
    import subprocess
    api_py = _ROOT / "gui" / "backend" / "api.py"
    if not api_py.exists():
        sys.exit(f"未找到 Web 服务脚本: {api_py}")
    cmd = [sys.executable, str(api_py), "--config", str(Path(args.config).resolve())]
    if args.port:
        cmd += ["--port", str(args.port)]
    print(f"启动 Web 服务（Ctrl-C 停止）: {' '.join(cmd)}", file=sys.stderr)
    sys.exit(subprocess.run(cmd).returncode)


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
                                 description="futu_trends 统一功能导出 CLI（当前：market-sense）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pk = sub.add_parser("kline", parents=[common], help="读 OHLCV")
    pk.add_argument("--code", required=True, help="如 US.AAPL / HK.00700 / SH.600519")
    pk.add_argument("--count", type=int, default=DEFAULT_COUNT, help="K 线根数")
    pk.add_argument("--ktype", help="覆盖 K 线周期，如 K_DAY/K_60M（缺省用 config）")
    pk.set_defaults(func=cmd_kline)

    ps = sub.add_parser("screen", parents=[common], help="条件选股 L1+L1.5+L2")
    ps.add_argument("--market", required=True, choices=list(sc.MARKETS), help="US / HK / A")
    ps.add_argument("--no-refine", action="store_true", help="只跑 L1+L1.5（0 配额，不进段2）")
    ps.set_defaults(func=cmd_screen)

    pg = sub.add_parser("signals", parents=[common], help="单/多只指标信号 + detect + L2")
    pg.add_argument("--code", dest="codes", action="append", required=True,
                    help="可重复传多只，如 --code US.AAPL --code US.NVDA")
    pg.add_argument("--count", type=int, default=DEFAULT_COUNT, help="K 线根数")
    pg.set_defaults(func=cmd_signals)

    pw = sub.add_parser("web", parents=[common], help="启动 Web UI（页面 + /api/* 接口）")
    pw.add_argument("--port", type=int, help="Web 服务端口（缺省由 api 自动选，8001 起）")
    pw.set_defaults(func=cmd_web)
    return ap


def main():
    args = _build_parser().parse_args()
    config = _load_config(args.config)
    result = args.func(args, config)
    _emit(result, args.out, args.pretty)


if __name__ == "__main__":
    main()
