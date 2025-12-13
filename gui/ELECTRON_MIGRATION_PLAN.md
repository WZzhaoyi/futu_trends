# Electron 迁移方案

## 📋 目录
1. [项目概述](#项目概述)
2. [技术选型](#技术选型)
3. [架构设计](#架构设计)
4. [实施计划](#实施计划)
5. [可行性分析](#可行性分析)

---

## 项目概述

### 当前系统
- **前端**: Tkinter (Python GUI)
- **图表库**: lightweight_charts (Python)
- **数据源**: 富途API、yfinance、akshare、IBKR
- **技术指标**: KD、MACD、RSI、EMA
- **数据存储**: SQLite (参数数据库)
- **配置**: ConfigParser

### 目标系统
- **前端**: Electron + React/Vue
- **图表库**: TradingView Lightweight Charts (JavaScript)
- **后端**: Python 服务层 (保留现有业务逻辑)
- **通信**: IPC (Electron) + HTTP API (可选)

---

## 技术选型（第一版确定方案）

### 1. 前端框架

#### ✅ 确定方案: Vue 3 + TypeScript
```json
{
  "优势": [
    "学习曲线平缓，开发效率高",
    "组合式 API (Composition API) 灵活易用",
    "性能优秀，体积小",
    "TypeScript 提供类型安全",
    "生态成熟，组件库丰富"
  ],
  "技术栈": [
    "Vue 3.4+",
    "TypeScript 5.0+",
    "Vite 5.0+ (构建工具)",
    "Pinia (状态管理)",
    "Naive UI (轻量级UI组件库)"
  ]
}
```

**选择理由**: 
- Vue 3 组合式 API 更适合快速开发
- 代码简洁，维护成本低
- 与 TradingView Charts 集成简单

---

### 2. 图表库

#### ✅ 确定方案: TradingView Lightweight Charts
```typescript
// 官方库，与 Python lightweight_charts 功能对等
import { createChart, IChartApi } from 'lightweight-charts';

// 优势：
// - 与 Python 版本 API 相似，迁移成本低
// - 性能优秀，支持大量数据（1000+ K线流畅）
// - 支持 K线、指标、子图、多时间周期
// - 主题切换（深色/浅色）原生支持
// - 免费开源，文档完善
// - 体积小（~200KB gzipped）
```

**安装**:
```bash
npm install lightweight-charts
```

**类型定义**:
```bash
npm install @types/lightweight-charts --save-dev
```

### 3. UI 组件库

#### ✅ 确定方案: Naive UI
```json
{
  "优势": [
    "专为 Vue 3 设计，完全支持 TypeScript",
    "体积小，按需引入（Tree-shaking）",
    "组件丰富，覆盖常用场景",
    "主题定制简单，支持深色/浅色模式",
    "性能优秀，无依赖"
  ],
  "适用组件": [
    "n-input (搜索框)",
    "n-data-table (股票列表)",
    "n-button (按钮)",
    "n-select (下拉选择)",
    "n-config-provider (主题配置)"
  ]
}
```

**安装**:
```bash
npm install naive-ui
npm install @vicons/ionicons5  # 图标库（可选）
```

**备选方案**: Element Plus（功能更全但体积稍大）

---

### 4. 后端架构

#### ✅ 确定方案: HTTP API 服务 (FastAPI)
```
Electron 渲染进程 (Vue 3)
    ↓ HTTP RESTful API
Python FastAPI 服务 (本地 127.0.0.1:8000)
    ↓
数据获取 + 指标计算 + 数据存储
```

**优点**:
- ✅ 前后端完全分离，架构清晰
- ✅ 保留现有 Python 代码，复用 `data.py`, `signal_analysis/`, `tools.py`
- ✅ 易于调试（可独立测试 API）
- ✅ 可扩展（未来可支持 Web 版本）
- ✅ 本地通信延迟可忽略（<1ms）

**技术栈**:
- **框架**: FastAPI (高性能，自动生成 API 文档)
- **异步**: asyncio (支持并发请求)
- **CORS**: 允许 Electron 跨域访问
- **端口**: 127.0.0.1:8000 (本地服务)

**API 端点设计**:
```
GET  /api/stocks/list              # 获取股票列表
GET  /api/kline/{code}              # 获取K线数据
GET  /api/indicators/{code}         # 获取技术指标（MACD/KD/RSI/EMA）
GET  /api/config                    # 获取配置信息
POST /api/config                    # 更新配置
```

---

### 5. 技术指标计算

#### ✅ 确定方案: Python 后端计算
- ✅ 保留现有 `signal_analysis/` 模块（KD, MACD, RSI）
- ✅ 保留现有 `tools.py` 中的 EMA 等工具函数
- ✅ 通过 FastAPI 返回计算结果
- ✅ 确保计算逻辑与现有系统完全一致

**实现方式**:
```python
# backend/api/routes/indicators.py
from signal_analysis import KD, MACD, RSI
from tools import EMA
from params_db import ParamsDB

@app.get("/api/indicators/{code}")
async def get_indicators(code: str):
    # 1. 获取K线数据
    df = get_kline_data(code, config)
    
    # 2. 从 SQLite 读取参数
    macd_params = db.get_stock_params(code, 'MACD')
    kd_params = db.get_stock_params(code, 'KD')
    rsi_params = db.get_stock_params(code, 'RSI')
    
    # 3. 计算指标（复用现有逻辑）
    macd = MACD()
    vmacd, signal = macd.indicator_calculate(df, macd_params)
    
    # 4. 返回 JSON
    return {"macd": {...}, "kd": {...}, "rsi": {...}, "ema": [...]}
```

**优势**:
- 无需重写指标计算逻辑
- 保证计算结果与现有系统一致
- 参数从 SQLite 读取，保持数据一致性

---

### 6. 数据存储

#### ✅ 确定方案: Python 后端统一管理

**存储架构**:
```
Python 后端 (FastAPI)
    ├── SQLite (参数数据库)
    │   ├── MACD 参数: macd_params_daily.db
    │   ├── KD 参数: kd_params_daily.db
    │   └── RSI 参数: rsi_params_daily.db
    │
    └── MongoDB (可选，用于扩展数据存储)
        └── 历史数据、回测结果等
```

**实现方式**:
- ✅ **SQLite**: 通过 `params_db.py` 读取参数（MACD/KD/RSI 最优参数）
- ✅ **MongoDB**: 可选，用于存储历史数据、回测结果等（第一版可暂不实现）
- ✅ **配置文件**: 继续使用 `ConfigParser` 读取 `ft_config.py`

**前端配置管理**:
```typescript
// 使用 electron-store 存储前端配置（主题、窗口大小等）
import Store from 'electron-store';

const store = new Store({
  defaults: {
    darkMode: true,
    windowSize: { width: 1200, height: 900 }
  }
});
```

**数据访问流程**:
```
Vue 前端
  ↓ HTTP API
Python FastAPI
  ↓
params_db.py → SQLite (读取参数)
data.py → 富途/yfinance/akshare (获取K线)
signal_analysis/ → 计算指标
  ↓
返回 JSON 给前端
```

---

## 架构设计

### 整体架构图

```
┌─────────────────────────────────────────────────────┐
│              Electron 应用                          │
│  ┌──────────────────────────────────────────────┐  │
│  │   渲染进程 (Vue 3 + TypeScript)             │  │
│  │  - GroupList.vue (股票列表)                  │  │
│  │  - SignalWindow.vue (图表窗口)                │  │
│  │  - TradingView Lightweight Charts            │  │
│  │  - Naive UI 组件                              │  │
│  └──────────────┬───────────────────────────────┘  │
│                 │ IPC (窗口管理)                    │
│  ┌──────────────▼───────────────────────────────┐  │
│  │   主进程 (Node.js)                           │  │
│  │  - 窗口管理 (BrowserWindow)                  │  │
│  │  - IPC 路由 (ipcMain)                        │  │
│  │  - Python 服务管理 (spawn)                    │  │
│  └──────────────┬───────────────────────────────┘  │
└─────────────────┼───────────────────────────────────┘
                  │ HTTP RESTful API (127.0.0.1:8000)
┌─────────────────▼───────────────────────────────────┐
│         Python 后端服务 (FastAPI)                   │
│  ┌──────────────────────────────────────────────┐  │
│  │  API 端点:                                   │  │
│  │  - GET  /api/stocks/list                     │  │
│  │  - GET  /api/kline/{code}                    │  │
│  │  - GET  /api/indicators/{code}               │  │
│  │  - GET  /api/config                          │  │
│  └──────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────┐  │
│  │  业务逻辑层 (复用现有代码)                    │  │
│  │  - data.py (数据获取: 富途/yfinance/akshare) │  │
│  │  - signal_analysis/ (指标计算: KD/MACD/RSI)  │  │
│  │  - tools.py (工具函数: EMA等)                │  │
│  │  - params_db.py (参数读取: SQLite)           │  │
│  │  - ft_config.py (配置管理: ConfigParser)     │  │
│  └──────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────┐  │
│  │  数据存储层                                    │  │
│  │  - SQLite (参数数据库)                        │  │
│  │  - MongoDB (可选，第一版暂不实现)            │  │
│  └──────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

### 第一版目录结构（极简版 - 快速跑通）

```
futu_trends_electron/
├── electron/
│   ├── main.ts                        # 主进程（包含 Python 服务管理）
│   └── preload.ts                     # 预加载脚本
│
├── src/
│   ├── main.ts                        # 入口文件
│   ├── App.vue                        # 根组件
│   ├── GroupList.vue                  # 股票列表组件（包含 API 调用）
│   └── SignalWindow.vue               # 图表窗口组件（包含图表逻辑）
│
├── backend/
│   └── api.py                         # FastAPI 应用（所有路由集中在此）
│
├── package.json
├── tsconfig.json
├── vite.config.ts
└── requirements.txt
```

**设计原则（第一版）**:
- ✅ **极简文件结构**: 前端 4 个文件，后端 1 个文件
- ✅ **代码集中**: API 调用、图表逻辑直接写在组件中
- ✅ **快速跑通**: 最小化文件数量，减少跳转
- ✅ **复用现有代码**: 后端直接 import 现有模块（data.py, tools.py 等）

**文件说明**:
- `electron/main.ts`: 主进程 + Python 服务启动逻辑
- `src/GroupList.vue`: 股票列表 + 搜索 + API 调用（内联）
- `src/SignalWindow.vue`: 图表 + 指标显示 + API 调用（内联）
- `backend/api.py`: 所有 API 路由集中在一个文件

---

## 第一版实施计划（简化高效）

### 阶段 1: 项目初始化 (1-2天)

#### 1.1 创建项目结构
```bash
# 初始化项目
npm init -y
npm install -D typescript vite @vitejs/plugin-vue electron electron-builder
npm install vue@next naive-ui lightweight-charts axios
npm install -D @types/node

# Python 后端依赖
pip install fastapi uvicorn
```

#### 1.2 配置 TypeScript + Vite
```typescript
// vite.config.ts
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  server: { port: 5173 }
});
```

#### 1.3 创建 FastAPI 单文件
```python
# backend/api.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.append(str(Path(__file__).resolve().parent.parent))

from ft_config import get_config
from data import get_kline_data
from signal_analysis import KD, MACD, RSI
from tools import EMA, code_in_futu_group
from params_db import ParamsDB

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"])

config = get_config()

# 所有路由直接写在这里
@app.get("/api/stocks/list")
async def get_stock_list():
    # 实现逻辑
    pass

@app.get("/api/kline/{code}")
async def get_kline(code: str, max_count: int = 1000):
    # 实现逻辑
    pass

@app.get("/api/indicators/{code}")
async def get_indicators(code: str):
    # 实现逻辑
    pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

**目标**: 项目可运行，前后端可独立启动

---

### 阶段 2: 后端 API 开发 (2-3天)

#### 2.1 在 `backend/api.py` 中实现所有 API

```python
# backend/api.py - 所有 API 集中在一个文件

@app.get("/api/stocks/list")
async def get_stock_list():
    """获取股票列表"""
    stocks = []
    group = config.get("CONFIG", "FUTU_GROUP", fallback='')
    host = config.get("CONFIG", "FUTU_HOST", fallback='127.0.0.1')
    port = config.getint("CONFIG", "FUTU_PORT", fallback=11111)
    
    # 从富途分组获取
    if group and host and port:
        df = code_in_futu_group(group, host, port)
        if isinstance(df, pd.DataFrame):
            stocks.extend(df[['code', 'name']].to_dict('records'))
    
    # 从配置获取
    code_list = config.get("CONFIG", "FUTU_CODE_LIST", fallback='').split(',')
    for code in code_list:
        if code.strip():
            stocks.append({'code': code.strip(), 'name': code.strip()})
    
    return {"stocks": stocks}

@app.get("/api/kline/{code}")
async def get_kline(code: str, max_count: int = 1000):
    """获取K线数据"""
    df = get_kline_data(code, config, max_count)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="未找到数据")
    df['time'] = df.index.astype(str)
    return {"data": df[['time', 'open', 'high', 'low', 'close', 'volume']].to_dict('records')}

@app.get("/api/indicators/{code}")
async def get_indicators(code: str):
    """获取技术指标"""
    df = get_kline_data(code, config, max_count=1000)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="未找到数据")
    
    df['time'] = df.index.astype(str)
    result = {'time': df['time'].tolist()}
    
    # EMA
    ema_period = config.getint("CONFIG", "EMA_PERIOD", fallback=240)
    result['ema'] = EMA(df['close'], ema_period).tolist()
    
    # MACD/KD/RSI（复用现有逻辑）
    # ... 实现代码
    
    return result
```

**目标**: 所有 API 端点可用，返回数据格式正确

---

### 阶段 3: 前端核心组件 (4-5天)

#### 3.1 GroupList 组件 (2天) - API 调用内联
```vue
<!-- src/GroupList.vue -->
<template>
  <n-config-provider :theme="darkTheme">
    <div class="group-list">
      <n-input v-model:value="searchTerm" placeholder="Search / Filter..." />
      <n-data-table :columns="columns" :data="filteredStocks" @dblclick="openChart" />
    </div>
  </n-config-provider>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { NConfigProvider, NInput, NDataTable, darkTheme } from 'naive-ui';
import axios from 'axios';
import type { DataTableColumns } from 'naive-ui';

// 类型定义（内联）
interface Stock {
  code: string;
  name: string;
}

// API 调用（内联，不单独文件）
const API_BASE = 'http://127.0.0.1:8000';
const stocks = ref<Stock[]>([]);
const searchTerm = ref('');
const loading = ref(false);

const loadStocks = async () => {
  loading.value = true;
  try {
    const res = await axios.get(`${API_BASE}/api/stocks/list`);
    stocks.value = res.data.stocks;
  } catch (error) {
    console.error('加载失败:', error);
  } finally {
    loading.value = false;
  }
};

const filteredStocks = computed(() => {
  if (!searchTerm.value) return stocks.value;
  const keyword = searchTerm.value.toLowerCase();
  return stocks.value.filter(s => 
    s.code.toLowerCase().includes(keyword) || 
    s.name.toLowerCase().includes(keyword)
  );
});

const openChart = (row: Stock) => {
  if (window.electronAPI) {
    window.electronAPI.openChartWindow(row.code);
  }
};

const columns: DataTableColumns<Stock> = [
  { title: '代码', key: 'code', width: 100 },
  { title: '名称', key: 'name', width: 200 },
];

onMounted(loadStocks);
</script>
```

#### 3.2 SignalWindow 组件 (2-3天) - 图表逻辑内联
```vue
<!-- src/SignalWindow.vue -->
<template>
  <div ref="chartContainer" class="chart-container"></div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue';
import { createChart, IChartApi, ColorType } from 'lightweight-charts';
import axios from 'axios';

const props = defineProps<{ code: string }>();
const chartContainer = ref<HTMLDivElement>();
const chartRef = ref<IChartApi | null>(null);
const API_BASE = 'http://127.0.0.1:8000';

// 加载图表数据（内联，不单独文件）
const loadChartData = async (chart: IChartApi, code: string) => {
  try {
    // 加载K线
    const klineRes = await axios.get(`${API_BASE}/api/kline/${code}`);
    const candlestickSeries = chart.addCandlestickSeries({
      upColor: 'rgba(255, 82, 82, 1)',
      downColor: 'rgba(0, 168, 67, 1)',
    });
    candlestickSeries.setData(klineRes.data.data.map((d: any) => ({
      time: d.time as any,
      open: d.open,
      high: d.high,
      low: d.low,
      close: d.close,
    })));

    // 加载指标
    const indicatorsRes = await axios.get(`${API_BASE}/api/indicators/${code}`);
    const indicators = indicatorsRes.data;
    
    // EMA
    if (indicators.ema) {
      const emaSeries = chart.addLineSeries({ color: 'rgba(224,82,211,0.8)' });
      emaSeries.setData(indicators.time.map((t: string, i: number) => ({
        time: t as any,
        value: indicators.ema[i],
      })));
    }
    
    // MACD/KD/RSI 子图（实现逻辑）
    // ...
  } catch (error) {
    console.error('加载失败:', error);
  }
};

onMounted(() => {
  if (!chartContainer.value) return;
  const chart = createChart(chartContainer.value, {
    layout: {
      background: { type: ColorType.Solid, color: '#191919' },
      textColor: '#ffffff',
    },
    width: chartContainer.value.clientWidth,
    height: chartContainer.value.clientHeight,
  });
  chartRef.current = chart;
  loadChartData(chart, props.code);
  
  window.addEventListener('resize', () => {
    if (chartContainer.value && chart) {
      chart.applyOptions({
        width: chartContainer.value.clientWidth,
        height: chartContainer.value.clientHeight,
      });
    }
  });
});

onUnmounted(() => {
  if (chartRef.value) chartRef.value.remove();
});
</script>
```

**目标**: 两个核心组件功能完整，代码集中，易于理解

---

### 阶段 4: Electron 集成 (1-2天)

#### 4.1 主进程（包含 Python 服务管理）
```typescript
// electron/main.ts - 所有逻辑集中在一个文件
import { app, BrowserWindow, ipcMain } from 'electron';
import { spawn, ChildProcess } from 'child_process';
import * as path from 'path';

let pythonService: ChildProcess | null = null;

// 启动 Python 服务
function startPythonService() {
  pythonService = spawn('python', [path.join(__dirname, '../backend/api.py')]);
  pythonService.stdout?.on('data', (data) => console.log(`Python: ${data}`));
  pythonService.stderr?.on('data', (data) => console.error(`Python错误: ${data}`));
}

// 创建主窗口
function createWindow() {
  const win = new BrowserWindow({
    width: 400,
    height: 700,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });
  
  if (process.env.NODE_ENV === 'development') {
    win.loadURL('http://localhost:5173');
  } else {
    win.loadFile(path.join(__dirname, '../dist/index.html'));
  }
}

// IPC: 打开图表窗口
ipcMain.handle('open-chart-window', (event, code: string) => {
  const chartWin = new BrowserWindow({
    width: 1200,
    height: 900,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });
  
  if (process.env.NODE_ENV === 'development') {
    chartWin.loadURL(`http://localhost:5173?code=${code}`);
  } else {
    chartWin.loadFile(path.join(__dirname, '../dist/index.html'), {
      query: { code },
    });
  }
  
  return chartWin.id;
});

