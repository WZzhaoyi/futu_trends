<template>
  <n-config-provider :theme="theme">
    <div class="group-list-container">
      <!-- 搜索框和刷新按钮 -->
      <div class="search-box">
        <n-input
          v-model:value="searchTerm"
          placeholder="Search / Filter..."
          clearable
          @focus="handleSearchFocus"
          @keyup.enter="handleEnterKey"
        />
        <!-- 按钮组：横向并排 -->
        <div class="button-group">
          <n-tooltip trigger="hover">
            <template #trigger>
              <n-button
                type="primary"
                @click="loadStocks()"
                :loading="loading"
                class="action-button"
              >
                <template #icon>
                  <span class="icon">↻</span>
                </template>
              </n-button>
            </template>
            刷新股票列表
          </n-tooltip>
          <n-tooltip v-if="isElectron" trigger="hover">
            <template #trigger>
              <n-button
                type="default"
                @click="handleSelectConfig"
                :loading="reloadingConfig"
                class="action-button"
              >
                <template #icon>
                  <span class="icon">⚙️</span>
                </template>
              </n-button>
            </template>
            配置管理
          </n-tooltip>
          <n-tooltip v-if="isElectron" trigger="hover">
            <template #trigger>
              <n-button
                type="default"
                @click="handleOpenLogDir"
                class="action-button"
              >
                <template #icon>
                  <span class="icon">📋</span>
                </template>
              </n-button>
            </template>
            打开日志目录
          </n-tooltip>
        </div>
      </div>

      <!-- 错误提示 -->
      <div v-if="errorMessage" class="error-message">
        <n-alert type="error" :title="errorMessage" closable @close="errorMessage = null">
          <template #default>
            {{ errorMessage }}
            <n-button size="small" @click="loadStocks()" style="margin-top: 8px;">
              Retry
            </n-button>
          </template>
        </n-alert>
      </div>

      <!-- 股票列表 -->
      <div class="stock-table-wrapper">
        <n-data-table
          :columns="columns"
          :data="filteredStocks"
          :loading="loading"
          :bordered="false"
          :single-line="false"
          :max-height="'100%'"
          class="stock-table"
          :row-props="rowProps"
        />
      </div>
    </div>

    <!-- 配置对话框 -->
    <ConfigDialog
      v-model:show="showConfigDialog"
      @save="handleSaveConfig"
    />
  </n-config-provider>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { NConfigProvider, NInput, NDataTable, NAlert, NButton, NTooltip, darkTheme, useMessage, type DataTableColumns } from 'naive-ui';
import { getStockList } from '../../../services/stockService';
import type { Stock } from '../../../types/chart';
import ConfigDialog from './ConfigDialog.vue';

// 主题配置（深色模式）
const theme = darkTheme;
const message = useMessage();

// 状态
const stocks = ref<Stock[]>([]);
const searchTerm = ref('');
const loading = ref(false);
const errorMessage = ref<string | null>(null);
const reloadingConfig = ref(false);
const showConfigDialog = ref(false);
const isElectron = typeof window !== 'undefined' && window.electronAPI !== undefined;

/**
 * 加载股票列表
 */
const loadStocks = async () => {
  loading.value = true;
  errorMessage.value = null;

  try {
    const stockList = await getStockList();
    stocks.value = stockList;
    errorMessage.value = null;
    console.log(`[GroupList] Stock list loaded successfully, ${stocks.value.length} stocks`);
  } catch (error) {
    console.error('[GroupList] Failed to load stock list:', error);
    errorMessage.value = `Failed to load stocks: ${error instanceof Error ? error.message : 'Unknown error'}`;
    stocks.value = [];
  } finally {
    loading.value = false;
  }
};

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

// 市场显示名称映射
const marketNames: Record<string, string> = {
  'SH': '沪市',
  'SZ': '深市',
  'HK': '港股',
  'US': '美股'
};

