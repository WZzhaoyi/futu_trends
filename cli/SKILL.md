---
name: futu-trends-cli
description: >
  futu_trends 统一功能导出与本地进程管理 CLI 的调用契约。行情命令只读并输出严格 JSON 到 stdout。
  当需要读取股票 K 线、做条件选股（SEPA/quality/growth_value/deep_value）、或计算单只标的的技术指标与
  趋势信号（MACD/KD/RSI/trend-template/RS/VCP），以及管理 order-engine / signal-api /
  csi-flow / etf-premium / momentum-rotation PM2 进程时调用。
---

# futu-trends-cli

futu_trends 项目对外的统一命令入口。`kline` / `screen` / `signals` 为只读 JSON 命令；
`web` 为前台服务命令；`pm2` 为本地进程状态管理命令。只有 JSON 命令承诺
**严格 JSON（无 NaN/Inf）到 stdout**，进度与告警走 stderr。

## 调用方式

```bash
# 子进程 / agent 推荐：脚本绝对路径（任意 CWD 可调，无需设 PYTHONPATH）
<ENV_PYTHON> <REPO>/cli/main.py <command> [args] --config <ABS_CONFIG> [--out <file>] [--pretty]
# 交互可选：模块入口（须在 <REPO> 下运行，或设 PYTHONPATH=<REPO>）
<ENV_PYTHON> -m cli <command> [args] --config <ABS_CONFIG>
```

PM2 管理命令的配置参数属于具体服务子命令：

```bash
<PYTHON> <REPO>/cli/main.py pm2 order-engine <ACTION> --config <ABS_CONFIG>
<PYTHON> <REPO>/cli/main.py pm2 signal-api <ACTION> --config <ABS_CONFIG> [--port 8001]
<PYTHON> <REPO>/cli/main.py pm2 csi-flow <ACTION> --config <ABS_CONFIG> --runtime-dir <ABS_RUNTIME>
<PYTHON> <REPO>/cli/main.py pm2 etf-premium <ACTION> --config <ABS_CONFIG> --runtime-dir <ABS_RUNTIME>
<PYTHON> <REPO>/cli/main.py pm2 momentum-rotation <ACTION> --mode live \
  --config <ABS_CONFIG> --runtime-dir <ABS_RUNTIME>
<PYTHON> <REPO>/cli/main.py pm2 save
```

`pm2` 分派只使用 Python 标准库，不要求调用它的 Python 环境安装 futu/pandas/scipy；
被管理进程的解释器由 ecosystem 决定，可通过 `ORDER_ENGINE_PYTHON=/path/to/python` 或
`SIGNAL_API_PYTHON=/path/to/python`、`CSI_FLOW_PYTHON=/path/to/python`、
`ETF_PREMIUM_PYTHON=/path/to/python`、`MOMENTUM_ROTATION_PYTHON=/path/to/python` 覆盖。

占位符（由部署方按本地环境替换，**不要硬编码进代码**）：
- `<ENV_PYTHON>`：装好依赖的 python 解释器绝对路径（含 futu / pandas / yfinance 的环境）。
- `<REPO>`：futu_trends 仓库根绝对路径。
- `<ABS_CONFIG>`：调用方自备的配置文件绝对路径（见下）。**必填、无默认**，缺失即报错；
  **须置于子命令之后**（如 `screen --market US --config <ABS_CONFIG>`）。始终传绝对路径，CLI 已 CWD 无关。
- `--out <file>`：写入文件而非 stdout；`--pretty`：缩进美化。
- **退出码**：成功 `0`；缺 `--config` `2`；配置不可读/缺 `[CONFIG]` 段 `1`；OpenD 不可达（仅 `screen`）`1`；
  `pm2` 本地参数/配置错误 `2`，PM2 执行失败 `1`，其他情况透传 PM2 退出码。

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
scr = call_cli("screen", "--market", "US", "--strategy", "sepa")
```

> ENV_PYTHON / REPO / ABS_CONFIG 由调用方从配置或环境变量注入，不要写死本地路径。

### 子 agent 权限边界

行情子命令只读，不下单、不改自选、不写交易。`pm2 start/restart/stop/delete/save` 会改变本地进程状态，
不应与只读行情调用共用无条件白名单；`logs/status` 虽只读，也可能暴露运行日志和部署路径。

```
<ENV_PYTHON> <REPO>/cli/main.py kline *
<ENV_PYTHON> <REPO>/cli/main.py screen *
<ENV_PYTHON> <REPO>/cli/main.py signals *
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
| `MACD_PARAMS_DB` / `KD_PARAMS_DB` / `RSI_PARAMS_DB` / `SR_PARAMS_DB` | ParamsDB 路径，供 `signals` 取最优参数 + detect | 否 |

