# Futu Trends Board

> 股票趋势分析桌面应用 - 支持多数据源、技术指标分析和参数优化

## 核心功能

- 📊 **实时K线图表** - 基于 TradingView Lightweight Charts，流畅交互体验
- 🎯 **多数据源支持** - 富途 API、Yahoo Finance、AkShare（A股）
- 📈 **技术指标分析** - MACD、KD、RSI 等主流技术指标
- 🎨 **参数优化** - 为每只股票配置和存储最佳参数
- 💾 **参数数据库** - 支持 SQLite 和 MongoDB
- 🔧 **灵活配置** - INI 配置文件，简单易用
- 🖥️ **跨平台** - Windows、macOS、Linux

## 快速开始

### 安装依赖

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

## 配置说明

应用通过 INI 配置文件管理参数，支持两种数据源：

### 1. 富途 OpenD API（推荐用于港股/美股）

```ini
[CONFIG]
DATA_SOURCE = futu
FUTU_HOST = 127.0.0.1
FUTU_PORT = 11111
FUTU_PUSH_TYPE = K_DAY
FUTU_GROUP = 自选股
EMA_PERIOD = 240
```

**前置条件**：
- 下载并运行 [富途 OpenD](https://openapi.futunn.com/futu-api-doc/opend/)
- 需要富途证券账号

### 2. AkShare API（推荐用于A股）

```ini
[CONFIG]
DATA_SOURCE = akshare
AKTOOLS_HOST = 127.0.0.1
AKTOOLS_PORT = 8080
FUTU_CODE_LIST = SH.600000,SH.600519,SZ.000001
FUTU_PUSH_TYPE = K_DAY
EMA_PERIOD = 240
```

**前置条件**：
```bash
# 安装并启动 AkTools
pip install aktools --upgrade
python -m aktools
```

### 3. Yahoo Finance API（全球市场）

```ini
[CONFIG]
DATA_SOURCE = yfinance
FUTU_CODE_LIST = AAPL,TSLA,00700.HK
FUTU_PUSH_TYPE = K_DAY
EMA_PERIOD = 240
```

## 使用说明

1. **加载配置** - 点击 ⚙️ 按钮，选择或创建配置文件
2. **选择股票** - 从列表中选择要分析的股票
3. **查看图表** - 双击股票查看 K 线图和技术指标
4. **参数优化** - 右键股票可设置独立参数
5. **查看日志** - 点击 📋 按钮查看运行日志

### 调试工具

打包后的应用支持开发者工具：
- **快捷键**: `F12` 或 `Ctrl+Shift+I` (macOS: `Cmd+Shift+I`)
- **右键菜单**: 右键点击界面 → "打开开发者工具"

## 技术栈

- **框架**: Electron 39 + Vue 3 + TypeScript
- **图表**: TradingView Lightweight Charts
- **UI**: Naive UI
- **数据**: 富途 SDK / AkShare / Yahoo Finance2
- **数据库**: SQLite / MongoDB

## 项目结构

```
futu-trends-board/
├── src/
│   ├── main/           # Electron 主进程
│   │   ├── dataService.ts    # 数据服务
│   │   ├── configManager.ts  # 配置管理
│   │   └── paramStore.ts     # 参数存储
│   ├── renderer/       # Vue 渲染进程
│   │   ├── components/       # UI 组件
│   │   └── api/             # API 调用
│   └── preload/        # 预加载脚本
├── resources/          # 应用资源（图标等）
└── build/             # 打包配置

```

## 开发命令

```bash
# 格式化代码
npm run format

# 代码检查
npm run lint

# 类型检查
npm run typecheck

# 构建（不打包）
npm run build
```

## 许可证

MIT License

## 问题反馈

如遇到问题，请查看应用内日志（点击 📋 按钮）或提交 Issue。