app.whenReady().then(() => {
  startPythonService();
  createWindow();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    if (pythonService) pythonService.kill();
    app.quit();
  }
});
```

#### 4.2 预加载脚本
```typescript
// electron/preload.ts
import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('electronAPI', {
  openChartWindow: (code: string) => ipcRenderer.invoke('open-chart-window', code),
});
```

**目标**: Electron 应用可启动，窗口管理正常

---

### 阶段 5: 测试与优化 (1-2天)

- ✅ 功能测试（股票列表、图表显示、指标计算）
- ✅ 错误处理（API 失败、数据为空）
- ✅ 打包配置（electron-builder）

**总耗时**: 约 9-13 天（1.5-2 周）

**关键优化**:
- ✅ 文件数量从 15+ 减少到 7 个核心文件
- ✅ 代码集中，减少文件跳转
- ✅ 快速跑通流程，后续再优化结构

---

## 可行性分析

### ✅ 可行性评估

| 功能模块 | 可行性 | 说明 |
|---------|--------|------|
| **股票列表** | ⭐⭐⭐⭐⭐ | React 组件实现简单，API 调用直接 |
| **搜索过滤** | ⭐⭐⭐⭐⭐ | 前端实现，无难度 |
| **K线图表** | ⭐⭐⭐⭐⭐ | TradingView Charts 功能完整 |
| **技术指标** | ⭐⭐⭐⭐ | 需确认指标计算库或使用 Python 后端 |
| **多窗口管理** | ⭐⭐⭐⭐⭐ | Electron 原生支持 |
| **主题切换** | ⭐⭐⭐⭐⭐ | CSS/JS 实现简单 |
| **数据获取** | ⭐⭐⭐⭐ | 需 Python 后端，但现有代码可复用 |
| **参数存储** | ⭐⭐⭐⭐ | SQLite 或通过 API |

### ⚠️ 潜在挑战

1. **Python 环境打包**
   - 使用 PyInstaller 或 py2exe
   - 或使用 conda-pack
   - **解决方案**: 使用 HTTP API，Python 作为独立服务

2. **富途 API 集成**
   - 富途 Python SDK 无法在 Node.js 使用
   - **解决方案**: 保留 Python 后端，通过 API 暴露

3. **性能考虑**
   - 大量数据渲染
   - **解决方案**: TradingView Charts 性能优秀，支持虚拟滚动

4. **跨平台兼容**
   - Windows/Mac/Linux
   - **解决方案**: Electron 天然跨平台

---

## 第一版技术栈总结

### 前端
- **框架**: Vue 3.4+ + TypeScript 5.0+
- **构建**: Vite 5.0+
- **图表**: TradingView Lightweight Charts 4.1+
- **UI组件**: Naive UI (轻量级)
- **状态管理**: Pinia
- **HTTP客户端**: Axios
- **图标**: @vicons/ionicons5 (可选)

### 后端
- **框架**: FastAPI
- **语言**: Python 3.10+
- **数据获取**: 复用现有 `data.py` (富途/yfinance/akshare)
- **指标计算**: 复用现有 `signal_analysis/` (KD/MACD/RSI)
- **工具函数**: 复用现有 `tools.py` (EMA等)
- **参数存储**: 复用现有 `params_db.py` (SQLite)
- **配置管理**: 复用现有 `ft_config.py` (ConfigParser)

### Electron
- **版本**: Electron 28+
- **打包**: electron-builder
- **配置存储**: electron-store (前端配置)

### Electron
- **版本**: Electron 28+
- **打包**: electron-builder
- **配置**: electron-store

---

## 第一版开发时间估算

| 阶段 | 任务 | 时间 | 难度 | 并行度 |
|------|------|------|------|--------|
| **阶段1** | 项目初始化 | 2-3天 | 低 | - |
| **阶段2** | 后端 API 开发 | 3-4天 | 中 | 可与阶段3并行 |
| **阶段3** | 前端核心组件 | 5-6天 | 中高 | 依赖阶段2 |
| **阶段4** | Electron 集成 | 2-3天 | 中 | 依赖阶段3 |
| **阶段5** | 测试与优化 | 2-3天 | 中 | - |
| **总计** | | **14-19天** | **中** | **2.5-3周** |

**优化策略**:
- ✅ 复用现有 Python 代码，减少开发时间
- ✅ 使用成熟的 Vue 3 + Naive UI，快速搭建 UI
- ✅ 后端 API 与前端组件可并行开发（Mock 数据）
- ✅ 分阶段交付，每个阶段都有可运行版本

---

## 结论

### ✅ 推荐迁移
1. **用户体验提升**: 现代化 UI，更好的交互
2. **跨平台支持**: Windows/Mac/Linux
3. **可维护性**: 前后端分离，代码结构清晰
4. **扩展性**: 易于添加新功能（Web 技术栈）

### 📝 注意事项
1. 保留 Python 后端，复用现有业务逻辑
2. 使用 HTTP API 通信，避免进程间通信复杂性
3. 分阶段迁移，先实现核心功能
4. 充分测试，确保功能对等

---

## 下一步行动

1. **POC (概念验证)**: 先实现一个简单的图表窗口，验证技术栈
2. **API 设计**: 定义前后端接口规范
3. **组件设计**: 设计 Vue 组件结构
4. **逐步迁移**: 按阶段实施

---

---

## 第一版实施总结

### 技术路线确认

| 技术栈 | 选型 | 理由 |
|--------|------|------|
| **前端框架** | Vue 3 + TypeScript | 学习曲线平缓，开发效率高，组合式 API 灵活 |
| **UI组件库** | Naive UI | 轻量级，专为 Vue 3 设计，支持 TypeScript |
| **图表库** | TradingView Lightweight Charts | 与 Python 版本 API 相似，性能优秀 |
| **后端架构** | FastAPI (HTTP API) | 前后端分离清晰，易于调试和扩展 |
| **指标计算** | Python 后端 | 复用现有代码，保证计算准确性 |
| **数据存储** | SQLite + MongoDB (Python后端) | 复用现有 params_db.py，MongoDB 第一版可选 |

### 第一版目录结构（极简版 - 最终确定）

```
futu_trends_electron/
├── electron/
│   ├── main.ts                        # 主进程（包含 Python 服务管理）
│   └── preload.ts                     # 预加载脚本
│
├── src/
│   ├── main.ts                        # 入口文件
│   ├── App.vue                        # 根组件
│   ├── GroupList.vue                  # 股票列表（API 调用内联）
│   └── SignalWindow.vue               # 图表窗口（图表逻辑内联）
│
├── backend/
│   └── api.py                         # FastAPI 应用（所有路由集中）
│
├── package.json
├── tsconfig.json
├── vite.config.ts
└── requirements.txt
```

**文件统计**:
- 前端: 4 个文件（main.ts, App.vue, GroupList.vue, SignalWindow.vue）
- 后端: 1 个文件（api.py）
- Electron: 2 个文件（main.ts, preload.ts）
- **总计: 7 个核心文件**

### 第一版功能范围

#### ✅ 必须实现（MVP）
1. **股票列表组件**
   - 搜索/过滤功能
   - 双击/回车打开图表
   - 深色/浅色主题

2. **图表窗口组件**
   - K线图显示
   - EMA 均线
   - MACD 子图
   - KD 子图
   - RSI 子图

3. **后端 API**
   - 股票列表 API
   - K线数据 API
   - 技术指标 API

4. **Electron 集成**
   - 窗口管理
   - Python 服务自动启动
   - IPC 通信

#### ⏸️ 暂不实现（后续版本）
- MongoDB 数据存储
- 配置管理界面
- 数据导出功能
- 多时间周期切换
- 回测功能

### 开发时间线（简化版）

```
第1周: 项目初始化 + 后端 API + 前端组件
  ├── 项目搭建 (1-2天)
  ├── 后端 API (2-3天) - 单文件实现
  └── 前端组件 (4-5天) - API 调用内联

第2周: Electron 集成 + 测试
  ├── Electron 集成 (1-2天)
  └── 测试与优化 (1-2天)
```

**总耗时**: 9-13 天（1.5-2 周）

**简化优势**:
- ✅ 文件数量减少 50%+
- ✅ 代码集中，易于理解和调试
- ✅ 快速跑通流程，验证可行性
- ✅ 后续可逐步拆分优化

### 关键决策点

1. **复用现有代码**: 通过符号链接或直接引用，避免重复开发
2. **最小化依赖**: 使用轻量级组件库，减少打包体积
3. **类型安全**: 完整的 TypeScript 类型定义
4. **分阶段交付**: 每个阶段都有可运行版本，第一版实现尽可能轻量化

---

**最后更新**: 2025年12月