> `screen` 默认只跑 OpenD 服务端选股 + snapshot 富集；传 `--refine` 后，L2 走 **yfinance**。
> `signals` 的趋势 L2 走 **yfinance**。`screen` 的 L1/snapshot 需要 OpenD。

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
MACD_PARAMS_DB=<DB_URI>        ; 四者均可选，如 sqlite:///<ABS_DB_PATH>
KD_PARAMS_DB=<DB_URI>
RSI_PARAMS_DB=<DB_URI>
SR_PARAMS_DB=<DB_URI>
```

- `[CONFIG]` 段头必须存在（configparser 要求）。
- `PROXY` 仅在取 yahoo 数据需代理时填（US `^GSPC` / A 股 `000510.SS` / 各标的 yfinance 均经它）。

**校验与 fail-fast（不回退默认、不静默带病启动）**：
- `kline`/`screen`/`signals`/`web`：缺 `--config`、文件不可读、或缺 `[CONFIG]` 段 → 立即报错退出（非 0）。
- `pm2 order-engine` / `pm2 signal-api` / `pm2 csi-flow` / `pm2 etf-premium` /
  `pm2 momentum-rotation`：
  缺 `--config` 或文件不存在 → 立即退出；`pm2 save` 不需要配置。
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

### 2) `screen` — 条件选股（策略条件 + L1 服务端首筛 + snapshot 排序 + 可选 L2）
```bash
screen --market US|HK|A [--strategy sepa|pr|growth_value|quality|deep_value] [--limit N] [--refine]
```
- `--strategy`：默认 `sepa`；也可选 `pr`、`growth_value`、`quality`、`deep_value`。
- `--limit`：按 `snapshot_score` 排序后只返回前 N 只。
- `--no-snapshot`：只跑 `get_stock_filter`，不做 snapshot 富集/排序。
- `--refine`：对候选运行 yfinance L2 精算；**定义了 L2 门槛的策略会在此步直接按门槛过滤**：
  - `quality` / `growth_value`：Piotroski 式质量分 ≥ 4；
  - `deep_value`：剔除报表币种≠交易币种的美股中概/ADR，并仅保留 `市值 < NCAV`(流动资产−总负债) 的 Graham 烟蒂；
  - `pr`：无 L2 门槛，仅附注不过滤；
  - `sepa`：无 L2 门槛，仅注释不过滤。
  仅精算前 `--refine-limit` 只，未精算的会被剔除（打 warning），需要全量请调大该值。
- `--refine-limit`：最多精算前多少只，默认 30；不截断最终返回列表。
- `--refine-sleep`：yfinance 单只间隔秒数，默认 1.2。
```json
{ "market": "HK", "strategy": "sepa", "l1_count": 26,
  "snapshot_enriched": true, "l2_refined": 0, "returned": 26,
  "candidates": [
    { "code": "HK.00981", "name": "中芯国际", "market": "HK",
      "market_val": 1.5e11, "eps_growth_rate": 42.8,
      "sum_of_business_growth": 63.1,
      "snapshot": { "turnover": 1.55e10, "turnover_rate": 3.38, "volume_ratio": 2.24 },
      "snapshot_metrics": { "turnover": 1.55e10, "snapshot_score": 1.55e10 },
      "l2": { /* 见下，只有 --refine 时有此字段 */ } }, ... ] }
