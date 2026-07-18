"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "..", "..");
const configInput = process.env.SIGNAL_API_CONFIG;
if (!configInput) {
  throw new Error("缺少 SIGNAL_API_CONFIG：Signal API 配置文件必须显式指定");
}
const configPath = path.resolve(projectRoot, configInput);
const portInput = process.env.SIGNAL_API_PORT || "8001";
const pythonInterpreter = process.env.SIGNAL_API_PYTHON
  || (process.platform === "win32" ? "python" : "python3");

if (!/^\d+$/.test(portInput) || Number(portInput) < 1 || Number(portInput) > 65535) {
  throw new Error(`Signal API 端口必须为 1-65535 的整数: ${portInput}`);
}

const port = String(Number(portInput));
const identityPath = process.platform === "win32" ? configPath.toLowerCase() : configPath;
const configSlug = path.basename(configPath, path.extname(configPath))
  .replace(/[^a-zA-Z0-9_-]+/g, "-")
  .replace(/^-+|-+$/g, "") || "config";
const instanceHash = crypto.createHash("sha256")
  .update(`${identityPath}\0${port}`)
  .digest("hex")
  .slice(0, 8);
const instanceName = `futu-signal-api-${configSlug}-${port}-${instanceHash}`;
const logsDir = path.join(projectRoot, "logs");
const pythonPath = [projectRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);

if (!fs.existsSync(configPath)) {
  throw new Error(`Signal API 配置文件不存在: ${configPath}`);
}
fs.mkdirSync(logsDir, { recursive: true });

module.exports = {
  apps: [{
    name: instanceName,
    cwd: projectRoot,
    script: path.join(projectRoot, "gui", "backend", "api.py"),
    interpreter: pythonInterpreter,
    interpreter_args: ["-u"],
    args: ["--config", configPath, "--port", port],

    exec_mode: "fork",
    instances: 1,
    // 这里只做进程级 liveness 守护：api.py 退出后重启。
    // OpenD 等运行期依赖由 api.py 自身降级/报错处理，不以重启 API 代替依赖恢复。
    autorestart: true,
    min_uptime: "15s",
    max_restarts: 5,
    restart_delay: 5000,
    kill_timeout: 10000,
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
