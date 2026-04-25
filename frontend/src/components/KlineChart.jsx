import React, { useEffect, useRef } from 'react';
import {
  CandlestickSeries,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
} from 'lightweight-charts';
import { Empty } from 'antd';
import { usePreferences } from '../app/preferences';

const EMA_COLORS = {
  '20': '#2563eb',
  '50': '#14b8a6',
  '100': '#f59e0b',
  '200': '#ef4444',
};

function asChartTime(unixSeconds) {
  return Number(unixSeconds);
}

export default function KlineChart({ payload }) {
  const containerRef = useRef(null);
  const { isDark, t } = usePreferences();

  useEffect(() => {
    if (!containerRef.current || !payload?.candles?.length) {
      return undefined;
    }

    const container = containerRef.current;
    const chart = createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight || 420,
      layout: {
        background: { type: 'solid', color: isDark ? '#08111f' : '#f8fbff' },
        textColor: isDark ? '#dbeafe' : '#1e293b',
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: isDark ? 'rgba(148, 163, 184, 0.08)' : 'rgba(148, 163, 184, 0.15)' },
        horzLines: { color: isDark ? 'rgba(148, 163, 184, 0.08)' : 'rgba(148, 163, 184, 0.15)' },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
      rightPriceScale: {
        borderColor: isDark ? 'rgba(148, 163, 184, 0.2)' : 'rgba(148, 163, 184, 0.24)',
        scaleMargins: { top: 0.08, bottom: 0.28 },
      },
      timeScale: {
        borderColor: isDark ? 'rgba(148, 163, 184, 0.2)' : 'rgba(148, 163, 184, 0.24)',
        timeVisible: true,
        secondsVisible: false,
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#16a34a',
      downColor: '#dc2626',
      borderVisible: false,
      wickUpColor: '#16a34a',
      wickDownColor: '#dc2626',
      lastValueVisible: true,
      priceLineVisible: true,
    });
    candleSeries.setData(
      payload.candles.map((item) => ({
        time: asChartTime(item.time),
        open: Number(item.open),
        high: Number(item.high),
        low: Number(item.low),
        close: Number(item.close),
      })),
    );

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: '',
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 },
    });
    volumeSeries.setData(
      (payload.volume || []).map((item) => ({
        time: asChartTime(item.time),
        value: Number(item.value),
        color: item.color,
      })),
    );

    Object.entries(payload.emas || {}).forEach(([span, values]) => {
      const series = chart.addSeries(LineSeries, {
        color: EMA_COLORS[span] || '#64748b',
        lineWidth: 2,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      series.setData(
        values.map((item) => ({
          time: asChartTime(item.time),
          value: Number(item.value),
        })),
      );
    });

    const lastTime = payload.candles[payload.candles.length - 1]?.time;
    const markers = [];

    (payload.positions || []).forEach((position, index) => {
      if (!lastTime) {
        return;
      }
      markers.push({
        time: asChartTime(lastTime),
        position: position.side === 'SHORT' ? 'aboveBar' : 'belowBar',
        color: position.side === 'SHORT' ? '#ef4444' : '#22c55e',
        shape: position.side === 'SHORT' ? 'arrowDown' : 'arrowUp',
        text: `POS ${position.side} ${index + 1}`,
      });
    });

    (payload.pending_orders || []).forEach((order, index) => {
      if (!lastTime) {
        return;
      }
      markers.push({
        time: asChartTime(lastTime),
        position: order.side === 'SELL' ? 'aboveBar' : 'belowBar',
        color: '#38bdf8',
        shape: 'circle',
        text: `ORD ${index + 1}`,
      });
    });

    if (markers.length) {
      createSeriesMarkers(candleSeries, markers);
    }

    const priceLines = [];
    (payload.positions || []).forEach((position) => {
      if (!position.entry_price) {
        return;
      }
      priceLines.push(
        candleSeries.createPriceLine({
          price: Number(position.entry_price),
          color: position.side === 'SHORT' ? '#f97316' : '#22c55e',
          lineWidth: 2,
          lineStyle: 2,
          axisLabelVisible: true,
          title: `POS ${position.side}`,
        }),
      );
    });

    (payload.pending_orders || []).forEach((order) => {
      if (!order.price) {
        return;
      }
      priceLines.push(
        candleSeries.createPriceLine({
          price: Number(order.price),
          color: '#38bdf8',
          lineWidth: 1,
          lineStyle: 1,
          axisLabelVisible: true,
          title: order.type || 'ORDER',
        }),
      );
    });

    (payload.risk_lines || []).forEach((line) => {
      if (!line.price) {
        return;
      }
      priceLines.push(
        candleSeries.createPriceLine({
          price: Number(line.price),
          color: line.type === 'take_profit' ? '#16a34a' : '#ef4444',
          lineWidth: 1,
          lineStyle: 3,
          axisLabelVisible: true,
          title: line.label || line.type,
        }),
      );
    });

    chart.timeScale().fitContent();

    const resizeObserver = new ResizeObserver(([entry]) => {
      const width = entry.contentRect.width;
      const height = entry.contentRect.height || 420;
      chart.applyOptions({ width, height });
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      priceLines.forEach((line) => candleSeries.removePriceLine(line));
      chart.remove();
    };
  }, [isDark, payload]);

  if (!payload?.candles?.length) {
    return <Empty description={t('noData')} />;
  }

  return <div ref={containerRef} className="trading-chart" />;
}