```
候选按 `snapshot_score` **降序**。`sepa` 的 `snapshot_score` 是成交额；基本面策略的
`snapshot_score` 是各策略脚本定义的估值/质量/流动性综合分。
`--market A` 会合并沪深两市服务端筛选结果。

#### 策略条件速览（L1 服务端筛选；财务字段均为年报口径）

- **sepa** — Minervini 趋势模板：价 > EMA50 > EMA150 > EMA200（日K）；
  距52周低点 ≥ +30%、距52周高点 ≥ -30%；市值 ≥ 150亿；
  EPS 增速 ≥ 20%、营收增速 ≥ 15%。排序=成交额。
- **pr** — 市赚率 PR=PE/ROE/100，PR≤0.5（**筛选全部在 L1 完成**）：市值 ≥ 100亿（本位币）、PB>0、PE_TTM∈(0,30]；
  ROE≥8%、ROA_TTM≥1%、净利润>0、经营现金流TTM>0、资产负债率≤65%；
  权益乘数∈[1,4]（净资产≥总资产25%，L1 剔除高杠杆/低基数的假高 ROE，银行/保险等多在此出局）；
  Python 精确算 PR≤0.5；`cash_coverage`(经营现金流TTM/净利润) 作为一次性收益提示字段（不硬过滤）。
  停牌股由快照阶段剔除。排序=PR 主导+账面折价+股息+流动性。
- **growth_value** — 成长价值：市值下限按市场 US ≥ $20亿 / HK ≥ HK$50亿 / A ≥ ¥50亿；PE_TTM ∈ (0, 35]、PB ∈ (0, 5]；
  ROE ≥ 8%；营收增速 ≥ 0、净利增速 ≥ 0；经营现金流 TTM ≥ 0；资产负债率 ≤ 60%。
  排序=ROE+盈利收益率+账面折价+股息+流动性加权。
- **quality** — 质量：市值下限按市场 US ≥ $20亿 / HK ≥ HK$50亿 / A ≥ ¥50亿；ROE ≥ 8%、ROA_TTM ≥ 1%；净利润 ≥ 0；
  经营现金流 TTM ≥ 0；毛利率 ≥ 0；资产负债率 ≤ 60%。
  排序=ROE 主导的质量/收益/流动性加权。
- **deep_value** — 深度价值/烟蒂：市值 ≥ 10亿；PE_TTM ∈ (0, 13]、PB ∈ (0, 1]；
  净利润 ≥ 0；资产负债率 ≤ 50%；流动比率 ≥ 1.5。排序=账面折价主导；
  `--refine` 精算 NCAV=流动资产−总负债、净现金等，并剔除中概/ADR、只留 `市值 < NCAV`。

阈值常量定义在 `fundamental_analysis/<strategy>_screener.py` 头部，调整只改常量。

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
web [--port <PORT>] [--forever]
```
子进程前台启动 gui/backend/api.py 的页面 + `/api/*` 接口，运行至中断（非 stdout-JSON）。
面向人工/浏览器，agent 自动化一般不调用此命令。端口默认 8001，被占用时报错退出
（`--port` 指定其他端口；启动打印 `API_PORT=`）；`--forever` 崩溃自动重启（正常退出/Ctrl-C 不重启）。

### 5) `pm2` — 管理长期运行进程（**非 JSON，会改变本地进程状态**）

```bash
pm2 order-engine start|restart|stop|delete|logs|status --config <ABS_CONFIG>
pm2 signal-api start|restart|stop|delete|logs|status --config <ABS_CONFIG> [--port <PORT>]
pm2 csi-flow start|restart|stop|delete|logs|status --config <ABS_CONFIG> --runtime-dir <ABS_RUNTIME>
pm2 etf-premium start|restart|stop|delete|logs|status --config <ABS_CONFIG> --runtime-dir <ABS_RUNTIME>
pm2 momentum-rotation start|restart|stop|delete|logs|status --mode live \
  --config <ABS_CONFIG> --runtime-dir <ABS_RUNTIME>
pm2 save
```

- 五类服务都必须显式传 `--config`，无默认配置；文件不存在时退出码为 `2`。
- `signal-api` 端口默认 `8001`，配置绝对路径与端口共同确定 PM2 实例名。
- `order-engine` 由配置绝对路径确定 PM2 实例名。
- `csi-flow` 还必须传绝对 `--runtime-dir`；`live` 在其中自动维护行情、阈值、状态和实例锁。
- `etf-premium` 也必须传绝对 `--runtime-dir`；默认标的 `159941`。交易信号只使用 T-1
  单位净值溢价，Futu IOPV 溢价只写入快照和通知作为盘中提示。
- `momentum-rotation` 也必须传绝对 `--runtime-dir`：PM2 `cron_restart` 定时触发
  （CN 北京 15:30 周一~五、US 北京 05:30 周一~六，`autorestart: false` 跑完即退），
  `pm2 momentum-rotation start` 一次管理 CN/US 两个实例
  （系统时区须为北京时区 UTC+8，否则报错并提示 `pm2 kill && TZ=Asia/Shanghai pm2 resurrect`）。
- `ORDER_ENGINE_PYTHON` / `SIGNAL_API_PYTHON` / `CSI_FLOW_PYTHON` /
  `ETF_PREMIUM_PYTHON` / `MOMENTUM_ROTATION_PYTHON` 可分别指定解释器；否则优先使用当前
  `CONDA_PREFIX`。
