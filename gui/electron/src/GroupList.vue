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
                @click="loadStocks" 
                :loading="loading"
                :disabled="loading || restarting"
                class="action-button"
              >
                <template #icon>
                  <span class="icon">↻</span>
                </template>
              </n-button>
            </template>
            刷新股票列表
          </n-tooltip>
          <!-- 配置文件选择按钮 -->
          <n-tooltip v-if="isElectron" trigger="hover">
            <template #trigger>
              <n-button 
                type="default" 
                @click="handleSelectConfig" 
                :loading="restarting"
                :disabled="loading || restarting"
                class="action-button"
              >
                <template #icon>
                  <span class="icon">⚙️</span>
                </template>
              </n-button>
            </template>
            选择配置文件并重启后端
          </n-tooltip>
        </div>
      </div>

      <!-- 错误提示 -->
      <div v-if="errorMessage" class="error-message">
        <n-alert type="error" :title="errorMessage" closable @close="errorMessage = null">
          <template #default>
            {{ errorMessage }}
            <n-button size="small" @click="loadStocks" style="margin-top: 8px;">
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
import axios from 'axios';
import { API_BASE } from './config';
import { checkServiceReady, requestWithRetry, getErrorMessage } from './utils/api';
import type { Stock } from './types/chart';

// 主题配置（深色模式）
const theme = darkTheme;

// 状态
const stocks = ref<Stock[]>([]);
const searchTerm = ref('');
const loading = ref(false);
const errorMessage = ref<string | null>(null);
const restarting = ref(false);
const isElectron = typeof window !== 'undefined' && window.electronAPI !== undefined;

/**
 * 加载股票列表
 */
const loadStocks = async () => {
  loading.value = true;
  errorMessage.value = null;
  
  try {
    // 检查服务是否就绪
    const isReady = await checkServiceReady();
    if (!isReady) {
      errorMessage.value = 'Backend service is not ready, please wait...';
    }
    
    // 加载股票列表（带重试机制）
    const res = await requestWithRetry(async () => {
      return await axios.get<{ stocks: Stock[] }>(`${API_BASE}/api/stocks/list`, { timeout: 5000 });
    });
    
    stocks.value = res.data.stocks || [];
    errorMessage.value = null;
    console.log(`[GroupList] Stock list loaded successfully, ${stocks.value.length} stocks`);
  } catch (error) {
    console.error('[GroupList] Failed to load stock list:', error);
    errorMessage.value = getErrorMessage(error);
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

// 表格列定义
const columns: DataTableColumns<Stock> = [
  { title: '代码', key: 'code', width: 100 },
  { title: '名称', key: 'name', width: 200 },
];

// 行属性（支持回车键和双击）
const rowProps = (row: Stock) => {
  // 如果正在加载或重启，禁用点击
  if (loading.value || restarting.value) {
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
  // 如果正在加载或重启，不处理
  if (loading.value || restarting.value) {
    return;
  }
  if (filteredStocks.value.length > 0) {
    handleDoubleClick(filteredStocks.value[0]);
  }
};

// 双击打开图表
const handleDoubleClick = (row: Stock) => {
  // 如果正在加载或重启，不处理
  if (loading.value || restarting.value) {
    return;
  }
  
  console.log('[GroupList.vue] Double-click to open chart, stock code:', row.code);
  
  // 检查是否在 Electron 环境中
  if (window.electronAPI && typeof window.electronAPI.openChartWindow === 'function') {
    // Electron 环境：通过 IPC 打开新窗口
    console.log('[GroupList.vue] Using Electron API to open new window');
    window.electronAPI.openChartWindow(row.code)
      .then((windowId) => {
        console.log(`[GroupList.vue] Chart window opened, window ID: ${windowId}`);
      })
      .catch((error) => {
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

// 选择配置文件并重启后端
const handleSelectConfig = async () => {
  if (!isElectron || !window.electronAPI) {
    console.warn('[GroupList] Electron API not available');
    return;
  }
  
  restarting.value = true;
  errorMessage.value = null;
  
  try {
    // 选择配置文件
    const configPath = await window.electronAPI.selectConfigFile();
    
    if (!configPath) {
      // 用户取消了选择
      restarting.value = false;
      return;
    }
    
    console.log('[GroupList] Config file selected:', configPath);
    
    // 重启后端服务
    const result = await window.electronAPI.restartBackend(configPath);
    
    if (result.success) {
      console.log('[GroupList] Backend restarted successfully');
      // 等待服务就绪后重新加载股票列表
      setTimeout(() => {
        loadStocks().finally(() => {
          restarting.value = false;
        });
      }, 2000);
    } else {
      errorMessage.value = result.message || '重启后端失败';
      console.error('[GroupList] Failed to restart backend:', result.message);
      restarting.value = false;
    }
  } catch (error) {
    console.error('[GroupList] Error selecting config or restarting backend:', error);
    errorMessage.value = `操作失败: ${error instanceof Error ? error.message : '未知错误'}`;
    restarting.value = false;
  }
};

// 加载数据
onMounted(() => {
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
}

.stock-table-wrapper {
  flex: 1;
  overflow: auto;
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
</style>