// 表格列定义
const columns: DataTableColumns<Stock> = [
  { 
    title: '市场', 
    key: 'market', 
    width: 60,
    render: (row: Stock) => marketNames[row.market] || row.market
  },
  { title: '代码', key: 'code', width: 120 },
  { title: '名称', key: 'name', width: 200 },
];

// 行属性（支持回车键和双击）
const rowProps = (row: Stock) => {
  if (loading.value) {
    return {
      class: 'table-row-disabled',
      tabindex: -1,
    };
  }

  return {
    class: 'table-row-clickable',
    onDblclick: () => {
      handleDoubleClick(row);
    },
    onKeydown: (e: KeyboardEvent) => {
      if (e.key === 'Enter') {
        handleDoubleClick(row);
      }
    },
    tabindex: 0,
  };
};

// 搜索框聚焦处理
const handleSearchFocus = (e: FocusEvent) => {
  const target = e.target as HTMLInputElement;
  if (target.placeholder === 'Search / Filter...') {
    target.placeholder = '';
  }
};

// 回车键处理（在搜索框时）
const handleEnterKey = () => {
  if (loading.value) {
    return;
  }
  if (filteredStocks.value.length > 0) {
    handleDoubleClick(filteredStocks.value[0]);
  }
};

// 双击打开图表
const handleDoubleClick = (row: Stock) => {
  if (loading.value) {
    return;
  }

  console.log('[GroupList.vue] Double-click to open chart, stock code:', row.code);

  // 检查是否在 Electron 环境中
  if (window.electronAPI && typeof window.electronAPI.openChartWindow === 'function') {
    // Electron 环境：通过 IPC 打开新窗口
    console.log('[GroupList.vue] Using Electron API to open new window');
    window.electronAPI.openChartWindow(row.code)
      .then((windowId: any) => {
        console.log(`[GroupList.vue] Chart window opened, window ID: ${windowId}`);
      })
      .catch((error: any) => {
        console.error('[GroupList.vue] Failed to open chart window:', error);
        // 失败时使用备用方案：在当前窗口打开
        window.location.href = `?code=${encodeURIComponent(row.code)}`;
      });
  } else {
    // 非 Electron 环境（开发模式或浏览器）：在当前窗口打开
    console.log('[GroupList.vue] Non-Electron environment, opening chart in current window');
    window.location.href = `?code=${encodeURIComponent(row.code)}`;
  }
};

// 打开配置对话框
const handleSelectConfig = () => {
  if (!isElectron || !window.electronAPI) {
    console.warn('[GroupList] Electron API not available');
    errorMessage.value = 'Electron API不可用';
    return;
  }

  showConfigDialog.value = true;
};

