<template>
  <div class="signal-window">
    <div v-if="loading" class="loading-container">
      <n-spin size="large">
        <template #description>
          Loading {{ code }}...
        </template>
      </n-spin>
    </div>
    <div v-if="errorMessage" class="error-container">
      <n-alert type="error" :title="errorMessage" closable @close="errorMessage = null" />
    </div>
    <div ref="chartContainer" class="chart-container"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue';
import { NSpin } from 'naive-ui';
import { createChart, IChartApi, ColorType, LineSeries, CandlestickSeries, HistogramSeries } from 'lightweight-charts';
import axios from 'axios';
import { API_BASE, MAX_KLINE_COUNT, CHART_COLORS, CHART_STYLES } from './config';
import { parseTime } from './utils/time';
import { isValidCode } from './utils/validation';
import { getErrorMessage } from './utils/api';
import { createMacdPane, createKdPane, createRsiPane } from './utils/chart';
import type { KlineData, Indicators } from './types/chart';

interface Props {
  code: string | null;
}

const props = defineProps<Props>();

const chartContainer = ref<HTMLDivElement>();
const chartRef = ref<IChartApi | null>(null);
const loading = ref(true);
const errorMessage = ref<string | null>(null);

/**
 * 清除图表中的所有系列和副图窗格
 */
const clearChart = (chart: IChartApi) => {
  const panes = chart.panes();
  panes.forEach((pane) => {
    const seriesList = pane.getSeries();
    seriesList.forEach((series) => {
      chart.removeSeries(series);
    });
  });
  // 移除所有副图窗格（保留第一个主图窗格）
  for (let i = panes.length - 1; i > 0; i--) {
    chart.removePane(i);
  }
};

/**
 * 更新窗口标题
 */
const updateWindowTitle = (code: string | null) => {
  if (code && window.electronAPI) {
    window.electronAPI.setWindowTitle(code).catch((error) => {
      console.error('[SignalWindow] Failed to update window title:', error);
    });
  }
};

/**
 * 加载图表数据
 */
const loadChartData = async (chart: IChartApi, code: string | null) => {
  if (!isValidCode(code)) {
    throw new Error(`Invalid code: ${code}`);
  }
  
  try {
    // 加载K线数据
    const klineRes = await axios.get(`${API_BASE}/api/kline/${code}?max_count=${MAX_KLINE_COUNT}`);
    const klineData: KlineData[] = klineRes.data.data;
    
    if (!klineData || klineData.length === 0) {
      throw new Error('K-line data is empty');
    }

    // 创建K线图
    const candlestickSeries = chart.addSeries(CandlestickSeries, {
      upColor: CHART_COLORS.CANDLESTICK_UP,
      downColor: CHART_COLORS.CANDLESTICK_DOWN,
      borderVisible: false,
      wickUpColor: CHART_COLORS.CANDLESTICK_UP,
      wickDownColor: CHART_COLORS.CANDLESTICK_DOWN,
    });

    candlestickSeries.setData(
      klineData.map((d) => ({
        time: parseTime(d.time),
        open: d.open,
        high: d.high,
        low: d.low,
        close: d.close,
      }))
    );

    // 添加成交量柱状图
    const volumeSeries = chart.addSeries(HistogramSeries, {
      color: CHART_COLORS.VOLUME_BASE,
      priceFormat: {
        type: 'volume',
      },
      priceScaleId: 'volume',
    });

    chart.priceScale('volume').applyOptions({
      scaleMargins: {
        top: 0.8,
        bottom: 0.05,
      },
    });

    volumeSeries.setData(
      klineData.map((d) => ({
        time: parseTime(d.time),
        value: d.volume,
        color: d.close >= d.open ? CHART_COLORS.VOLUME_UP : CHART_COLORS.VOLUME_DOWN,
      }))
    );

    // 加载指标数据
    const indicatorsRes = await axios.get(`${API_BASE}/api/indicators/${code}`);
    const indicators: Indicators = indicatorsRes.data;

    if (!indicators.time || indicators.time.length === 0) {
      throw new Error('Indicator time data is empty');
    }

    // 添加 EMA（在主图窗格）
    if (indicators.ema) {
      const emaSeries = chart.addSeries(LineSeries, {
        color: CHART_COLORS.EMA,
        lineWidth: 1,
        title: 'EMA',
      });
      
      emaSeries.setData(
        indicators.time.map((t, i) => ({
          time: parseTime(t),
          value: indicators.ema![i],
        }))
      );
    }

    // 创建副图窗格
    createMacdPane(chart, indicators, indicators.time);
    createKdPane(chart, indicators, indicators.time);
    createRsiPane(chart, indicators, indicators.time);
    
  } catch (error) {
    const message = getErrorMessage(error);
    errorMessage.value = message;
    console.error('[loadChartData] Failed to load chart data:', error);
    throw error;
  }
};

onMounted(() => {
  if (!chartContainer.value) {
    console.error('[SignalWindow] chartContainer not found');
    loading.value = false;
    return;
  }

  if (!isValidCode(props.code)) {
    console.error('[SignalWindow] Invalid code:', props.code);
    loading.value = false;
    return;
  }

  // 创建图表
  const chart = createChart(chartContainer.value, {
    layout: {
      background: { type: ColorType.Solid, color: CHART_STYLES.BACKGROUND },
      textColor: CHART_STYLES.TEXT,
    },
    width: chartContainer.value.clientWidth,
    height: chartContainer.value.clientHeight,
    grid: {
      vertLines: { color: CHART_STYLES.GRID },
      horzLines: { color: CHART_STYLES.GRID },
    },
    timeScale: {
      timeVisible: true,
      secondsVisible: false,
    },
    rightPriceScale: {
      scaleMargins: {
        top: 0.1,
        bottom: 0.1,
      },
    },
  });

  chartRef.value = chart;

  // 更新窗口标题
  updateWindowTitle(props.code);

  // 加载数据
  loading.value = true;
  errorMessage.value = null;
  
  loadChartData(chart, props.code)
    .then(() => {
      console.log('[SignalWindow] Data loaded successfully');
    })
    .catch((error) => {
      console.error('[SignalWindow] Failed to load data:', error);
    })
    .finally(() => {
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
  watch(
    () => props.code,
    (newCode) => {
      const currentChart = chartRef.value;
      if (!currentChart || !isValidCode(newCode)) {
        return;
      }

      clearChart(currentChart);
      loading.value = true;
      errorMessage.value = null;
      
      // 更新窗口标题
      updateWindowTitle(newCode);
      
      loadChartData(currentChart, newCode)
        .then(() => {
          console.log('[SignalWindow] Data reloaded successfully');
        })
        .catch((error) => {
          console.error('[SignalWindow] Failed to reload data:', error);
        })
        .finally(() => {
          loading.value = false;
        });
    },
    { immediate: false }
  );
});

onUnmounted(() => {
  const currentChart = chartRef.value;
  if (currentChart) {
    currentChart.remove();
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
  z-index: 10;
}

.error-container {
  position: absolute;
  top: 10px;
  left: 10px;
  right: 10px;
  z-index: 10;
}
</style>

