"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "..");
const configInput = process.env.ETF_PREMIUM_CONFIG;
const runtimeInput = process.env.ETF_PREMIUM_RUNTIME_DIR;
const symbol = process.env.ETF_PREMIUM_SYMBOL || "159941";
const initialPosition = process.env.ETF_PREMIUM_INITIAL_POSITION || "base";
const interval = process.env.ETF_PREMIUM_INTERVAL || "60";
const navRefresh = process.env.ETF_PREMIUM_NAV_REFRESH || "900";
const maxNavAge = process.env.ETF_PREMIUM_MAX_NAV_AGE || "14";
const maxQuoteAge = process.env.ETF_PREMIUM_MAX_QUOTE_AGE || "180";
const maxErrors = process.env.ETF_PREMIUM_MAX_ERRORS || "5";
const strategyInput = process.env.ETF_PREMIUM_STRATEGY_FILE;
const cacheInput = process.env.ETF_PREMIUM_CACHE_DIR;
const condaPython = process.env.CONDA_PREFIX
  ? (process.platform === "win32"
    ? path.join(process.env.CONDA_PREFIX, "python.exe")
    : path.join(process.env.CONDA_PREFIX, "bin", "python"))
  : null;
const pythonInterpreter = process.env.ETF_PREMIUM_PYTHON
  || condaPython
  || (process.platform === "win32" ? "python" : "python3");

if (!configInput) {
  throw new Error("缺少 ETF_PREMIUM_CONFIG");
}
if (!runtimeInput || !path.isAbsolute(runtimeInput)) {
  throw new Error("ETF_PREMIUM_RUNTIME_DIR 必须是绝对路径");
}
if (!/^\d{6}$/.test(symbol)) {
  throw new Error("ETF_PREMIUM_SYMBOL 必须是6位基金代码");
}
if (!["base", "low"].includes(initialPosition)) {
  throw new Error("ETF_PREMIUM_INITIAL_POSITION 只能是 base 或 low");
}
for (const [name, value] of Object.entries({
  ETF_PREMIUM_INTERVAL: interval,
  ETF_PREMIUM_NAV_REFRESH: navRefresh,
  ETF_PREMIUM_MAX_NAV_AGE: maxNavAge,
  ETF_PREMIUM_MAX_QUOTE_AGE: maxQuoteAge,
  ETF_PREMIUM_MAX_ERRORS: maxErrors,
})) {
  if (!Number.isFinite(Number(value)) || Number(value) <= 0) {
    throw new Error(`${name} 必须大于0`);
  }
}

const configPath = path.resolve(projectRoot, configInput);
const runtimeDir = path.resolve(runtimeInput);
const strategyPath = strategyInput
  ? path.resolve(projectRoot, strategyInput)
  : null;
const cacheDir = cacheInput ? path.resolve(projectRoot, cacheInput) : null;
if (!fs.existsSync(configPath)) {
  throw new Error(`配置文件不存在: ${configPath}`);
}
if (strategyPath && !fs.existsSync(strategyPath)) {
  throw new Error(`策略文件不存在: ${strategyPath}`);
}
fs.mkdirSync(runtimeDir, { recursive: true });

const identityConfig = process.platform === "win32"
  ? configPath.toLowerCase()
  : configPath;
const identityRuntime = process.platform === "win32"
  ? runtimeDir.toLowerCase()
  : runtimeDir;
const digest = crypto.createHash("sha256")
  .update(`${identityConfig}\0${identityRuntime}\0${symbol}`)
  .digest("hex")
  .slice(0, 8);
const instanceName = `futu-etf-premium-${digest}`;
const logsDir = path.join(projectRoot, "logs");
const pythonPath = [projectRoot, process.env.PYTHONPATH]
  .filter(Boolean)
  .join(path.delimiter);
const args = [
  "live",
  "--symbol", symbol,
  "--config", configPath,
  "--runtime-dir", runtimeDir,
  "--initial-position", initialPosition,
  "--interval", interval,
  "--nav-refresh", navRefresh,
  "--max-nav-age", maxNavAge,
  "--max-quote-age", maxQuoteAge,
  "--max-errors", maxErrors,
];
if (strategyPath) {
  args.push("--strategy-file", strategyPath);
}
if (cacheDir) {
  args.push("--cache-dir", cacheDir);
}

fs.mkdirSync(logsDir, { recursive: true });

module.exports = {
  apps: [{
    name: instanceName,
    cwd: projectRoot,
    script: path.join(projectRoot, "market_analysis", "etf_premium_rate.py"),
    interpreter: pythonInterpreter,
    interpreter_args: ["-u"],
    args,

    exec_mode: "fork",
    instances: 1,
    autorestart: true,
    min_uptime: "30s",
    max_restarts: 20,
    exp_backoff_restart_delay: 1000,
    kill_timeout: 30000,
    max_memory_restart: "300M",
    watch: false,

    time: true,
    merge_logs: true,
    out_file: path.join(logsDir, `${instanceName}.out.log`),
    error_file: path.join(logsDir, `${instanceName}.error.log`),
    env: {
      PYTHONUNBUFFERED: "1",
      PYTHONPATH: pythonPath,
    },
  }],
};
