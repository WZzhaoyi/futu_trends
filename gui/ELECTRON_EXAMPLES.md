# Electron 迁移示例代码（Vue 3 + TypeScript）

## 技术栈
- **前端**: Vue 3 + TypeScript
- **UI组件**: Naive UI
- **图表**: TradingView Lightweight Charts
- **后端**: FastAPI (Python)
- **构建**: Vite

---

## 1. Electron 主进程 (main.ts)

```typescript
import { app, BrowserWindow, ipcMain } from 'electron';
import { spawn, ChildProcess } from 'child_process';
import * as path from 'path';

let pythonService: ChildProcess | null = null;
let mainWindow: BrowserWindow | null = null;

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 400,
    height: 700,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  // 开发环境加载 Vite 开发服务器
  if (process.env.NODE_ENV === 'development') {
    mainWindow.loadURL('http://localhost:5173');
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'));
  }
}

function startPythonService(): void {
  // 启动 Python FastAPI 服务
  const pythonPath = process.env.PYTHON_PATH || 'python';
  pythonService = spawn(pythonPath, [
    path.join(__dirname, '../backend/api/main.py')
  ]);

  pythonService.stdout?.on('data', (data: Buffer) => {
    console.log(`Python服务: ${data.toString()}`);
  });

  pythonService.stderr?.on('data', (data: Buffer) => {
    console.error(`Python错误: ${data.toString()}`);
  });

  pythonService.on('close', (code) => {
    console.log(`Python服务退出，代码: ${code}`);
  });
}

app.whenReady().then(() => {
  startPythonService();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    if (pythonService) {
      pythonService.kill();
    }
    app.quit();
  }
});

// IPC 处理：打开新图表窗口
ipcMain.handle('open-chart-window', (event, code: string) => {
  const chartWindow = new BrowserWindow({
    width: 1200,
    height: 900,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  if (process.env.NODE_ENV === 'development') {
    chartWindow.loadURL(`http://localhost:5173/chart?code=${code}`);
  } else {
    chartWindow.loadFile(path.join(__dirname, '../dist/index.html'), {
      query: { code },
    });
  }

  return chartWindow.id;
});
```

---

## 2. 预加载脚本 (preload.ts)

```typescript
import { contextBridge, ipcRenderer } from 'electron';

// 定义 Electron API 类型
export interface ElectronAPI {
  openChartWindow: (code: string) => Promise<number>;
  getConfig: () => Promise<any>;
  saveConfig: (config: any) => Promise<void>;
}

// 暴露安全的 API 给渲染进程
contextBridge.exposeInMainWorld('electronAPI', {
  openChartWindow: (code: string) => ipcRenderer.invoke('open-chart-window', code),
  getConfig: () => ipcRenderer.invoke('get-config'),
  saveConfig: (config: any) => ipcRenderer.invoke('save-config', config),
} as ElectronAPI);

// 类型声明（在 src/types/electron.d.ts 中）
declare global {
  interface Window {
    electronAPI: ElectronAPI;
  }
}
```

---

## 3. Python 后端 API (backend/api/main.py)

```python
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import sys
from pathlib import Path

# 添加项目路径
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from configparser import ConfigParser
from data import get_kline_data
from signal_analysis import KD, MACD, RSI
from tools import EMA
from params_db import ParamsDB
from tools import code_in_futu_group
from ft_config import get_config

app = FastAPI()

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

config = get_config()

@app.get("/api/stocks/list")
async def get_stock_list():
    """获取股票列表"""
    try:
        group = config.get("CONFIG", "FUTU_GROUP", fallback='')
        host = config.get("CONFIG", "FUTU_HOST", fallback='127.0.0.1')
        port = config.getint("CONFIG", "FUTU_PORT", fallback=11111)
        
        stocks = []
        
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
        
        return JSONResponse(content={'stocks': stocks})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/kline/{code}")
async def get_kline(code: str, max_count: int = 1000):
    """获取K线数据"""
    try:
        df = get_kline_data(code, config, max_count)
        if df is None or df.empty:
            raise HTTPException(status_code=404, detail="未找到数据")
        
        # 转换为前端需要的格式
        df['time'] = df.index.astype(str)
        result = df[['time', 'open', 'high', 'low', 'close', 'volume']].to_dict('records')
        return JSONResponse(content={'data': result})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/indicators/{code}")
