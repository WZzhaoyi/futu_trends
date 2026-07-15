#!/usr/bin/env node
/*
 * 条件单 PM2 管理入口。
 *
 * 使用前：
 *   conda activate futu_trends
 *   npm install --global pm2
 *
 * 命令：
 *   node order-engine-pm2.js start [config.ini]
 *   node order-engine-pm2.js restart [config.ini]
 *   node order-engine-pm2.js stop [config.ini]
 *   node order-engine-pm2.js delete [config.ini]
 *   node order-engine-pm2.js logs [config.ini]
 *   node order-engine-pm2.js status [config.ini]
 *   node order-engine-pm2.js save
 *
 * config.ini 默认为 env/trade.ini；不同配置文件对应不同 PM2 实例。
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { execFileSync, spawnSync } = require("node:child_process");

const projectRoot = __dirname;
const ecosystemPath = path.join(projectRoot, "order_engine", "ecosystem.order-engine.config.js");
const command = process.argv[2] || "help";
const configArgument = process.argv[3];

function usage(exitCode = 0) {
  console.log(`用法:
  node order-engine-pm2.js start [config.ini]
  node order-engine-pm2.js restart [config.ini]
  node order-engine-pm2.js stop [config.ini]
  node order-engine-pm2.js delete [config.ini]
  node order-engine-pm2.js logs [config.ini]
  node order-engine-pm2.js status [config.ini]
  node order-engine-pm2.js save

config.ini 默认为 env/trade.ini；status 不指定配置时列出全部 PM2 进程。`);
  process.exit(exitCode);
}

function resolveConfig(input) {
  return path.resolve(projectRoot, input || path.join("env", "trade.ini"));
}

function loadInstance(configPath) {
  process.env.ORDER_ENGINE_CONFIG = configPath;
  delete require.cache[require.resolve(ecosystemPath)];
  const ecosystem = require(ecosystemPath);
  return ecosystem.apps[0].name;
}

function resolvePm2Invocation(args) {
  if (process.platform !== "win32") {
    return { executable: process.env.PM2_BIN || "pm2", args };
  }

  const explicit = process.env.PM2_BIN;
  if (explicit && !explicit.toLowerCase().endsWith(".cmd")) {
    return { executable: explicit, args };
  }

  try {
    const commandFile = explicit || execFileSync("where.exe", ["pm2.cmd"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).split(/\r?\n/).map((line) => line.trim()).find(Boolean);
    const pm2Cli = path.join(path.dirname(commandFile), "node_modules", "pm2", "bin", "pm2");
    if (fs.existsSync(pm2Cli)) {
      return { executable: process.execPath, args: [pm2Cli, ...args] };
    }
  } catch (_error) {
    // 使用下方统一回退。
  }
  return { executable: "pm2.exe", args };
}

function runPm2(args, extraEnv = {}) {
  const invocation = resolvePm2Invocation(args);
  const result = spawnSync(invocation.executable, invocation.args, {
    cwd: projectRoot,
    env: { ...process.env, ...extraEnv },
    stdio: "inherit",
  });
  if (result.error) {
    if (result.error.code === "ENOENT") {
      console.error("未找到 PM2。请先执行 npm install --global pm2");
    } else {
      console.error(`PM2 执行失败: ${result.error.message}`);
    }
    process.exit(1);
  }
  process.exit(result.status ?? 1);
}

if (["help", "-h", "--help"].includes(command)) {
  usage();
}
if (command === "save") {
  runPm2(["save"]);
}
if (command === "status" && !configArgument) {
  runPm2(["status"]);
}
if (!["start", "restart", "stop", "delete", "logs", "status"].includes(command)) {
  console.error(`不支持的操作: ${command}`);
  usage(2);
}

const configPath = resolveConfig(configArgument);
if (!fs.existsSync(configPath)) {
  console.error(`配置文件不存在: ${configPath}`);
  process.exit(2);
}
const instanceName = loadInstance(configPath);
const pm2Env = {
  ORDER_ENGINE_CONFIG: configPath,
};

if (command === "start") {
  runPm2(["start", ecosystemPath, "--only", instanceName, "--update-env"], pm2Env);
}
if (command === "restart") {
  runPm2(["restart", instanceName, "--update-env"], pm2Env);
}
if (command === "logs") {
  runPm2(["logs", instanceName, "--lines", "100"], pm2Env);
}
if (command === "status") {
  runPm2(["describe", instanceName], pm2Env);
}
runPm2([command, instanceName], pm2Env);
