---
name: futu-trends-cli
description: >
  futu_trends 统一功能导出 CLI 的调用契约。只读，输出严格 JSON 到 stdout。
  当需要读取股票 K 线、做条件选股（趋势模板/SEPA）、或计算单只标的的技术指标与
  趋势信号（MACD/KD/RSI/trend-template/RS/VCP）时调用。market-sense 为当前唯一功能域。
---

# futu-trends-cli

futu_trends 项目对外的统一命令入口。三个只读子命令 `kline` / `screen` / `signals`，
全部输出**严格 JSON（无 NaN/Inf）到 stdout**，进度与告警走 stderr。

## 调用方式

```bash
# 子进程 / agent 推荐：脚本绝对路径（任意 CWD 可调，无需设 PYTHONPATH）
<ENV_PYTHON> <REPO>/cli/main.py <command> [args] --config <ABS_CONFIG> [--out <file>] [--pretty]
# 交互可选：模块入口（须在 <REPO> 下运行，或设 PYTHONPATH=<REPO>）
<ENV_PYTHON> -m cli <command> [args] --config <ABS_CONFIG>
```

占位符（由部署方按本地环境替换，**不要硬编码进代码**）：
- `<ENV_PYTHON>`：装好依赖的 python 解释器绝对路径（含 futu / pandas / yfinance 的环境）。
- `<REPO>`：futu_trends 仓库根绝对路径。
- `<ABS_CONFIG>`：调用方自备的配置文件绝对路径（见下）。**必填、无默认**，缺失即报错；
  **须置于子命令之后**（如 `screen --market US --config <ABS_CONFIG>`）。始终传绝对路径，CLI 已 CWD 无关。
- `--out <file>`：写入文件而非 stdout；`--pretty`：缩进美化。
- **退出码**：成功 `0`；缺 `--config` `2`；配置不可读/缺 `[CONFIG]` 段 `1`；OpenD 不可达（仅 `screen`）`1`。

> 子进程务必用**脚本绝对路径**形态（`main.py` 自引导 sys.path，任意 CWD 可跑）；
> `-m cli` 在非仓库目录会报 `No module named cli`，不适合无环境约束的子进程调用。
> 全局选项（`--config`/`--out`/`--pretty`）只在**子命令之后**生效。

### 子进程范式（Python）

```python
import subprocess, json

def call_cli(*args) -> dict:
    p = subprocess.run(
        [ENV_PYTHON, f"{REPO}/cli/main.py", *args, "--config", ABS_CONFIG],
        capture_output=True, text=True, timeout=600,
    )
    if p.returncode != 0:           # 如 screen 遇 OpenD 不可达 → 退出码 1
        raise RuntimeError(f"cli 失败(code {p.returncode}): {p.stderr.strip()}")
    return json.loads(p.stdout)     # stdout 永远是单个严格 JSON（无 NaN/Inf/日志混入）

sig = call_cli("signals", "--code", "US.AAPL")
scr = call_cli("screen", "--market", "US")
```

> ENV_PYTHON / REPO / ABS_CONFIG 由调用方从配置或环境变量注入，不要写死本地路径。

### 子 agent 白名单

只放行这一条只读命令即可（CLI 不下单、不改自选、不写交易，攻击面极小）：

```
<ENV_PYTHON> <REPO>/cli/main.py *
```

## 配置（config.ini，`[CONFIG]` 段）

| 键 | 用途 | 必需 |
|---|---|---|
| `FUTU_HOST` / `FUTU_PORT` | OpenD 地址（默认 127.0.0.1 / 11111） | `screen` 必需 |
| `DATA_SOURCE` | 默认数据源：`yfinance`/`futu`/`akshare`/`longbridge`/`ibkr` | `kline` 用 |
| `DATA_SOURCE_{US,HK,SH,SZ}` | 分市场覆盖数据源（可选） | 否 |
| `FUTU_PUSH_TYPE` | K 线周期默认（如 `K_DAY`） | 否 |
| `PROXY` | yfinance/基准指数取数代理 | 视网络 |
| `CACHE_DIR` | K 线文件缓存目录（默认 `<REPO>/data`，自动转绝对路径） | 否 |
| `EMA_PERIOD` | `signals` 的 EMA 周期（默认 240） | 否 |
| `MACD_PARAMS_DB` / `KD_PARAMS_DB` / `RSI_PARAMS_DB` | ParamsDB 路径，供 `signals` 取最优参数 + detect | 否 |

