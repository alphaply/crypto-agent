import React from 'react';
import ReactECharts from 'echarts-for-react';

const EMA_COLORS = {
  '20': '#2563eb',
  '50': '#059669',
  '100': '#d97706',
  '200': '#dc2626',
};

export default function KlineChart({ payload }) {
  const candles = payload?.candles || [];
  const labels = candles.map((item) => new Date(item.time * 1000).toLocaleString());
  const candleSeries = candles.map((item) => [item.open, item.close, item.low, item.high]);

  const emaSeries = Object.entries(payload?.emas || {}).map(([span, values]) => ({
    name: `EMA ${span}`,
    type: 'line',
    smooth: true,
    showSymbol: false,
    lineStyle: { width: 1.5, color: EMA_COLORS[span] || '#475569' },
    data: labels.map((label, index) => {
      const candleTime = candles[index]?.time;
      const match = values.find((entry) => entry.time === candleTime);
      return match ? match.value : null;
    }),
  }));

  const riskLines = (payload?.risk_lines || []).map((line) => ({
    yAxis: line.price,
    label: { formatter: `${line.label}: ${line.price}` },
    lineStyle: {
      color: line.type === 'take_profit' ? '#16a34a' : '#dc2626',
      type: 'dashed',
    },
  }));

  const option = {
    tooltip: { trigger: 'axis' },
    legend: { top: 0 },
    grid: { left: 32, right: 24, top: 36, bottom: 28 },
    xAxis: { type: 'category', data: labels, scale: true, boundaryGap: false },
    yAxis: { scale: true },
    series: [
      {
        name: 'Kline',
        type: 'candlestick',
        data: candleSeries,
        markLine: riskLines.length ? { symbol: ['none', 'none'], data: riskLines } : undefined,
      },
      ...emaSeries,
    ],
  };

  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} />;
}