- `start` 与 `restart` 都重新加载 ecosystem 文件并传 `--update-env`，确保参数和环境同时更新；
  日志默认显示最后 100 行。
- PM2 只做进程级 liveness：`api.py` 退出后重启。`/` 不参与自动探活，OpenD/MongoDB/Longbridge
  等运行期依赖也不作为重启条件，避免依赖故障引发重启风暴。
- `save` 保存当前 PM2 进程列表，供 PM2 startup/resurrect 使用。

示例：

```bash
<PYTHON> <REPO>/cli/main.py pm2 order-engine start --config <ABS_ORDER_CONFIG>
SIGNAL_API_PYTHON=<API_PYTHON> <PYTHON> <REPO>/cli/main.py pm2 signal-api start \
  --config <ABS_SIGNAL_CONFIG> --port 8001
<PYTHON> <REPO>/cli/main.py pm2 signal-api status --config <ABS_SIGNAL_CONFIG> --port 8001
ETF_PREMIUM_PYTHON=<ENV_PYTHON> <PYTHON> <REPO>/cli/main.py pm2 etf-premium start \
  --config <ABS_CONFIG> --runtime-dir <ABS_RUNTIME>
MOMENTUM_ROTATION_PYTHON=<ENV_PYTHON> <PYTHON> <REPO>/cli/main.py pm2 momentum-rotation start \
  --mode live --config <ABS_CONFIG> --runtime-dir <ABS_RUNTIME>
<PYTHON> <REPO>/cli/main.py pm2 save
```

## L2 信号块

`signals` 的 L2 是趋势模板/RS/VCP 信号；`screen --refine` 的 L2 则由策略决定：
`deep_value` 输出现金/负债精算，`growth_value` / `quality` 默认输出 yfinance 质量与
Piotroski-like 精算。

### `signals` L2 趋势信号

```json
"l2": {
  "ok": true, "close": 440.2, "bars": 400, "bars_dropped": 0,
  "trend_template_pass": true,
  "ema200_slope_pct": 6.3, "ema200_uptrend": true,
  "dist_from_low52_pct": 41.8, "dist_from_high52_pct": -11.5,
  "rs_proxy": { "approx": true, "note": "...", "excess_3m": -15.7,
                "excess_6m": -20.4, "excess_12m": 19.4, "low_confidence": false },
  "vcp": { "heuristic": true, "num_contractions": 4,
           "contraction_depths_pct": [12.5,11.5,11.3,8.5],
           "depths_decreasing": true, "volume_contracting": true, "pivot": 484.8 } }
```
数据不足/取数失败：`"l2": {"ok": false, "note": "清洗后有效K线不足(...)"}`。

### `screen --refine` L2 财报精算示例

```json
"l2": {
  "ok": true,
  "yf_code": "1765.HK",
  "cash_and_equivalents": 2770782000.0,
  "total_liabilities": 12415062000.0,
  "net_cash": -9644280000.0,
  "cash_to_market_cap": 2.51,
  "condition_cash_minus_liabilities_gt_market_cap": false
}
```

财务表为空/字段缺失：`"l2": {"ok": false, "note": "income_stmt or balance_sheet is empty"}`。

**口径约束（策略侧不得当真值）**：
- `rs_proxy` 标 `approx: true`——是 vs 单一基准指数的超额收益，非 IBD 百分位；
  `low_confidence: true` 表示删行较多、RS 可能漂移。
- `vcp` 标 `heuristic: true`——结构化测量，非精确 VCP 判定。
- `bars_dropped` 为清洗掉的非法行数（NaN/Inf/≤0）；`trend_template_pass` 需
  收盘价>EMA50>EMA150>EMA200 且 MA200 上行且在 52 周高低带内。

## 不变量（契约保证）
1. `kline`/`screen`/`signals` 的 stdout 永远是单个严格 JSON 对象（无日志混入、无 NaN/Inf）。
2. `kline`/`signals` 不需要 OpenD；`screen` 的 L1/snapshot 需要，OpenD 不可达时 stderr 报错 + 退出码 1。
3. 行情命令只读：不下单、不改自选、不写交易；`pm2` 仅管理本机进程，不执行交易业务。
4. `--config` 传绝对路径即 CWD 无关；缓存写入 `CACHE_DIR`（绝对路径）。
5. `pm2` 不输出 JSON，stdout/stderr 直接透传 PM2，退出码也按上述规则透传。
