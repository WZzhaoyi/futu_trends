// 技术指标计算服务 - 已移至主进程
// 此文件保留用于类型导出

import type { KlineData, IndicatorResult } from '../types'

// 重新导出类型供兼容性
export type { KlineData, IndicatorResult }

/**
 * 注意：所有指标计算已移至主进程
 * 渲染进程通过 getChartData() 获取计算好的指标数据
 * 请使用 stockService.getChartData() 获取完整的图表数据
 */
