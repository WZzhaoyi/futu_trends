"use strict";

// PM2 配置：中证流通 M2 在线信号（不下单）。
// 必填：CSI_FLOW_M2_CONFIG（config.ini）、CSI_FLOW_M2_RUNTIME_DIR（绝对路径，不可与 M1 共用）。
// 与 M1 并行运行期间可设 CSI_FLOW_M2_NO_SHEET=1，不写共享表。
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "..");
const configInput = process.env.CSI_FLOW_M2_CONFIG;
const runtimeInput = process.env.CSI_FLOW_M2_RUNTIME_DIR;
const initialPosition = process.env.CSI_FLOW_M2_INITIAL_POSITION || "flat";
const entryDate = process.env.CSI_FLOW_M2_ENTRY_DATE;
const heldEtf = process.env.CSI_FLOW_M2_HELD_ETF;
const noSheet = process.env.CSI_FLOW_M2_NO_SHEET === "1";
const pythonInterpreter = process.env.CSI_FLOW_PYTHON
  || (process.platform === "win32" ? "python" : "python3");

if (!configInput) {
  throw new Error("缺少 CSI_FLOW_M2_CONFIG");
}
if (!runtimeInput || !path.isAbsolute(runtimeInput)) {
  throw new Error("CSI_FLOW_M2_RUNTIME_DIR 必须是绝对路径");
}
if (!["flat", "long"].includes(initialPosition)) {
  throw new Error("CSI_FLOW_M2_INITIAL_POSITION 只能是 flat 或 long");
}
if (initialPosition === "long" && (!entryDate || !heldEtf)) {
  throw new Error("初始多头需要 CSI_FLOW_M2_ENTRY_DATE 与 CSI_FLOW_M2_HELD_ETF");
}

const configPath = path.resolve(projectRoot, configInput);
const runtimeDir = path.resolve(runtimeInput);
if (!fs.existsSync(configPath)) {
  throw new Error(`配置文件不存在: ${configPath}`);
}
fs.mkdirSync(runtimeDir, { recursive: true });

const digest = crypto.createHash("sha256")
  .update(`${configPath}\0${runtimeDir}\0m2`)
  .digest("hex")
  .slice(0, 8);
const instanceName = `futu-csi-flow-m2-${digest}`;
const logsDir = path.join(projectRoot, "logs");
const pythonPath = [projectRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
const args = [
  "live",
  "--config", configPath,
  "--runtime-dir", runtimeDir,
  "--initial-position", initialPosition,
];
if (initialPosition === "long") {
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
    script: path.join(projectRoot, "market_analysis", "csi_flow_timing_m2.py"),
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