> `screen` 段 2 与 `signals` 一律走 **yfinance**（免 futu 历史 K 线配额），不需 OpenD；
> 仅 `screen` 段 1/1.5 需要 OpenD（服务端选股 + 快照，均 0 历史 K 线配额）。

调用方自备一份精简 config（只需 `[CONFIG]` 段 + 下列键，其余项一概不用写）。
占位 `<...>` 由部署方按本地环境填写：

**(a) 仅用 `signals` / `kline`（走 yfinance，免 OpenD）**
```ini
[CONFIG]
DATA_SOURCE=yfinance
PROXY=<PROXY_URL_OR_EMPTY>     ; 主机需翻墙取 yahoo 数据时填，直连外网留空
FUTU_PUSH_TYPE=K_DAY
CACHE_DIR=<ABS_CACHE_DIR>      ; 可选；缺省写 <REPO>/data
```

**(b) 还要用 `screen`（需 futu OpenD 运行）**
```ini
[CONFIG]
FUTU_HOST=<OPEND_HOST>
FUTU_PORT=<OPEND_PORT>
DATA_SOURCE=yfinance
PROXY=<PROXY_URL_OR_EMPTY>
FUTU_PUSH_TYPE=K_DAY
```

**(c) 让 `signals` 用优化过的最优参数（可选；缺省/缺记录则回退统一默认参数）**
```ini
MACD_PARAMS_DB=<DB_URI>        ; 三者均可选，如 sqlite:///<ABS_DB_PATH>
KD_PARAMS_DB=<DB_URI>
RSI_PARAMS_DB=<DB_URI>
```

- `[CONFIG]` 段头必须存在（configparser 要求）。
- `PROXY` 仅在取 yahoo 数据需代理时填（US `^GSPC` / A 股 `000510.SS` / 各标的 yfinance 均经它）。

**校验与 fail-fast（不回退默认、不静默带病启动）**：
- 所有子命令：缺 `--config`、文件不可读、或缺 `[CONFIG]` 段 → 立即报错退出（非 0）。
- `web` 额外要求（由 api.py 校验）：`[CONFIG]` 必含 `FUTU_HOST`/`FUTU_PORT`/`DATA_SOURCE`，
  且 `FUTU_PORT`/`EMA_PERIOD`（若提供）须为整数，否则拒绝启动。

## 子命令与输出

> 下列示例只列**子命令与参数**；实际调用须前置 `<ENV_PYTHON> <REPO>/cli/main.py` 与 `--config <ABS_CONFIG>`。

### 1) `kline` — 读 OHLCV
```bash
kline --code US.AAPL --count 400 [--ktype K_DAY]
```
```json
{ "code": "US.AAPL", "ktype": "K_DAY", "count": 400,
  "bars": [ {"time": "2026-03-25 00:00:00", "open": 253.8, "high": 254.7,
             "low": 251.3, "close": 252.3, "volume": 28476700.0}, ... ] }
```
失败：`{"code": "...", "bars": [], "error": "无数据或取数失败"}`

