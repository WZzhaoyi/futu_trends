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
        <n-button type="primary" @click="loadStocks" :loading="loading" style="margin-top: 8px; width: 100%;">
          Refresh
        </n-button>
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
  </n-config-provider>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { NConfigProvider, NInput, NDataTable, NAlert, NButton, darkTheme, type DataTableColumns } from 'naive-ui';
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
  return {
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
  if (filteredStocks.value.length > 0) {
    handleDoubleClick(filteredStocks.value[0]);
  }
};

// 双击打开图表
const handleDoubleClick = (row: Stock) => {
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

.error-message {
  padding: 10px;
}

.stock-table {
  flex: 1;
  overflow-y: auto;
}
</style>

