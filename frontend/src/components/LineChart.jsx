import React from 'react';
import ReactECharts from 'echarts-for-react';
import { usePreferences } from '../app/preferences';

export default function LineChart({ series = [], yName, xName, smooth = true, area = false }) {
  const { isDark } = usePreferences();
  const option = {
    color: ['#2563eb', '#14b8a6', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4'],
    tooltip: {
      trigger: 'axis',
    },
    legend: {
      textStyle: {
        color: isDark ? '#dbeafe' : '#334155',
      },
    },
    grid: {
      left: 32,
      right: 24,
      top: 30,
      bottom: 28,
    },
    xAxis: {
      type: 'category',
      name: xName,
      data: series[0]?.data?.map((item) => item.name) || [],
      axisLine: { lineStyle: { color: isDark ? '#475569' : '#cbd5e1' } },
      axisLabel: { color: isDark ? '#cbd5e1' : '#475569' },
    },
    yAxis: {
      type: 'value',
      name: yName,
      scale: true,
      axisLine: { lineStyle: { color: isDark ? '#475569' : '#cbd5e1' } },
      axisLabel: { color: isDark ? '#cbd5e1' : '#475569' },
      splitLine: { lineStyle: { color: isDark ? 'rgba(148, 163, 184, 0.12)' : 'rgba(148, 163, 184, 0.18)' } },
    },
    series: series.map((item) => ({
      name: item.name,
      type: 'line',
      smooth,
      showSymbol: false,
      areaStyle: area ? {} : undefined,
      data: item.data.map((point) => point.value),
    })),
  };

  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} />;
}
