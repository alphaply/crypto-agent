import React from 'react';
import ReactECharts from 'echarts-for-react';

export default function LineChart({ series = [], yName, xName, smooth = true, area = false }) {
  const option = {
    tooltip: {
      trigger: 'axis',
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
    },
    yAxis: {
      type: 'value',
      name: yName,
      scale: true,
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