async def get_indicators(code: str):
    """获取所有技术指标"""
    try:
        # 获取K线数据
        df = get_kline_data(code, config, max_count=1000)
        if df is None or df.empty:
            raise HTTPException(status_code=404, detail="未找到数据")
        
        df['time'] = df.index
        
        # 计算 EMA
        ema_period = config.getint("CONFIG", "EMA_PERIOD", fallback=240)
        df[f'EMA_{ema_period}'] = EMA(df['close'], ema_period)
        
        result = {
            'time': df['time'].astype(str).tolist(),
            'ema': df[f'EMA_{ema_period}'].tolist(),
        }
        
        # 读取参数并计算指标
        db_paths = {
            'MACD': config.get("CONFIG", "MACD_PARAMS_DB", fallback=None),
            'KD': config.get("CONFIG", "KD_PARAMS_DB", fallback=None),
            'RSI': config.get("CONFIG", "RSI_PARAMS_DB", fallback=None),
        }
        
        # MACD
        if db_paths['MACD']:
            db = ParamsDB(db_paths['MACD'])
            macd_params = db.get_stock_params(code)
            if macd_params and macd_params.get('best_params'):
                macd = MACD()
                vmacd, signal = macd.indicator_calculate(df.copy(), macd_params['best_params'])
                result['macd'] = {
                    'vmacd': vmacd.tolist(),
                    'signal': signal.tolist(),
                    'hist': (vmacd - signal).tolist(),
                }
        
        # KD
        if db_paths['KD']:
            db = ParamsDB(db_paths['KD'])
            kd_params = db.get_stock_params(code)
            if kd_params and kd_params.get('best_params'):
                kd = KD()
                k, d = kd.indicator_calculate(df.copy(), kd_params['best_params'])
                result['kd'] = {
                    'k': k.tolist(),
                    'd': d.tolist(),
                    'oversold': kd_params['best_params'].get('oversold', 20),
                    'overbought': kd_params['best_params'].get('overbought', 80),
                }
        
        # RSI
        if db_paths['RSI']:
            db = ParamsDB(db_paths['RSI'])
            rsi_params = db.get_stock_params(code)
            if rsi_params and rsi_params.get('best_params'):
                rsi = RSI()
                rsi_values = rsi.indicator_calculate(df.copy(), rsi_params['best_params'])
                result['rsi'] = {
                    'values': rsi_values.tolist(),
                    'oversold': rsi_params['best_params'].get('oversold', 30),
                    'overbought': rsi_params['best_params'].get('overbought', 70),
                }
        
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

---

## 4. Vue 3 组件 - GroupList (src/components/GroupList.vue)

```vue
<template>
  <n-config-provider :theme="theme">
    <div class="group-list-container">
      <!-- 搜索框 -->
      <div class="search-box">
        <n-input
          v-model:value="searchTerm"
          placeholder="Search / Filter..."
          clearable
          @focus="handleSearchFocus"
        />
      </div>

      <!-- 股票列表 -->
      <n-data-table
        :columns="columns"
        :data="filteredStocks"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        @dblclick="handleDoubleClick"
        @row-keypress="handleKeyPress"
        class="stock-table"
      />
    </div>
  </n-config-provider>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { NConfigProvider, NInput, NDataTable, darkTheme, type DataTableColumns } from 'naive-ui';
import { useApi } from '@/composables/useApi';
import type { Stock } from '@/types/api';

// 主题配置（深色模式）
const theme = darkTheme;

// API 调用
const { stocks, loading, loadStocks } = useApi();

// 搜索关键词
const searchTerm = ref('');

// 过滤后的股票列表
const filteredStocks = computed(() => {
  if (!searchTerm.value) {
    return stocks.value;
  }
  const keyword = searchTerm.value.toLowerCase();
  return stocks.value.filter(
    (stock) =>
      stock.code.toLowerCase().includes(keyword) ||
      stock.name.toLowerCase().includes(keyword)
  );
});

// 表格列定义
const columns: DataTableColumns<Stock> = [
  { title: '代码', key: 'code', width: 100 },
  { title: '名称', key: 'name', width: 200 },
];

// 搜索框聚焦处理
const handleSearchFocus = (e: FocusEvent) => {
  const target = e.target as HTMLInputElement;
  if (target.placeholder === 'Search / Filter...') {
    target.placeholder = '';
  }
};

// 双击打开图表
const handleDoubleClick = (row: Stock) => {
  if (window.electronAPI) {
    window.electronAPI.openChartWindow(row.code);
  }
};

// 回车键打开图表
const handleKeyPress = (row: Stock, e: KeyboardEvent) => {
  if (e.key === 'Enter') {
    handleDoubleClick(row);
  }
};

// 加载数据
onMounted(() => {
  loadStocks();
});
</script>

<style scoped>
.group-list-container {
  width: 350px;
  height: 600px;
  display: flex;
  flex-direction: column;
  background-color: #191919;
  color: #ffffff;
}

.search-box {
  padding: 10px;
}

.stock-table {
  flex: 1;
  overflow-y: auto;
}
</style>
```

---

