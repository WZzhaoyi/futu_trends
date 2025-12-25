/**
 * 图表工具函数
 */
import { IChartApi, IPaneApi, LineSeries, HistogramSeries, Time } from 'lightweight-charts'
import { CHART_COLORS, CHART_STYLES } from '../services/config'
import { parseTime } from './time'
import type { IndicatorResult } from '../types'

/**
 * 创建超买超卖参考线
 * @param pane - 窗格对象
 * @param timeArray - 时间数组
 * @param overbought - 超买值
 * @param oversold - 超卖值
 * @param priceScaleId - 价格刻度ID
 */
function createOverboughtOversoldLines(
  pane: IPaneApi<Time>,
  timeArray: string[],
  overbought: number,
  oversold: number,
  priceScaleId: string
): void {
  // 超买线
  const overboughtSeries = pane.addSeries(LineSeries, {
    color: CHART_COLORS.RSI_OVERBOUGHT,
    lineWidth: 1,
    lineStyle: 2, // 虚线
    title: 'Overbought',
    priceScaleId
  })
  
  overboughtSeries.setData(
    timeArray.map((t) => ({
      time: parseTime(t) as Time,
      value: overbought
    }))
  )

  // 超卖线
  const oversoldSeries = pane.addSeries(LineSeries, {
    color: CHART_COLORS.RSI_OVERSOLD,
    lineWidth: 1,
    lineStyle: 2, // 虚线
    title: 'Oversold',
    priceScaleId
  })
  
  oversoldSeries.setData(
    timeArray.map((t) => ({
      time: parseTime(t) as Time,
      value: oversold
    }))
  )
}

/**
 * 创建 MACD 指标子图
 * @param chart - 图表对象
 * @param indicators - 指标数据
 * @param timeData - 时间数组
 */
export function createMacdPane(
  chart: IChartApi,
  indicators: IndicatorResult,
  timeData: string[]
): void {
  if (!indicators.macd || !indicators.signal) {
    return
  }

  // 创建副图窗格
  const macdPane = chart.addPane()
  macdPane.setStretchFactor(CHART_STYLES.PANE_STRETCH_FACTOR)

  // MACD 柱状图（histogram = MACD - Signal）
  const histSeries = macdPane.addSeries(HistogramSeries, {
    color: CHART_COLORS.MACD_HIST_BASE,
    priceFormat: {
      type: 'price',
      precision: 4,
      minMove: 0.0001
    },
    priceScaleId: 'macd'
  })

  histSeries.setData(
    timeData.map((t, i) => {
      const histValue = indicators.macd![i] - indicators.signal![i]
      const prevHistValue = i > 0 ? (indicators.macd![i - 1] - indicators.signal![i - 1]) : 0
      
      let color: string
      if (histValue >= 0 && histValue >= prevHistValue) {
        // 正值且上升：深红色
        color = CHART_COLORS.MACD_HIST_POSITIVE_UP
      } else if (histValue > 0 && histValue < prevHistValue) {
        // 正值但下降：浅红色
        color = CHART_COLORS.MACD_HIST_POSITIVE_DOWN
      } else if (histValue <= 0 && histValue >= prevHistValue) {
        // 负值但上升：浅绿色
        color = CHART_COLORS.MACD_HIST_NEGATIVE_UP
      } else {
        // 负值且下降：深绿色
        color = CHART_COLORS.MACD_HIST_NEGATIVE_DOWN
      }
      
      return {
        time: parseTime(t) as Time,
        value: histValue,
        color
      }
    })
  )

  // MACD 线
  const macdSeries = macdPane.addSeries(LineSeries, {
    color: CHART_COLORS.MACD_LINE,
    lineWidth: 1,
    title: 'MACD',
    priceScaleId: 'macd'
  })

  macdSeries.setData(
    timeData.map((t, i) => ({
      time: parseTime(t) as Time,
      value: indicators.macd![i]
    }))
  )

  // Signal 线
  const signalSeries = macdPane.addSeries(LineSeries, {
    color: CHART_COLORS.MACD_SIGNAL,
    lineWidth: 1,
    title: 'Signal',
    priceScaleId: 'macd'
  })

  signalSeries.setData(
    timeData.map((t, i) => ({
      time: parseTime(t) as Time,
      value: indicators.signal![i]
    }))
  )
}

/**
 * 创建 KD 指标子图
 * @param chart - 图表对象
 * @param indicators - 指标数据
 * @param timeData - 时间数组
 */
export function createKdPane(
  chart: IChartApi,
  indicators: IndicatorResult,
  timeData: string[]
): void {
  // 优先使用嵌套对象格式（包含超买超卖线）
  const kdData = indicators.kd
  if (!kdData || !kdData.k || !kdData.d) {
    return
  }

  // 创建副图窗格
  const kdPane = chart.addPane()
  kdPane.setStretchFactor(CHART_STYLES.PANE_STRETCH_FACTOR)

  // K 线
  const kSeries = kdPane.addSeries(LineSeries, {
    color: CHART_COLORS.KD_K,
    lineWidth: 1,
    title: 'K',
    priceScaleId: 'kd'
  })

  kSeries.setData(
    timeData.map((t, i) => ({
      time: parseTime(t) as Time,
      value: kdData.k[i]
    }))
  )

  // D 线
  const dSeries = kdPane.addSeries(LineSeries, {
    color: CHART_COLORS.KD_D,
    lineWidth: 1,
    title: 'D',
    priceScaleId: 'kd'
  })

  dSeries.setData(
    timeData.map((t, i) => ({
      time: parseTime(t) as Time,
      value: kdData.d[i]
    }))
  )

  // 添加超买超卖参考线（从数据库读取的参数）
  const oversold = kdData.oversold || 20
  const overbought = kdData.overbought || 80
  createOverboughtOversoldLines(kdPane, timeData, overbought, oversold, 'kd')
}

/**
 * 创建 RSI 指标子图
 * @param chart - 图表对象
 * @param indicators - 指标数据
 * @param timeData - 时间数组
 */
export function createRsiPane(
  chart: IChartApi,
  indicators: IndicatorResult,
  timeData: string[]
): void {
  // 优先使用嵌套对象格式（包含超买超卖线）
  const rsiData = indicators.rsi_indicator
  if (!rsiData || !rsiData.values) {
    return
  }

  // 创建副图窗格
  const rsiPane = chart.addPane()
  rsiPane.setStretchFactor(CHART_STYLES.PANE_STRETCH_FACTOR)

  // RSI 线
  const rsiSeries = rsiPane.addSeries(LineSeries, {
    color: CHART_COLORS.RSI,
    lineWidth: 1,
    title: 'RSI',
    priceScaleId: 'rsi'
  })

  rsiSeries.setData(
    timeData.map((t, i) => ({
      time: parseTime(t) as Time,
      value: rsiData.values[i]
    }))
  )

  // 添加超买超卖参考线（从数据库读取的参数）
  const oversold = rsiData.oversold || 30
  const overbought = rsiData.overbought || 70
  createOverboughtOversoldLines(rsiPane, timeData, overbought, oversold, 'rsi')
}