### 2) `screen` — 条件选股（L1 服务端首筛 + L1.5 快照排序 + L2 本地精算）
```bash
screen --market US|HK|A [--no-refine]
```
- `--no-refine`：只出 L1+L1.5（0 配额，含成交额排序，不进段 2）。
```json
{ "market": "HK", "l1_count": 26, "quota_used": 0, "l2_refined": 26,
  "candidates": [
    { "code": "HK.00981", "name": "中芯国际", "market": "HK",
      "market_val": 1.5e11, "dist_from_low52": 35.3, "dist_from_high52": -21.2,
      "eps_growth": 42.8, "rev_growth": 63.1,
      "turnover": 1.55e10, "turnover_rate": 3.38, "volume_ratio": 2.24,
      "l2": { /* 见下，--no-refine 时无此字段 */ } }, ... ] }
```
候选按 `turnover`（成交额）**降序**。`quota_used` 恒为 0（L1/L1.5 不计历史 K 线配额，L2 走 yfinance）。

### 3) `signals` — 单/多只指标信号 + detect + L2
```bash
signals --code US.AAPL [--code US.NVDA ...] [--count 400]
```
```json
{ "count": 1, "signals": [
  { "code": "US.AAPL", "market": "US",
    "indicators": {
      "ema":  {"period": 240, "value": 262.9},
      "macd": {"vmacd": 17.6, "signal": 50.5, "hist": -65.7, "params_source": "best_params|default"},
      "kd":   {"k": 35.4, "d": 31.8, "oversold": 50, "overbought": 50, "params_source": "..."},
      "rsi":  {"value": 39.0, "oversold": 30, "overbought": 70, "params_source": "..."} },
    "detect": { "MACD": {"best_params": {...}, "meta_info": {...}, "performance": {...}}, ... },
    "l2": { /* 见下 */ } } ] }
```
- `params_source`：`best_params`（来自 ParamsDB）或 `default`（缺则回退统一默认参数）。
- `detect`：各指标的最优参数/元信息/绩效；ParamsDB 未配置或无记录则为 `{}`。

### 4) `web` — 启动 Web UI（**非 JSON，不属 agent 调用契约**）
```bash
web [--port <PORT>]
```
子进程前台启动 gui/backend/api.py 的页面 + `/api/*` 接口，运行至中断（非 stdout-JSON）。
面向人工/浏览器，agent 自动化一般不调用此命令。缺 `--port` 时端口自动选（8001 起，打印 `API_PORT=`）。

## L2 信号块（`screen` 候选与 `signals` 共用）

```json
"l2": {
  "ok": true, "close": 440.2, "bars": 400, "bars_dropped": 0,
  "trend_template_pass": true,
  "ma200_slope_pct": 6.3, "ma200_uptrend": true,
  "dist_from_low52_pct": 41.8, "dist_from_high52_pct": -11.5,
  "rs_proxy": { "approx": true, "note": "...", "excess_3m": -15.7,
                "excess_6m": -20.4, "excess_12m": 19.4, "low_confidence": false },
  "vcp": { "heuristic": true, "num_contractions": 4,
           "contraction_depths_pct": [12.5,11.5,11.3,8.5],
           "depths_decreasing": true, "volume_contracting": true, "pivot": 484.8 } }
```
数据不足/取数失败：`"l2": {"ok": false, "note": "清洗后有效K线不足(...)"}`

**口径约束（策略侧不得当真值）**：
- `rs_proxy` 标 `approx: true`——是 vs 单一基准指数的超额收益，非 IBD 百分位；
  `low_confidence: true` 表示删行较多、RS 可能漂移。
- `vcp` 标 `heuristic: true`——结构化测量，非精确 VCP 判定。
- `bars_dropped` 为清洗掉的非法行数（NaN/Inf/≤0）；`trend_template_pass` 需
  收盘价>EMA50>EMA150>EMA200 且 MA200 上行且在 52 周高低带内。

## 不变量（契约保证）
1. stdout 永远是单个严格 JSON 对象（无日志混入、无 NaN/Inf）。
2. `kline`/`signals` 不需要 OpenD；`screen` 需要，OpenD 不可达时 stderr 报错 + 退出码 1。
3. 只读：不下单、不改自选、不写交易。
4. `--config` 传绝对路径即 CWD 无关；缓存写入 `CACHE_DIR`（绝对路径）。