## 5. Vue 3 组件 - SignalWindow (src/components/SignalWindow.vue)

```vue
<template>
  <div class="signal-window">
    <div v-if="loading" class="loading-container">
      <n-spin size="large">
        <template #description>
          正在加载 {{ code }}...
        </template>
      </n-spin>
    </div>
    <div ref="chartContainer" class="chart-container"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue';
import { NSpin } from 'naive-ui';
import { createChart, IChartApi, ColorType } from 'lightweight-charts';
import { useChart } from '@/composables/useChart';

interface Props {
  code: string;
}

const props = defineProps<Props>();

const chartContainer = ref<HTMLDivElement>();
const chartRef = ref<IChartApi | null>(null);
const loading = ref(true);

const { loadChartData } = useChart();

onMounted(() => {
  if (!chartContainer.value) return;

  // 创建图表
  const chart = createChart(chartContainer.value, {
    layout: {
      background: { type: ColorType.Solid, color: '#191919' },
      textColor: '#ffffff',
    },
    width: chartContainer.value.clientWidth,
    height: chartContainer.value.clientHeight,
    grid: {
      vertLines: { color: '#333333' },
      horzLines: { color: '#333333' },
    },
  });

  chartRef.current = chart;

  // 加载数据
  loadChartData(chart, props.code).finally(() => {
    loading.value = false;
  });

  // 响应式调整
  const handleResize = () => {
    if (chartContainer.value && chart) {
      chart.applyOptions({
        width: chartContainer.value.clientWidth,
        height: chartContainer.value.clientHeight,
      });
    }
  };

  window.addEventListener('resize', handleResize);

  // 监听 code 变化，重新加载数据
  watch(() => props.code, (newCode) => {
    if (chart) {
      loading.value = true;
      loadChartData(chart, newCode).finally(() => {
        loading.value = false;
      });
    }
  });
});

onUnmounted(() => {
  if (chartRef.value) {
    chartRef.value.remove();
  }
});
</script>

<style scoped>
.signal-window {
  width: 100%;
  height: 100%;
  background-color: #191919;
  position: relative;
}

.chart-container {
  width: 100%;
  height: 100%;
}

.loading-container {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  color: #ffffff;
}
</style>
```

---

## 6. 后端 API 单文件实现 (backend/api.py)

```python
# backend/api.py - 所有 API 路由集中在一个文件
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import sys
from pathlib import Path
import pandas as pd

# 添加项目根目录到路径（复用现有代码）
sys.path.append(str(Path(__file__).resolve().parent.parent))

from ft_config import get_config
from data import get_kline_data
from signal_analysis import KD, MACD, RSI
from tools import EMA, code_in_futu_group
from params_db import ParamsDB

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"])

config = get_config()

@app.get("/api/stocks/list")
async def get_stock_list():
    """获取股票列表"""
    try:
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
        
        return JSONResponse(content={'stocks': stocks})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/kline/{code}")
async def get_kline(code: str, max_count: int = 1000):
    """获取K线数据"""
    try:
        df = get_kline_data(code, config, max_count)
        if df is None or df.empty:
            raise HTTPException(status_code=404, detail="未找到数据")
        
        df['time'] = df.index.astype(str)
        result = df[['time', 'open', 'high', 'low', 'close', 'volume']].to_dict('records')
        return JSONResponse(content={'data': result})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/indicators/{code}")
async def get_indicators(code: str):
    """获取技术指标"""
    try:
        df = get_kline_data(code, config, max_count=1000)
        if df is None or df.empty:
            raise HTTPException(status_code=404, detail="未找到数据")
        
        df['time'] = df.index.astype(str)
        result = {'time': df['time'].tolist()}
        
        # EMA
        ema_period = config.getint("CONFIG", "EMA_PERIOD", fallback=240)
        result['ema'] = EMA(df['close'], ema_period).tolist()
        
        # MACD
        macd_db_path = config.get("CONFIG", "MACD_PARAMS_DB", fallback=None)
        if macd_db_path:
            db = ParamsDB(macd_db_path)
            macd_params = db.get_stock_params(code)
            if macd_params and macd_params.get('best_params'):
                macd = MACD()
                vmacd, signal = macd.indicator_calculate(df.copy(), macd_params['best_params'])
                result['macd'] = {
                    'vmacd': vmacd.tolist(),
                    'signal': signal.tolist(),
                    'hist': (vmacd - signal).tolist(),
                }
        
        # KD
        kd_db_path = config.get("CONFIG", "KD_PARAMS_DB", fallback=None)
        if kd_db_path:
            db = ParamsDB(kd_db_path)
            kd_params = db.get_stock_params(code)
            if kd_params and kd_params.get('best_params'):
                kd = KD()
                k, d = kd.indicator_calculate(df.copy(), kd_params['best_params'])
                result['kd'] = {
                    'k': k.tolist(),
                    'd': d.tolist(),
                    'oversold': kd_params['best_params'].get('oversold', 20),
                    'overbought': kd_params['best_params'].get('overbought', 80),
                }
        
        # RSI
        rsi_db_path = config.get("CONFIG", "RSI_PARAMS_DB", fallback=None)
        if rsi_db_path:
            db = ParamsDB(rsi_db_path)
            rsi_params = db.get_stock_params(code)
            if rsi_params and rsi_params.get('best_params'):
                rsi = RSI()
                rsi_values = rsi.indicator_calculate(df.copy(), rsi_params['best_params'])
                result['rsi'] = {
                    'values': rsi_values.tolist(),
                    'oversold': rsi_params['best_params'].get('oversold', 30),
                    'overbought': rsi_params['best_params'].get('overbought', 70),
                }
        
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

---

## 8. package.json 示例

```json
{
  "name": "futu-trends-electron",
  "version": "1.0.0",
  "main": "electron/main.js",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "electron:dev": "concurrently \"npm run dev\" \"wait-on http://localhost:5173 && electron .\"",
    "electron:build": "npm run build && electron-builder",
    "backend:dev": "cd backend && uvicorn api.main:app --reload"
  },
  "dependencies": {
    "vue": "^3.4.0",
    "pinia": "^2.1.0",
    "naive-ui": "^2.38.0",
    "lightweight-charts": "^4.1.0",
    "axios": "^1.6.0",
    "electron-store": "^8.1.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5.0.0",
    "@types/node": "^20.0.0",
    "typescript": "^5.0.0",
    "vite": "^5.0.0",
    "electron": "^28.0.0",
    "electron-builder": "^24.0.0",
    "concurrently": "^8.2.0",
    "wait-on": "^7.0.0"
  }
}
```

## 7. 类型定义（内联在组件中）

第一版简化：类型定义直接写在组件文件中，不单独创建类型文件。

```typescript
// 在 GroupList.vue 中
interface Stock {
  code: string;
  name: string;
}

