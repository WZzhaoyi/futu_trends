"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "..");
const configInput = process.env.CSI_FLOW_CONFIG;
const runtimeInput = process.env.CSI_FLOW_RUNTIME_DIR;
const symbol = (process.env.CSI_FLOW_SYMBOL || "SH.000902").toUpperCase();
const initialPosition = process.env.CSI_FLOW_INITIAL_POSITION || "flat";
const entryDate = process.env.CSI_FLOW_ENTRY_DATE;
const notificationMode = process.env.CSI_FLOW_NOTIFICATION_MODE || "position-aware";
const windowMonths = process.env.CSI_FLOW_WINDOW_MONTHS || "9";
const t1SellMode = process.env.CSI_FLOW_T1_SELL_MODE || "defer-next-open";
const pythonInterpreter = process.env.CSI_FLOW_PYTHON
  || (process.platform === "win32" ? "python" : "python3");

if (!configInput) {
  throw new Error("缺少 CSI_FLOW_CONFIG");
}
if (!runtimeInput || !path.isAbsolute(runtimeInput)) {
  throw new Error("CSI_FLOW_RUNTIME_DIR 必须是绝对路径");
}
if (!["flat", "long"].includes(initialPosition)) {
  throw new Error("CSI_FLOW_INITIAL_POSITION 只能是 flat 或 long");
}
if (initialPosition === "long" && !entryDate) {
  throw new Error("初始多头状态必须提供 CSI_FLOW_ENTRY_DATE");
}
if (!["position-aware", "position-independent"].includes(notificationMode)) {
  throw new Error("CSI_FLOW_NOTIFICATION_MODE 无效");
}
if (!/^\d+$/.test(windowMonths) || Number(windowMonths) < 1) {
  throw new Error("CSI_FLOW_WINDOW_MONTHS 必须是正整数");
}
if (!["defer-next-open", "ignore-same-day"].includes(t1SellMode)) {
  throw new Error("CSI_FLOW_T1_SELL_MODE 无效");
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
  .update(`${identityConfig}\0${identityRuntime}\0${symbol}`)
  .digest("hex")
  .slice(0, 8);
const instanceName = `futu-csi-flow-${digest}`;
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
  "--notification-mode", notificationMode,
  "--window-months", windowMonths,
  "--t1-sell-mode", t1SellMode,
];
if (entryDate) {
  args.push("--entry-date", entryDate);
}

fs.mkdirSync(logsDir, { recursive: true });

module.exports = {
  apps: [{
    name: instanceName,
    cwd: projectRoot,
    script: path.join(projectRoot, "market_analysis", "csi_flow_timing.py"),
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
