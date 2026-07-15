"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "..");
const configInput = process.env.ORDER_ENGINE_CONFIG || path.join("env", "trade.ini");
const configPath = path.resolve(projectRoot, configInput);
const identityPath = process.platform === "win32" ? configPath.toLowerCase() : configPath;
const configSlug = path.basename(configPath, path.extname(configPath))
  .replace(/[^a-zA-Z0-9_-]+/g, "-")
  .replace(/^-+|-+$/g, "") || "config";
const configHash = crypto.createHash("sha256").update(identityPath).digest("hex").slice(0, 8);
const instanceName = `futu-order-${configSlug}-${configHash}`;
const logsDir = path.join(projectRoot, "logs");
const pythonPath = [projectRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);

if (!fs.existsSync(configPath)) {
  throw new Error(`条件单配置文件不存在: ${configPath}`);
}
fs.mkdirSync(logsDir, { recursive: true });

module.exports = {
  apps: [{
    name: instanceName,
    cwd: projectRoot,
    script: path.join(projectRoot, "order_engine", "__main__.py"),
    interpreter: "python",
    interpreter_args: ["-u"],
    args: ["--config", configPath],

    exec_mode: "fork",
    instances: 1,
    autorestart: true,
    min_uptime: "30s",
    max_restarts: 5,
    restart_delay: 5000,
    kill_timeout: 20000,
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
