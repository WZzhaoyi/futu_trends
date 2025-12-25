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
import { NSpin, NAlert } from 'naive-ui';
import { createChart, IChartApi, ColorType, LineSeries, CandlestickSeries, HistogramSeries } from 'lightweight-charts';
import { getChartData } from '../../../services/stockService';
import { parseTime } from '../../../utils/time';
import { createMacdPane, createKdPane, createRsiPane } from '../../../utils/chart';
import { CHART_COLORS, CHART_STYLES } from '../../../services/config';
import { Time } from 'lightweight-charts';

interface Props {
  code: string | null;
}

const props = defineProps<Props>();

const chartContainer = ref<HTMLDivElement>();
const chartRef = ref<IChartApi | null>(null);
const loading = ref(true);
const errorMessage = ref<string | null>(null);

/**
 * 更新窗口标题
 */
const updateWindowTitle = (code: string | null) => {
  if (code && window.electronAPI) {
    window.electronAPI.setWindowTitle(code).catch((error: any) => {
      console.error('[SignalWindow] Failed to update window title:', error);
    });
  }
};

/**
 * 加载图表数据（所有数据和指标计算都在主进程完成）
 */
const loadChartData = async (chart: IChartApi, code: string | null) => {
  if (!code) {
    throw new Error('Stock code is required');
  }

  try {
    // 从主进程获取完整的图表数据（包含K线和所有指标）
    console.log('[SignalWindow] Fetching chart data for:', code);
    const chartData = await getChartData(code);

    if (!chartData.klines || chartData.klines.length === 0) {
      throw new Error('K-line data is empty');
    }

    console.log('[SignalWindow] Chart data received:', {
      stock: chartData.stock.code,
      klines: chartData.klines.length,
      indicators: Object.keys(chartData.indicators),
      sampleKline: chartData.klines[0]
    });

    // 验证K线数据格式
    const invalidKlines = chartData.klines.filter(k => !k.time || k.time === '');
    if (invalidKlines.length > 0) {
      console.error('[SignalWindow] Found invalid klines:', invalidKlines);
      throw new Error(`${invalidKlines.length} klines have invalid time field`);
    }

    // 创建K线图
    const candlestickSeries = chart.addSeries(CandlestickSeries, {
      upColor: CHART_COLORS.CANDLESTICK_UP,
      downColor: CHART_COLORS.CANDLESTICK_DOWN,
      borderVisible: false,
      wickUpColor: CHART_COLORS.CANDLESTICK_UP,
      wickDownColor: CHART_COLORS.CANDLESTICK_DOWN,
    });

    // 转换K线数据并添加错误处理
    const candleData = chartData.klines.map((d, index) => {
      try {
        const parsedTime = parseTime(d.time);
        return {
          time: parsedTime as Time,
          open: d.open,
          high: d.high,
          low: d.low,
          close: d.close,
        };
      } catch (error) {
        console.error(`[SignalWindow] Failed to parse time at index ${index}:`, d.time, error);
        throw error;
      }
    });
    
    candlestickSeries.setData(candleData);

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
      chartData.klines.map((d) => ({
        time: parseTime(d.time) as Time,
        value: d.volume,
        color: d.close >= d.open ? CHART_COLORS.VOLUME_UP : CHART_COLORS.VOLUME_DOWN,
      }))
    );

    // 添加 EMA 指标线（在主图窗格）
    const { indicators } = chartData;
    const { time } = indicators;

    if (indicators.ema) {
      const emaSeries = chart.addSeries(LineSeries, {
        color: CHART_COLORS.EMA,
        lineWidth: 1,
        title: 'EMA',
      });
      
      emaSeries.setData(
        time.map((t, i) => ({
          time: parseTime(t) as Time,
          value: indicators.ema![i],
        }))
      );
    }

    // 创建副图窗格显示其他指标
    createMacdPane(chart, indicators, time);
    createKdPane(chart, indicators, time);
    createRsiPane(chart, indicators, time);

    console.log('[SignalWindow] Chart rendered successfully');
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Failed to load chart data';
    errorMessage.value = message;
    console.error('[SignalWindow] Failed to load chart data:', error);
    throw error;
  }
};

onMounted(() => {
  if (!chartContainer.value) {
    console.error('[SignalWindow] chartContainer not found');
    loading.value = false;
    return;
  }

  if (!props.code) {
    console.error('[SignalWindow] No code provided');
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

  // 清理函数
  onUnmounted(() => {
    window.removeEventListener('resize', handleResize);
    if (chart) {
      chart.remove();
    }
  });
});

// 监听 code 变化，重新加载数据
watch(
  () => props.code,
  (newCode) => {
    if (!newCode || !chartContainer.value) {
      return;
    }

    // 移除旧图表，创建新图表
    if (chartRef.value) {
      chartRef.value.remove();
      chartRef.value = null;
    }

    // 创建新图表
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
    loading.value = true;
    errorMessage.value = null;

    // 更新窗口标题
    updateWindowTitle(newCode);

    // 加载新数据
    loadChartData(chart, newCode)
      .then(() => {
        console.log('[SignalWindow] Data reloaded successfully');
      })
      .catch((error) => {
        console.error('[SignalWindow] Failed to reload data:', error);
      })
      .finally(() => {
        loading.value = false;
      });
  }
);
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