// 保存配置
const handleSaveConfig = async (config: any) => {
  if (!isElectron || !window.electronAPI) {
    console.warn('[GroupList] Electron API not available');
    return;
  }

  reloadingConfig.value = true;
  errorMessage.value = null;

  try {
    // 将响应式对象转换为纯 JavaScript 对象
    const plainConfig = {
      DATA_SOURCE: config.DATA_SOURCE,
      FUTU_HOST: config.FUTU_HOST,
      FUTU_PORT: config.FUTU_PORT,
      FUTU_WS_PORT: config.FUTU_WS_PORT,
      FUTU_WS_KEY: config.FUTU_WS_KEY,
      FUTU_GROUP: config.FUTU_GROUP,
      FUTU_CODE_LIST: config.FUTU_CODE_LIST,
      FUTU_PUSH_TYPE: config.FUTU_PUSH_TYPE,
      AKTOOLS_HOST: config.AKTOOLS_HOST,
      AKTOOLS_PORT: config.AKTOOLS_PORT,
      EMA_PERIOD: config.EMA_PERIOD,
      KD_PARAMS_DB: config.KD_PARAMS_DB,
      MACD_PARAMS_DB: config.MACD_PARAMS_DB,
      RSI_PARAMS_DB: config.RSI_PARAMS_DB,
      PROXY: config.PROXY,
      DATA_DIR: config.DATA_DIR,
      DARK_MODE: config.DARK_MODE
    };
    
    console.log('[GroupList] Saving config:', plainConfig);
    
    // 保存配置到主进程
    if (typeof window.electronAPI.saveConfig === 'function') {
      await window.electronAPI.saveConfig(plainConfig);
      message.success('配置已保存');
      console.log('[GroupList] Config saved successfully');
      
      // 关闭对话框
      showConfigDialog.value = false;
      
      // 重新加载股票列表
      console.log('[GroupList] Reloading stock list with new config...');
      await loadStocks();
      
      if (stocks.value.length === 0) {
        errorMessage.value = '配置已保存，但未获取到股票列表。请检查配置是否正确。';
      } else {
        message.success(`成功加载 ${stocks.value.length} 只股票`);
        console.log(`[GroupList] Successfully loaded ${stocks.value.length} stocks with new config`);
      }
    } else {
      throw new Error('saveConfig API not available');
    }
  } catch (error) {
    console.error('[GroupList] Error saving config:', error);
    errorMessage.value = `保存配置失败: ${error instanceof Error ? error.message : '未知错误'}`;
    message.error('保存配置失败');
  } finally {
    reloadingConfig.value = false;
  }
};

// 打开日志目录
const handleOpenLogDir = async () => {
  if (!isElectron || !window.electronAPI) {
    console.warn('[GroupList] Electron API not available');
    return;
  }

  try {
    console.log('[GroupList] Opening log directory...');
    const logDir = await window.electronAPI.openLogDir();
    console.log('[GroupList] Log directory opened:', logDir);
  } catch (error) {
    console.error('[GroupList] Failed to open log directory:', error);
    errorMessage.value = `打开日志目录失败: ${error instanceof Error ? error.message : '未知错误'}`;
  }
};

// 加载数据
onMounted(() => {
  // 直接加载股票列表（会自动从主进程获取配置）
  loadStocks();
});
</script>

<style scoped>
.group-list-container {
  width: 100%;
  height: 100vh;
  display: flex;
  flex-direction: column;
  background-color: #191919;
  color: #ffffff;
}

.search-box {
  padding: 10px;
  flex-shrink: 0;
}

.button-group {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.action-button {
  flex: 1;
}

.action-button .icon {
  font-size: 18px;
  line-height: 1;
}

.error-message {
  padding: 10px;
  flex-shrink: 0;
}

.stock-table-wrapper {
  flex: 1;
  overflow: auto;
  min-height: 0;
  /* 叠加滚动条 - Firefox */
  scrollbar-width: thin;
  scrollbar-color: rgba(255, 255, 255, 0.4) rgba(255, 255, 255, 0.1);
}

.stock-table {
  height: 100%;
  user-select: none;
  -webkit-user-select: none;
  -moz-user-select: none;
  -ms-user-select: none;
}

/* Webkit 滚动条 - 始终可见 */
.stock-table-wrapper::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}

.stock-table-wrapper::-webkit-scrollbar-track {
  background: rgba(255, 255, 255, 0.05);
  border-radius: 5px;
}

.stock-table-wrapper::-webkit-scrollbar-thumb {
  background-color: rgba(255, 255, 255, 0.3);
  border-radius: 5px;
  border: 2px solid rgba(25, 25, 25, 1);
  transition: background-color 0.2s ease;
}

.stock-table-wrapper::-webkit-scrollbar-thumb:hover {
  background-color: rgba(255, 255, 255, 0.5);
}

.stock-table-wrapper::-webkit-scrollbar-thumb:active {
  background-color: rgba(255, 255, 255, 0.6);
}

.table-row-clickable:hover {
  background-color: rgba(255, 255, 255, 0.1);
}

.table-row-disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
</style>
