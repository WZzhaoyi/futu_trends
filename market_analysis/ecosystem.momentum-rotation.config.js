"use strict";

// momentum-rotation 触发时机由 cron 表达式保证，宿主需北京时区（UTC+8，无 DST）：
// 系统时区偏移必须为 +480 分钟，否则直接报错（设置 TZ=Asia/Shanghai 后
// `pm2 kill && TZ=Asia/Shanghai pm2 resurrect` 重启守护进程）。
//
//   CN: 北京 15:30 每天（周一~周五）——15:00 收盘后 30 分钟，日K 必齐
//   US: 北京 05:30 每天（周一~周六）——覆盖美东冬令时 16:30 / 夏令时 17:30；
//       周六触发覆盖纽约周五收盘；周末/假期由脚本交易日检查跳过（发 IDLE，不通知）
//
// autorestart: false —— 脚本评估完即退出，PM2 保持停止直到下次 cron_restart 拉起；
// 两个 app 共享 runtime 目录，但各自持有分市场的状态文件和锁。

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "..");
const configInput = process.env.MOMENTUM_ROTATION_CONFIG;
const runtimeInput = process.env.MOMENTUM_ROTATION_RUNTIME_DIR;
const mode = process.env.MOMENTUM_ROTATION_MODE || "live";
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
if (mode !== "live") {
  throw new Error("MOMENTUM_ROTATION_MODE 只能是 live（cron_restart 定时触发）");
}
const hostOffsetMinutes = -new Date().getTimezoneOffset();
if (hostOffsetMinutes !== 480) {
  throw new Error(
    `cron 表达式假定北京时区（UTC+8），当前系统时区偏移 ${hostOffsetMinutes} 分钟；` +
    "请设置 TZ=Asia/Shanghai 并重启 PM2 守护进程：pm2 kill && TZ=Asia/Shanghai pm2 resurrect"
  );
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
const logsDir = path.join(projectRoot, "logs");
const pythonPath = [projectRoot, process.env.PYTHONPATH]
  .filter(Boolean)
  .join(path.delimiter);

fs.mkdirSync(logsDir, { recursive: true });

const markets = [
  {
    name: "CN",
    cron: "30 15 * * 1-5", // 北京 15:30 周一~周五（CN 15:00 收盘 + 30 分钟）
  },
  {
    name: "US",
    cron: "30 5 * * 1-6", // 北京 05:30 周一~周六（覆盖美东冬/夏令时收盘；周六覆盖周五纽约）
  },
];

const apps = markets.map((market) => {
  const digest = crypto.createHash("sha256")
    .update(`${identityConfig}\0${identityRuntime}\0${market.name}`)
    .digest("hex")
    .slice(0, 8);
  const instanceName = `futu-momentum-rotation-${market.name.toLowerCase()}-${digest}`;
  return {
    name: instanceName,
    cron_restart: market.cron,
    autorestart: false,
    cwd: projectRoot,
    script: path.join(projectRoot, "market_analysis", "momentum_rotation_strategy.py"),
    interpreter: pythonInterpreter,
    interpreter_args: ["-u"],
    args: [
      "live",
      "--markets", market.name,
      "--config", configPath,
      "--runtime-dir", runtimeDir,
    ],

    exec_mode: "fork",
    instances: 1,
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
  };
});

module.exports = { apps };
