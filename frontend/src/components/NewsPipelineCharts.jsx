import React, { useEffect, useMemo, useRef } from 'react';
import { init, use as registerCharts } from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent, AriaComponent } from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';
import { usePreferences } from '../app/usePreferences';
import { scoreDistribution, sourceDistribution } from '../lib/newsPipeline';

registerCharts([BarChart, GridComponent, LegendComponent, TooltipComponent, AriaComponent, SVGRenderer]);

function Chart({ option, label }) {
  const element = useRef(null);
  const instance = useRef(null);
  useEffect(() => {
    const chart = init(element.current, undefined, { renderer: 'svg' });
    instance.current = chart;
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(element.current);
    return () => { observer.disconnect(); chart.dispose(); instance.current = null; };
  }, []);
  useEffect(() => { instance.current?.setOption(option, true); }, [option]);
  return <div className="news-pipeline-chart" ref={element} role="img" aria-label={label} />;
}

export default function NewsPipelineCharts({ items, locale }) {
  const { isDark } = usePreferences();
  const zh = locale === 'zh';
  const histogram = useMemo(() => scoreDistribution(items), [items]);
  const sources = useMemo(() => sourceDistribution(items), [items]);
  const text = isDark ? '#cbd5e1' : '#475569';
  const grid = isDark ? '#334155' : '#e2e8f0';
  const shared = {
    animation: false, aria: { enabled: true },
    tooltip: { trigger: 'axis', confine: true, renderMode: 'richText' },
    grid: { left: 18, right: 20, top: 28, bottom: 16, containLabel: true },
    xAxis: { type: 'category', axisLabel: { color: text }, axisLine: { lineStyle: { color: grid } }, axisTick: { show: false } },
    yAxis: { type: 'value', minInterval: 1, axisLabel: { color: text }, splitLine: { lineStyle: { color: grid, type: 'dashed' } } },
  };
  return <div className="news-pipeline-charts">
    <section className="news-pipeline-plot"><h4>{zh ? '相关度分布' : 'Relevance distribution'}</h4><p>{zh ? '已成功评分的消息 · 0–100' : 'Successfully scored items · 0–100'}</p>
      <Chart label={zh ? '消息相关度分布柱状图' : 'News relevance histogram'} option={{ ...shared, xAxis: { ...shared.xAxis, data: histogram.map((bucket) => bucket.label) }, series: [{ type: 'bar', data: histogram.map((bucket) => bucket.count), barMaxWidth: 42, itemStyle: { color: '#5b7cfa', borderRadius: [5, 5, 0, 0] } }] }} />
    </section>
    <section className="news-pipeline-plot"><h4>{zh ? '各来源处理结果' : 'Results by source'}</h4><p>{zh ? '入选、过滤与失败均独立统计' : 'Selected, filtered and failed items counted separately'}</p>
      <Chart label={zh ? '消息来源处理结果堆叠柱状图' : 'Source result stacked bar chart'} option={{ ...shared, grid: { ...shared.grid, top: 45, bottom: 35 }, legend: { top: 0, type: 'scroll', textStyle: { color: text } }, xAxis: { ...shared.xAxis, data: sources.map((source) => source.name), axisLabel: { color: text, interval: 0, width: 90, overflow: 'truncate', rotate: sources.length > 4 ? 25 : 0 } }, series: [
        ['selected', zh ? '入选' : 'Selected', '#14b8a6'], ['filtered', zh ? '过滤' : 'Filtered', '#94a3b8'],
        ['failed', zh ? '失败' : 'Failed', '#f87171'], ['scored', zh ? '已评分' : 'Scored', '#5b7cfa'], ['pending', zh ? '待处理' : 'Pending', '#eab308'],
      ].map(([key, name, color]) => ({ name, type: 'bar', stack: 'source', barMaxWidth: 34, data: sources.map((source) => source[key]), itemStyle: { color } })) }} />
    </section>
  </div>;
}
