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
            选择配置文件
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
          class="stock-table"
          :row-props="rowProps"
        />
      </div>
    </div>
  </n-config-provider>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { NConfigProvider, NInput, NDataTable, NAlert, NButton, NTooltip, darkTheme, type DataTableColumns } from 'naive-ui';
import { getStockList } from '../../../services/stockService';
import type { Stock } from '../../../types/chart';

// 主题配置（深色模式）
const theme = darkTheme;

// 状态
const stocks = ref<Stock[]>([]);
const searchTerm = ref('');
const loading = ref(false);
const errorMessage = ref<string | null>(null);
const reloadingConfig = ref(false);
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

// 选择配置文件
const handleSelectConfig = async () => {
  if (!isElectron || !window.electronAPI) {
    console.warn('[GroupList] Electron API not available');
    errorMessage.value = 'Electron API不可用';
    return;
  }

  reloadingConfig.value = true;
  errorMessage.value = null;

  try {
    console.log('[GroupList] Opening config file dialog...');
    const result = await window.electronAPI.selectConfigFile();

    if (!result) {
      // 用户取消了选择
      console.log('[GroupList] User cancelled config file selection');
      return;
    }

    console.log('[GroupList] Config file selected and loaded:', result.path);
    console.log('[GroupList] New config summary:', {
      DATA_SOURCE: result.config.DATA_SOURCE,
      FUTU_CODE_LIST: result.config.FUTU_CODE_LIST ? '已配置' : '未配置',
      FUTU_GROUP: result.config.FUTU_GROUP || '未配置',
      EMA_PERIOD: result.config.EMA_PERIOD || '使用默认值'
    });
    
    // 主进程已经在 select-config-file 中加载了新配置
    // 重新加载股票列表（会从主进程获取最新配置）
    console.log('[GroupList] Reloading stock list with new config...');
    await loadStocks();
    
    if (stocks.value.length === 0) {
      errorMessage.value = '配置已加载，但未获取到股票列表。请检查配置是否正确。';
    } else {
      console.log(`[GroupList] Successfully loaded ${stocks.value.length} stocks with new config`);
    }
  } catch (error) {
    console.error('[GroupList] Error selecting config file:', error);
    errorMessage.value = `选择配置文件失败: ${error instanceof Error ? error.message : '未知错误'}`;
  } finally {
    reloadingConfig.value = false;
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
  height: 100%;
  display: flex;
  flex-direction: column;
  background-color: #191919;
  color: #ffffff;
  overflow: hidden;
}

.search-box {
  padding: 10px;
  flex-shrink: 0;
  background-color: #191919;
  z-index: 10;
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
  scrollbar-color: rgba(255, 255, 255, 0.4) transparent;
}

.stock-table {
  height: 100%;
  user-select: none;
  -webkit-user-select: none;
  -moz-user-select: none;
  -ms-user-select: none;
}

/* Webkit 叠加滚动条 - hover时显示 */
.stock-table-wrapper::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

.stock-table-wrapper::-webkit-scrollbar-track {
  background: transparent;
}

.stock-table-wrapper::-webkit-scrollbar-thumb {
  background-color: transparent;
  border-radius: 4px;
  transition: background-color 0.3s ease;
}

.stock-table-wrapper:hover::-webkit-scrollbar-thumb {
  background-color: rgba(255, 255, 255, 0.4);
}

.table-row-clickable:hover {
  background-color: rgba(255, 255, 255, 0.1);
}

.table-row-disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
</style>
