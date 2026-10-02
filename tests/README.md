# 测试运行与覆盖

使用已安装项目依赖的 conda 环境 `futu_trends`。测试导入真实依赖，仅在行情 SDK、券商及外部通知调用边界使用替身；不在模块导入时安装全局依赖桩。

开发时运行相关文件，例如：

```sh
conda run --no-capture-output -n futu_trends \
  python -m unittest tests.test_indicator_orders_e2e -v
```

开发完成后执行完整离线验证并保留产物，在仓库根目录运行：

```sh
mkdir -p /tmp/futu-tests-final
FUTU_TEST_ARTIFACT_DIR=/tmp/futu-tests-final \
  conda run --no-capture-output -n futu_trends \
  python -m tests.offline_guard \
  > /tmp/futu-tests-final/unittest.log 2>&1
```

检查退出码、日志末尾的 `OK` 和网络闸门记录。闸门拦截并记录所有非本地 socket 连接；本地 HTTP 接收器用于验证通知链路。测试不连接真实券商，不向外部发送通知。单个场景也可以使用 `with offline() as blocked:` 检查连接记录。

主要行为覆盖：

- 条件单忽略零价 Tick，一次性交易状态跨重启保留；撤单重试有次数上限。
- 状态文件原子替换失败时，现有文件保持完整；后续成功写入能够恢复。
- T+1 信号在下一交易日开盘成交，停牌时在恢复日成交；日期、价格和数量可核对。
- 同标的持仓不重复调仓；网格与回测执行真实模拟，保留仓位、委托及成交记录。
- 时间戳按市场当地时区解释；日历区分节假日、全天市及半日市，处理美股夏令时。
- 市场与闸门配置必须显式声明：`CONFIG/MARKET` 支持多个市场；`CONFIG/TRADING_DAY_GATE=on` 过滤休市市场，`off` 允许报告上一根已结束的日/周 K 线。日历缓存无条件信任历史日期，当天及未来日期查询失败时才回退已有缓存。
- 通知按实际请求、HTTP 接收内容及持久化状态验证，覆盖进入条件、冷却和重启去重。

`momentum-daily.csv`、`momentum-trades.json` 来自合成行情的网格与回测计算。PM2 场景需要 PATH 中可用的 Node.js，执行真实 ecosystem 文件并生成 `pm2-ecosystems.json`，不启动业务服务或 PM2 守护进程。这些产物不代表实盘成交或外部通知送达验证。

## 条件单与指标提醒端到端验证

```sh
FUTU_TEST_ARTIFACT_DIR=/tmp/futu-indicator-e2e \
  conda run --no-capture-output -n futu_trends \
  python -m unittest tests.test_indicator_orders_e2e -v
```

场景从 `orders` YAML 加载开始，经 Tick、事件引擎、交易时段聚合、指标计算和状态落盘，再由 AsyncNotifier 与 NotificationEngine 发送到本地 Webhook 接收器。Tick 网关、日历和历史行情 SDK 使用合成输入。

覆盖全部 tools 指标输出、signal 参数继承与显式覆盖、1h/2h/3h/4h/1d/1w 周期边界、午休重置、短尾窗口、节假日短周和分钟历史分页。通知模式缺省 `once`，`on_enter` 支持反复进入条件；包含冷却、缺失数据、重启去重，以及重复通知时 SIM 交易动作只执行一次的组合规则。连续 Tick 不重复取数；没有 Tick 时不触发指标检查，多周期组合使用最近完成的周期结果。

产物包括：

- `sessions.json`、`us-summer.json`、`us-early-close.json`、`hk-half-day.json`、`native-bars.json`：市场时段与周期边界。
- `final-quote-*.json`：真实 Futu 网关解析收盘时间的最后一条合成报价，验证三市场、夏令时/冬令时和半日市的小时窗口结束检查及重复报价去重。
- `tick-boundaries.json`、`mixed-timeframes.json`：周期内取数限制、午休重置、延迟到达的 Tick 与混合周期结果复用。
- `indicator-outputs.json`、`minute-pagination.json`、`calendar-holiday.json`、`calendar-recovery.json`：指标值、数据分页、休市过滤和日历失败恢复。
- `tick-notification-modes.json`、`restart.json`、`modes.json`、`indicator-order.json`：通知模式、冷却、重启状态及一次性模拟成交。

JSON 记录输入行情、Tick 时间、规则配置、日历请求、收到的 HTTP 内容、审计记录和持久化状态。上述命令可重复生成产物；合成收盘报价验证引擎处理能力，不证明供应商必定发送收盘报价，也不验证真实历史行情权限。