// 在 SignalWindow.vue 中
interface KlineData {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface Indicators {
  time?: string[];
  ema?: number[];
  macd?: {
    vmacd: number[];
    signal: number[];
    hist: number[];
  };
  // ...
}
```

**简化原则**: 第一版所有类型定义内联，后续可提取到单独文件。

---

## 使用说明

### 1. 安装依赖

```bash
# 前端依赖
npm install

# Python 后端依赖
pip install fastapi uvicorn
```

### 2. 启动开发环境

```bash
# 终端1: 启动 Python 后端（单文件）
python backend/api.py
# 或
cd backend && uvicorn api:app --reload

# 终端2: 启动 Vite 开发服务器
npm run dev

# 终端3: 启动 Electron
npm run electron:dev
```

### 3. 打包应用

```bash
# 构建前端
npm run build

# 打包 Electron 应用
npm run electron:build
```

## 第一版简化说明

### 文件结构对比

| 项目 | 原计划 | 简化版 | 说明 |
|------|--------|--------|------|
| 前端组件 | 2 个 | 2 个 | 保持不变 |
| API 封装 | 1 个文件 | 内联在组件 | 减少文件跳转 |
| 图表逻辑 | 1 个文件 | 内联在组件 | 减少文件跳转 |
| 类型定义 | 2 个文件 | 内联在组件 | 减少文件跳转 |
| 后端路由 | 3 个文件 | 1 个文件 | 集中管理 |
| **总计** | **15+ 文件** | **7 个文件** | **减少 50%+** |

### 简化优势

- ✅ **快速跑通**: 减少文件数量，降低复杂度
- ✅ **易于调试**: 代码集中，问题定位快
- ✅ **易于理解**: 逻辑清晰，无需跳转多个文件
- ✅ **后续优化**: 跑通后可逐步拆分重构

---

## 关键特性

### Vue 3 组合式 API
- ✅ 使用 `<script setup>` 语法，代码简洁
- ✅ 组合式函数 (`composables`) 实现逻辑复用
- ✅ TypeScript 类型安全

### Naive UI 组件
- ✅ 轻量级，按需引入
- ✅ 支持深色/浅色主题
- ✅ 组件丰富，覆盖常用场景

### TradingView Charts
- ✅ 高性能，支持大量数据
- ✅ 支持 K线、指标、子图
- ✅ 主题切换原生支持

### 后端 API
- ✅ FastAPI 自动生成 API 文档
- ✅ 复用现有 Python 代码
- ✅ 类型安全的 API 调用

---

这些示例代码展示了基于 Vue 3 + TypeScript 的 Electron 迁移核心实现，可以作为第一版开发的起点。

