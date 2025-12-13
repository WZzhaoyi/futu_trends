/**
 * 图表工具函数
 */
import { IChartApi, IPaneApi, LineSeries, HistogramSeries, Time } from 'lightweight-charts';
import { CHART_COLORS, CHART_STYLES } from '../config';
import { parseTime } from './time';
import type { Indicators } from '../types/chart';

/**
 * 创建超买超卖参考线
 * @param pane - 窗格对象
 * @param timeArray - 时间数组
 * @param overbought - 超买值
 * @param oversold - 超卖值
 * @param priceScaleId - 价格刻度ID
 */
export function createOverboughtOversoldLines(
  pane: IPaneApi<Time>,
  timeArray: string[],
  overbought: number,
  oversold: number,
  priceScaleId: string
): void {
  // 超买线
  const overboughtSeries = pane.addSeries(LineSeries, {
    color: CHART_COLORS.KD_OVERBOUGHT,
    lineWidth: 1,
    lineStyle: 2, // 虚线
    title: 'Overbought',
    priceScaleId,
  });
  
  overboughtSeries.setData(
    timeArray.map((t) => ({
      time: parseTime(t),
      value: overbought,
    }))
  );

  // 超卖线
  const oversoldSeries = pane.addSeries(LineSeries, {
    color: CHART_COLORS.KD_OVERSOLD,
    lineWidth: 1,
    lineStyle: 2, // 虚线
    title: 'Oversold',
    priceScaleId,
  });
  
  oversoldSeries.setData(
    timeArray.map((t) => ({
      time: parseTime(t),
      value: oversold,
    }))
  );
}

/**
 * 创建 MACD 指标子图
 * @param chart - 图表对象
 * @param indicators - 指标数据
 * @param timeArray - 时间数组
 */
export function createMacdPane(
  chart: IChartApi,
  indicators: Indicators,
  timeArray: string[]
): void {
  if (!indicators.macd) return;

  const pane = chart.addPane();
  pane.setStretchFactor(CHART_STYLES.PANE_STRETCH_FACTOR);

  // MACD 柱状图
  const histSeries = pane.addSeries(HistogramSeries, {
    color: CHART_COLORS.MACD_HIST_BASE,
    priceFormat: {
      type: 'price',
      precision: 2,
      minMove: 0.01,
    },
    priceScaleId: 'macd',
  });

  histSeries.setData(
    timeArray.map((t, i) => {
      const histValue = indicators.macd!.hist[i] ?? 0;
      const prevValue = i > 0 ? (indicators.macd!.hist[i - 1] ?? 0) : 0;
      
      let color: string;
      if (histValue >= 0 && histValue >= prevValue) {
        // 正值且上升：深红色
        color = CHART_COLORS.MACD_HIST_POSITIVE_UP;
      } else if (histValue > 0 && histValue < prevValue) {
        // 正值但下降：浅红色
        color = CHART_COLORS.MACD_HIST_POSITIVE_DOWN;
      } else if (histValue <= 0 && histValue >= prevValue) {
        // 负值但上升：浅绿色
        color = CHART_COLORS.MACD_HIST_NEGATIVE_UP;
      } else {
        // 负值且下降：深绿色
        color = CHART_COLORS.MACD_HIST_NEGATIVE_DOWN;
      }
      
      return {
        time: parseTime(t),
        value: histValue*2,
        color,
      };
    })
  );

  // MACD 线
  const macdSeries = pane.addSeries(LineSeries, {
    color: CHART_COLORS.MACD_LINE,
    lineWidth: 1,
    title: 'MACD',
    priceScaleId: 'macd',
  });
  
  macdSeries.setData(
    timeArray.map((t, i) => ({
      time: parseTime(t),
      value: indicators.macd!.vmacd[i],
    }))
  );

  // Signal 线
  const signalSeries = pane.addSeries(LineSeries, {
    color: CHART_COLORS.MACD_SIGNAL,
    lineWidth: 1,
    title: 'Signal',
    priceScaleId: 'macd',
  });
  
  signalSeries.setData(
    timeArray.map((t, i) => ({
      time: parseTime(t),
      value: indicators.macd!.signal[i],
    }))
  );
}

/**
 * 创建 KD 指标子图
 * @param chart - 图表对象
 * @param indicators - 指标数据
 * @param timeArray - 时间数组
 */
export function createKdPane(
  chart: IChartApi,
  indicators: Indicators,
  timeArray: string[]
): void {
  if (!indicators.kd) return;

  const pane = chart.addPane();
  pane.setStretchFactor(CHART_STYLES.PANE_STRETCH_FACTOR);

  // K 线
  const kSeries = pane.addSeries(LineSeries, {
    color: CHART_COLORS.KD_K,
    lineWidth: 1,
    title: 'K',
    priceScaleId: 'kd',
  });
  
  kSeries.setData(
    timeArray.map((t, i) => ({
      time: parseTime(t),
      value: indicators.kd!.k[i],
    }))
  );

  // D 线
  const dSeries = pane.addSeries(LineSeries, {
    color: CHART_COLORS.KD_D,
    lineWidth: 1,
    title: 'D',
    priceScaleId: 'kd',
  });
  
  dSeries.setData(
    timeArray.map((t, i) => ({
      time: parseTime(t),
      value: indicators.kd!.d[i],
    }))
  );

  // 超买超卖线
  createOverboughtOversoldLines(
    pane,
    timeArray,
    indicators.kd.overbought,
    indicators.kd.oversold,
    'kd'
  );
}

/**
 * 创建 RSI 指标子图
 * @param chart - 图表对象
 * @param indicators - 指标数据
 * @param timeArray - 时间数组
 */
export function createRsiPane(
  chart: IChartApi,
  indicators: Indicators,
  timeArray: string[]
): void {
  if (!indicators.rsi) return;

  const pane = chart.addPane();
  pane.setStretchFactor(CHART_STYLES.PANE_STRETCH_FACTOR);

  // RSI 线
  const rsiSeries = pane.addSeries(LineSeries, {
    color: CHART_COLORS.RSI,
    lineWidth: 1,
    title: 'RSI',
    priceScaleId: 'rsi',
  });
  
  rsiSeries.setData(
    timeArray.map((t, i) => ({
      time: parseTime(t),
      value: indicators.rsi!.values[i],
    }))
  );

  // 超买超卖线
  createOverboughtOversoldLines(
    pane,
    timeArray,
    indicators.rsi.overbought,
    indicators.rsi.oversold,
    'rsi'
  );
}

