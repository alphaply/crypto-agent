import React, { useMemo } from 'react';
import { Grid } from 'antd';
import ReactECharts from 'echarts-for-react';
import { usePreferences } from '../app/usePreferences';

function LineChart({
  series = [],
  yName,
  xName,
  smooth = true,
  area = false,
  valueFormatter,
  tooltipFormatter,
}) {
  const { isDark } = usePreferences();
  const screens = Grid.useBreakpoint();
  const isMobile = !screens.md;
  const option = useMemo(() => ({
    animation: false,
    color: ['#2563eb', '#14b8a6', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4'],
    tooltip: {
      trigger: 'axis',
      confine: true,
      formatter: tooltipFormatter,
    },
    legend: {
      type: 'scroll',
      top: isMobile ? 8 : 0,
      left: 0,
      right: 0,
      itemWidth: isMobile ? 12 : 16,
      itemHeight: isMobile ? 8 : 10,
      textStyle: {
        color: isDark ? '#dbeafe' : '#334155',
        fontSize: isMobile ? 11 : 12,
      },
    },
    grid: {
      left: isMobile ? 28 : 40,
      right: isMobile ? 12 : 24,
      top: isMobile ? 54 : 42,
      bottom: isMobile ? 52 : 36,
      containLabel: true,
    },
    xAxis: {
      type: 'time',
      name: xName,
      splitNumber: isMobile ? 3 : 6,
      axisLine: { lineStyle: { color: isDark ? '#475569' : '#cbd5e1' } },
      axisLabel: {
        color: isDark ? '#cbd5e1' : '#475569',
        hideOverlap: true,
        margin: isMobile ? 10 : 8,
        rotate: isMobile ? 24 : 0,
        fontSize: isMobile ? 11 : 12,
        formatter: (value) => {
          const date = new Date(value);
          if (Number.isNaN(date.getTime())) return value;
          return isMobile ? `${date.getMonth() + 1}/${date.getDate()}` : `${date.getMonth() + 1}-${date.getDate()}`;
        },
      },
    },
    yAxis: {
      type: 'value',
      name: yName,
      scale: true,
      axisLine: { lineStyle: { color: isDark ? '#475569' : '#cbd5e1' } },
      axisLabel: {
        color: isDark ? '#cbd5e1' : '#475569',
        fontSize: isMobile ? 11 : 12,
        formatter: valueFormatter,
      },
      splitLine: { lineStyle: { color: isDark ? 'rgba(148, 163, 184, 0.12)' : 'rgba(148, 163, 184, 0.18)' } },
    },
    series: series.map((item) => ({
      name: item.name,
      type: 'line',
      smooth,
      showSymbol: (item.data || []).length < 3,
      areaStyle: area ? {} : undefined,
      connectNulls: false,
      data: (item.data || [])
        .filter((point) => point?.name !== undefined && point?.value !== undefined && point?.value !== null)
        .map((point) => [point.name, point.value]),
    })),
  }), [area, isDark, isMobile, series, smooth, tooltipFormatter, valueFormatter, xName, yName]);

  return (
    <ReactECharts
      option={option}
      notMerge={false}
      replaceMerge={['series']}
      lazyUpdate
      style={{ height: '100%', width: '100%' }}
    />
  );
}

export default React.memo(LineChart);
