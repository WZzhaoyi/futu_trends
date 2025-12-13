# Futu Trends Electron - 第一版

这是 Futu Trends 的 Electron 版本第一版实现，目的是快速验证可行性和识别性能瓶颈。

## 技术栈

- **前端**: Vue 3 + TypeScript + Naive UI + TradingView Lightweight Charts
- **后端**: FastAPI (Python)
- **Electron**: 主进程管理窗口和 Python 服务

## 项目结构

```
gui/electron/
├── electron/           # Electron 主进程
│   ├── main.ts        # 主进程入口
│   └── preload.ts     # 预加载脚本
├── src/               # Vue 前端
│   ├── main.ts       # 入口文件
│   ├── App.vue       # 根组件
│   ├── GroupList.vue # 股票列表组件
│   ├── SignalWindow.vue # 图表窗口组件
│   └── types/        # 类型定义
├── backend/          # Python 后端（在 gui/backend/）
│   └── api.py        # FastAPI 应用（单文件）
├── package.json
├── tsconfig.json
├── vite.config.ts
└── requirements.txt
```

## 快速开始

### 1. 安装依赖

```bash
# 前端依赖
cd gui/electron
npm install

# Python 后端依赖
pip install -r requirements.txt
```

### 2. 启动开发环境

**方式一：分别启动（推荐用于调试）**

```bash
# 终端1: 启动 Python 后端
cd gui
python backend/api.py --config=your_config_file

# 终端2: 启动 Vite 开发服务器
cd gui/electron
npm run dev

# 终端3: 启动 Electron
cd gui/electron
npm run electron:dev
```

**方式二：使用 concurrently（一键启动）**

```bash
cd gui/electron
npm run electron:dev
```

### 3. 构建生产版本

```bash
# 构建前端
npm run build

# 打包 Electron 应用
npm run electron:build
```

## 功能说明

### 第一版功能（MVP）

1. **股票列表组件 (GroupList.vue)**
   - 显示股票列表（从配置或富途分组获取）
   - 搜索/过滤功能
   - 双击/回车打开图表窗口

2. **图表窗口组件 (SignalWindow.vue)**
   - K线图显示
   - EMA 均线
   - MACD 子图
   - KD 子图
   - RSI 子图

3. **后端 API (backend/api.py)**
   - `GET /api/stocks/list` - 获取股票列表
   - `GET /api/kline/{code}` - 获取K线数据
   - `GET /api/indicators/{code}` - 获取技术指标

4. **Electron 集成**
   - 窗口管理（主窗口 + 图表窗口）
   - Python 服务自动启动
   - IPC 通信

## 配置说明

后端 API 会读取项目根目录的配置文件（通过 `ft_config.py`），需要确保：

1. 配置文件存在（如 `config_template.ini`）
2. 配置中包含必要的参数：
   - `FUTU_GROUP` - 富途分组名称（可选）
   - `FUTU_HOST` - 富途主机地址
   - `FUTU_PORT` - 富途端口
   - `FUTU_CODE_LIST` - 股票代码列表（逗号分隔）
   - `DATA_SOURCE` - 数据源（futu/yfinance/akshare）
   - `FUTU_PUSH_TYPE` - K线类型
   - `EMA_PERIOD` - EMA 周期
   - `MACD_PARAMS_DB` - MACD 参数数据库路径
   - `KD_PARAMS_DB` - KD 参数数据库路径
   - `RSI_PARAMS_DB` - RSI 参数数据库路径

## 开发注意事项

### 第一版简化原则

- ✅ **代码集中**: API 调用、图表逻辑直接写在组件中
- ✅ **单文件后端**: 所有 API 路由集中在一个文件
- ✅ **快速验证**: 最小化文件数量，减少跳转
- ✅ **类型安全**: 使用 TypeScript，类型定义内联

### 性能验证重点

1. **数据加载性能**
   - K线数据获取速度
   - 指标计算耗时
   - 前端渲染性能

2. **内存使用**
   - 大量数据的内存占用
   - 图表实例的内存管理

3. **响应速度**
   - 窗口切换速度
   - 图表更新速度

## 已知限制

1. **子图实现**: 使用 `priceScaleId` 实现子图，可能不如独立图表清晰
2. **错误处理**: 第一版错误处理较简单，后续需要完善
3. **配置管理**: 前端配置管理暂未实现
4. **数据缓存**: 未实现数据缓存机制

## 后续优化方向

1. 拆分代码结构（API 封装、图表逻辑分离）
2. 添加数据缓存机制
3. 完善错误处理和用户提示
4. 优化图表性能（虚拟滚动、数据分页）
5. 添加配置管理界面

## 故障排查

### Python 服务启动失败

- 检查 Python 环境是否正确
- 检查 `backend/api.py` 路径是否正确
- 检查配置文件是否存在

### 前端无法连接后端

- 确认后端服务运行在 `http://127.0.0.1:8000`
- 检查 CORS 配置
- 查看浏览器控制台错误信息

### 图表不显示

- 检查数据格式是否正确
- 查看浏览器控制台错误
- 确认 TradingView Lightweight Charts 版本兼容性

## 许可证

Apache License 2.0

