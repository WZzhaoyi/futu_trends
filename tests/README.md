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

测试绝不允许真的发出通知。想验证这一点（或在改过通知/下单代码后复查），用网络闸门再跑一遍：

```sh
conda run --no-capture-output -n futu_trends python -m tests.offline_guard
```

它把 socket 层非本地连接全部拦掉并计数，任何直连 webhook/Telegram/邮件的用例都会当场失败，而不是把消息发出去。当前基线：153 个用例通过，拦截记录只有 1 条 —— `xtquant` 导入时的 pypi.org 版本检查（第三方行为，与通知无关）。单用例里也可以 `with offline() as blocked:` 包住，断言 `blocked == []`。

本次保留的关键故障判据：

- 零价会命中止损条件但必须被忽略；有效价格仍能触发。等待 tick 处理完成，重启前停止旧实例并确认状态落盘。
- 替换 JSON 失败时旧文件字节保持不变，随后成功写入能够恢复。
- 信号次日开盘成交，停牌则在首个恢复日成交；日期、价格、数量均须匹配。
- 同标的持仓不反复调仓；网格与正式回测均执行真实模拟，不只检查内部传参。
- UTC 输入应按中国交易时段判断；live 与回测都要符合独立的期望仓位序列。
- 交易日按市场当地日期判定，每个市场问自己的日历锚点（美股 `US.QQQ` / A股 `SH.000001` / 港股 `HK.800000`）：上海早上跑美股必须落到美东的前一天，半日市（`MORNING`）仍算交易日，三个市场的节假日互不影响（2026-10-02 A股休市而港股美股开市）；日历缓存只信任已过去的日期，当天结论一律现查。
- 市场必须显式声明，没有任何兜底默认值：`trading_calendar` 的接口必须显式传入市场（空值/未知市场直接报错），定时任务从 `CONFIG/MARKET` 读取（可多个，`CN,HK` 表示任一市场开市即运行），配置缺这一项时任务直接失败而不是猜一个市场。多市场配置（如 A股+港股混合分组）按「任一市场开市即运行」，但**休市市场的标的行不进报告**（正文标注 `休市跳过: CN`），当期快照仍保存全量供 rank_rotation 对比。`CONFIG/TRADING_DAY_GATE` 也是必填项（与 `MARKET` 一样由日历模块统一解析，无默认值）：`on` 按交易日过滤，`off` 表示本任务不受交易日约束（如数据来自上一根已收盘周K的周报），此时既不查日历也不剔行；缺这一项或取值非法都直接报错。

合并了财报展示名、交易日 helper、重复仓位序列及回测委托参数用例。T+1、状态迁移、错误传播、配置拒绝和连接生命周期保留。

PM2 测试需要 PATH 中可用的 Node.js。它从 CLI 传入非默认设置，并执行真实 ecosystem 文件，验证进程选择、脚本和参数；不会启动 PM2 守护进程或业务服务。设置上述产物目录后会额外保存 `pm2-ecosystems.json`，记录五种服务的 PM2 命令及 Node 生成的配置。测试不再逐项冻结默认参数，也不在测试中重算生产哈希算法。

策略测试不再冻结 ETF CLI 默认值、动量腿参数清单或筛选器市值/成交额阈值；参数调优无需同步修改测试快照。独立期望的仓位/动作序列、动量公式与窗口、财报字段单位、CSI 发布文件身份和跨重启 T+1 行为仍保留。两个 CSI 通知模式共享同一重启场景；通知渠道按实际请求与送达信息验证，不锁定整行日志或 `print` 调用方式。
