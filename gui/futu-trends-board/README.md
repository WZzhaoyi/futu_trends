# Futu Trends Board

> 股票趋势分析工具 - 支持多数据源和自定义技术指标

## 特性

- 📊 **实时K线图表** - 基于 TradingView Lightweight Charts
- 🎯 **多数据源** - 支持富途 API 和 Yahoo Finance
- 📈 **技术指标** - MACD、KD、RSI 等主流指标
- 🎨 **自定义参数** - 支持为每只股票配置独立参数
- 💾 **参数数据库** - SQLite/MongoDB 存储优化参数
- 🖥️ **跨平台** - Windows、macOS、Linux

## 快速开始

### 安装

```bash
npm install
```

### 开发模式

```bash
npm run dev
```

### 打包应用

```bash
# Windows
npm run build:win

# macOS  
npm run build:mac

# Linux
npm run build:linux
```

## 配置

创建或编辑配置文件（`.ini` 格式）：

```ini
[CONFIG]
# 数据源选择：futu 或 yfinance
DATA_SOURCE = yfinance

# 富途配置（使用 futu 数据源时需要）
FUTU_HOST = 127.0.0.1
FUTU_PORT = 11111
FUTU_GROUP = 自选股

# EMA 指标周期
EMA_PERIOD = 20
```

## 使用说明

1. **启动应用** - 运行打包后的应用或开发模式
2. **加载配置** - 点击"加载配置"选择 `.ini` 文件
3. **选择股票** - 从列表中选择要查看的股票
4. **查看图表** - 自动显示K线图和技术指标

### 调试控制台

在打包后的应用中，你可以通过以下方式打开开发者控制台：

- **快捷键**: 按 `F12` 或 `Ctrl+Shift+I` (macOS: `Cmd+Shift+I`)
- **右键菜单**: 在应用界面任意位置右键点击，选择"打开开发者工具"

控制台可以帮助你：
- 查看应用日志和错误信息
- 调试前端问题
- 检查网络请求
- 查看应用性能

## 技术栈

- Electron 39
- Vue 3 + TypeScript
- TradingView Lightweight Charts
- Naive UI

## 开发文档

详见 [TECHNICAL_NOTES.md](./TECHNICAL_NOTES.md)

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request
