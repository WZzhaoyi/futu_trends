# 测试运行与精简原则

使用已安装项目依赖的 conda 环境 `futu_trends`。通知测试直接导入真实依赖，仅在网络调用边界 mock；不要在模块导入时向 `sys.modules` 安装全局桩。

开发时只运行相关文件，例如：

```sh
conda run -n futu_trends python -m unittest discover -s tests -p test_native_backtest.py -v
```

最终执行完整测试并保留可复查产物（在仓库根目录运行）：

```sh
mkdir -p /tmp/futu-tests-final
FUTU_TEST_ARTIFACT_DIR=/tmp/futu-tests-final \
  conda run --no-capture-output -n futu_trends \
  python -m unittest discover -s tests -v \
  > /tmp/futu-tests-final/unittest.log 2>&1
```

检查命令退出码和日志末尾的 `OK`。`momentum-daily.csv`、`momentum-trades.json` 来自合成行情的真实网格/回测计算，可核对只建仓一次的成交记录。它们不代表连接券商的实盘 E2E；外部行情、下单及通知仍由测试替身隔离。

本次保留的关键故障判据：

- 零价会命中止损条件但必须被忽略；有效价格仍能触发。等待 tick 处理完成，重启前停止旧实例并确认状态落盘。
- 替换 JSON 失败时旧文件字节保持不变，随后成功写入能够恢复。
- 信号次日开盘成交，停牌则在首个恢复日成交；日期、价格、数量均须匹配。
- 同标的持仓不反复调仓；网格与正式回测均执行真实模拟，不只检查内部传参。
- UTC 输入应按中国交易时段判断；live 与回测都要符合独立的期望仓位序列。

合并了财报展示名、交易日 helper、重复仓位序列及回测委托参数用例。T+1、状态迁移、错误传播、配置拒绝和连接生命周期保留。

PM2 测试需要 PATH 中可用的 Node.js。它从 CLI 传入非默认设置，并执行真实 ecosystem 文件，验证进程选择、脚本和参数；不会启动 PM2 守护进程或业务服务。设置上述产物目录后会额外保存 `pm2-ecosystems.json`，记录五种服务的 PM2 命令及 Node 生成的配置。测试不再逐项冻结默认参数，也不在测试中重算生产哈希算法。

策略测试不再冻结 ETF CLI 默认值、动量腿参数清单或筛选器市值/成交额阈值；参数调优无需同步修改测试快照。独立期望的仓位/动作序列、动量公式与窗口、财报字段单位、CSI 发布文件身份和跨重启 T+1 行为仍保留。两个 CSI 通知模式共享同一重启场景；通知渠道按实际请求与送达信息验证，不锁定整行日志或 `print` 调用方式。
