"use strict";

// PM2 配置：中证流通择时在线信号（csi_flow_timing_m3.py）。
// 必填：CSI_FLOW_CONFIG（config.ini）、CSI_FLOW_RUNTIME_DIR（绝对路径，每个预设各用一个）。
// CSI_FLOW_PRESET 选择权重预设 m3（默认）、m1 或 m2；同时运行多个预设时各起一个实例。
// 初始持仓：CSI_FLOW_INITIAL_LAYERS（各启用层 0/1，逗号分隔）配合 CSI_FLOW_ENTRY_DATE 与 CSI_FLOW_HELD_ETF。
// CSI_FLOW_NO_SHEET=1 时不写共享表。
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "..");
const configInput = process.env.CSI_FLOW_CONFIG;
const runtimeInput = process.env.CSI_FLOW_RUNTIME_DIR;
const preset = process.env.CSI_FLOW_PRESET || "m3";
const initialLayers = process.env.CSI_FLOW_INITIAL_LAYERS;
const entryDate = process.env.CSI_FLOW_ENTRY_DATE;
const heldEtf = process.env.CSI_FLOW_HELD_ETF;
const noSheet = process.env.CSI_FLOW_NO_SHEET === "1";
const pythonInterpreter = process.env.CSI_FLOW_PYTHON
  || (process.platform === "win32" ? "python" : "python3");

if (!configInput) {
  throw new Error("缺少 CSI_FLOW_CONFIG");
}
if (!runtimeInput || !path.isAbsolute(runtimeInput)) {
  throw new Error("CSI_FLOW_RUNTIME_DIR 必须是绝对路径");
}
if (!["m3", "m1", "m2"].includes(preset)) {
  throw new Error("CSI_FLOW_PRESET 只能是 m3、m1 或 m2");
}
const anyHeld = Boolean(initialLayers) && initialLayers.split(",").includes("1");
if (anyHeld && (!entryDate || !heldEtf)) {
  throw new Error("初始持仓需要 CSI_FLOW_ENTRY_DATE 与 CSI_FLOW_HELD_ETF");
}

const configPath = path.resolve(projectRoot, configInput);
const runtimeDir = path.resolve(runtimeInput);
if (!fs.existsSync(configPath)) {
  throw new Error(`配置文件不存在: ${configPath}`);
}
fs.mkdirSync(runtimeDir, { recursive: true });

const identityConfig = process.platform === "win32" ? configPath.toLowerCase() : configPath;
const identityRuntime = process.platform === "win32" ? runtimeDir.toLowerCase() : runtimeDir;
const digest = crypto.createHash("sha256")
  .update(`${identityConfig}\0${identityRuntime}\0${preset}`)
  .digest("hex")
  .slice(0, 8);
const instanceName = `futu-csi-flow-${preset}-${digest}`;
const logsDir = path.join(projectRoot, "logs");
const pythonPath = [projectRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
const args = [
  "live",
  "--config", configPath,
  "--runtime-dir", runtimeDir,
  "--preset", preset,
];
if (initialLayers) {
  args.push("--initial-layers", initialLayers);
}
if (anyHeld) {
  args.push("--entry-date", entryDate, "--held-etf", heldEtf);
}
if (noSheet) {
  args.push("--no-sheet");
}

fs.mkdirSync(logsDir, { recursive: true });

module.exports = {
  apps: [{
    name: instanceName,
    cwd: projectRoot,
    script: path.join(projectRoot, "market_analysis", "csi_flow_timing_m3.py"),
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
      MALLOC_ARENA_MAX: "2",
    },
  }],
};
