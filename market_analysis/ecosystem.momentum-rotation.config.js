"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "..");
const configInput = process.env.MOMENTUM_ROTATION_CONFIG;
const runtimeInput = process.env.MOMENTUM_ROTATION_RUNTIME_DIR;
const mode = process.env.MOMENTUM_ROTATION_MODE || "live-us";
const interval = process.env.MOMENTUM_ROTATION_INTERVAL || "60";
const maxQuoteAge = process.env.MOMENTUM_ROTATION_MAX_QUOTE_AGE || "14400";
const maxErrors = process.env.MOMENTUM_ROTATION_MAX_ERRORS || "5";
const condaPython = process.env.CONDA_PREFIX
  ? (process.platform === "win32"
    ? path.join(process.env.CONDA_PREFIX, "python.exe")
    : path.join(process.env.CONDA_PREFIX, "bin", "python"))
  : null;
const pythonInterpreter = process.env.MOMENTUM_ROTATION_PYTHON
  || condaPython
  || (process.platform === "win32" ? "python" : "python3");

if (!configInput) {
  throw new Error("缺少 MOMENTUM_ROTATION_CONFIG");
}
if (!runtimeInput || !path.isAbsolute(runtimeInput)) {
  throw new Error("MOMENTUM_ROTATION_RUNTIME_DIR 必须是绝对路径");
}
if (!["live-us", "live-cn"].includes(mode)) {
  throw new Error("MOMENTUM_ROTATION_MODE 只能是 live-us 或 live-cn");
}
for (const [name, value] of Object.entries({
  MOMENTUM_ROTATION_INTERVAL: interval,
  MOMENTUM_ROTATION_MAX_QUOTE_AGE: maxQuoteAge,
  MOMENTUM_ROTATION_MAX_ERRORS: maxErrors,
})) {
  if (!Number.isFinite(Number(value)) || Number(value) <= 0) {
    throw new Error(`${name} 必须大于0`);
  }
}

const configPath = path.resolve(projectRoot, configInput);
const runtimeDir = path.resolve(runtimeInput);
if (!fs.existsSync(configPath)) {
  throw new Error(`配置文件不存在: ${configPath}`);
}
fs.mkdirSync(runtimeDir, { recursive: true });

const identityConfig = process.platform === "win32"
  ? configPath.toLowerCase()
  : configPath;
const identityRuntime = process.platform === "win32"
  ? runtimeDir.toLowerCase()
  : runtimeDir;
const digest = crypto.createHash("sha256")
  .update(`${identityConfig}\0${identityRuntime}\0${mode}`)
  .digest("hex")
  .slice(0, 8);
const instanceName = `futu-momentum-rotation-${digest}`;
const logsDir = path.join(projectRoot, "logs");
const pythonPath = [projectRoot, process.env.PYTHONPATH]
  .filter(Boolean)
  .join(path.delimiter);

fs.mkdirSync(logsDir, { recursive: true });

module.exports = {
  apps: [{
    name: instanceName,
    cwd: projectRoot,
    script: path.join(projectRoot, "market_analysis", "momentum_rotation_strategy.py"),
    interpreter: pythonInterpreter,
    interpreter_args: ["-u"],
    args: [
      mode,
      "--config", configPath,
      "--runtime-dir", runtimeDir,
      "--interval", interval,
      "--max-quote-age", maxQuoteAge,
      "--max-errors", maxErrors,
    ],

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
