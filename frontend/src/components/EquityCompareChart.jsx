import React, { lazy, Suspense, useCallback, useMemo, useState } from 'react';
import { Empty, Segmented, Select, Space, Spin, Tag, Typography } from 'antd';
import { usePreferences } from '../app/usePreferences';
import { equityPointValue } from '../lib/equitySeries';

const LineChart = lazy(() => import('./LineChart'));
const { Text } = Typography;

function formatNumber(value) {
  if (value == null) return '—';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return numeric.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatPercent(value) {
  if (value == null) return '—';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return `${numeric >= 0 ? '+' : ''}${numeric.toFixed(2)}%`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function sourceName(source, locale) {
  const kind = source?.kind;
  if (kind === 'real_equity') return locale === 'zh' ? '实盘账户权益快照' : 'Live account equity snapshots';
  if (kind === 'strategy_equity') return locale === 'zh' ? '策略模拟权益快照' : 'Strategy simulated equity snapshots';
  if (kind === 'dca_invested') return locale === 'zh' ? '定投累计投入快照' : 'DCA invested capital snapshots';
  if (kind === 'spot_holding') return locale === 'zh' ? '现货持仓收益 · 按剩余成本' : 'Spot holding return · remaining cost';
  return source?.display_label || source?.label || '-';
}

function EquityCompareChart({ series = [], selectedIds = [], onSelectedIdsChange }) {
  const { locale, t } = usePreferences();
  const [valueMode, setValueMode] = useState('equity');
  const allIds = useMemo(() => series.map((item) => item.config_id).filter(Boolean), [series]);
  const activeIds = selectedIds.length ? selectedIds : allIds;
  const visibleItems = useMemo(
    () => series.filter((item) => activeIds.includes(item.config_id)),
    [activeIds, series],
  );
  const plottedItems = useMemo(() => visibleItems.filter((item) => item.points?.length), [visibleItems]);
  const isReturnMode = valueMode === 'return';
  const hasSpot = visibleItems.some(item => item.mode === 'SPOT_DCA');

  const chartSeries = useMemo(() => plottedItems.map((item) => {
    return {
      name: item.label || item.config_id,
      data: (item.points || []).map((point) => ({
        name: point.date,
        value: equityPointValue(item, point, valueMode),
      })),
      calibrationDates: (item.points || []).filter(point => point.calibrated).map(point => point.date),
    };
  }), [valueMode, plottedItems]);

  const tooltipFormatter = useCallback((params = []) => {
    if (!params.length) return '';
    const date = escapeHtml(params[0]?.value?.[0] || '');
    const rows = params.map((item) => (
      `<div>${item.marker}${escapeHtml(item.seriesName)} <strong>${isReturnMode ? formatPercent(item.value?.[1]) : formatNumber(item.value?.[1])}</strong></div>`
    )).join('');
    return `<div class="equity-chart-tooltip"><strong>${date}</strong>${rows}</div>`;
  }, [isReturnMode]);

  const valueFormatter = useCallback(
    (value) => `${value}%`,
    [],
  );

  return (
    <div className="equity-compare-content">
      <div className="equity-compare-toolbar">
        <Segmented
          value={valueMode}
          options={[
            { label: locale === 'zh' ? '权益 / 市值' : 'Equity / value', value: 'equity' },
            { label: locale === 'zh' ? '收益率' : 'Return', value: 'return' },
            ...(hasSpot ? [{ label: locale === 'zh' ? '投入记录' : 'Recorded investment', value: 'invested' }] : []),
          ]}
          onChange={setValueMode}
        />
        <Select
          mode="multiple"
          allowClear
          maxTagCount="responsive"
          className="equity-compare-select"
          value={activeIds}
          options={series.map((item) => ({
            value: item.config_id,
            label: `${item.label || item.config_id} · ${item.mode || '-'}`,
          }))}
          onChange={(values) => onSelectedIdsChange?.(values)}
          placeholder={locale === 'zh' ? '选择对比曲线' : 'Select curves'}
        />
      </div>

      <Text type="secondary" className="equity-compare-help">
        {locale === 'zh'
          ? '实盘收益率 =（权益变化 − 转入 + 转出）/ 起点权益；划转不算盈亏，流水不完整时待核对。权益曲线保留实际金额。现货收益率按剩余持仓成本计算，虚线标记成本校准日；旧记录缺少市值时只能查看投入。'
          : 'Live return = (equity change − deposits + withdrawals) / starting equity. Transfers are not profit; incomplete history awaits reconciliation. Equity shows actual funds. Spot return uses remaining cost; dashed lines mark calibrations. Legacy records without valuation only show invested amounts.'}
      </Text>

      <div className="chart-wrap">
        {chartSeries.some(item => item.data.some(point => point.value != null)) ? (
          <Suspense fallback={<Spin />}><LineChart
            series={chartSeries}
            yName={isReturnMode ? '%' : valueMode === 'invested' ? locale === 'zh' ? '投入记录' : 'Invested' : t('equity')}
            valueFormatter={isReturnMode ? valueFormatter : undefined}
            tooltipFormatter={tooltipFormatter}
          /></Suspense>
        ) : (
          <Empty description={t('noData')} />
        )}
      </div>

      <div className="equity-series-list">
        {visibleItems.map((item) => (
          <div className="equity-series-item" key={item.config_id}>
            <div className="equity-series-head">
              <Space size={6} wrap>
                <Tag color={item.mode === 'REAL' ? 'green' : item.mode === 'STRATEGY' ? 'blue' : 'gold'}>{item.mode || '-'}</Tag>
                <Text strong>{item.label || item.config_id}</Text>
              </Space>
              <Tag color={item.data_state === 'ready' ? 'success' : 'default'}>
                {item.data_state === 'ready'
                  ? `${item.point_count || 0} ${locale === 'zh' ? '个快照' : 'snapshots'}`
                  : (locale === 'zh' ? '暂无快照' : 'No snapshots')}
              </Tag>
            </div>
            <Text type="secondary" className="equity-series-source">
              {sourceName(item.data_source, locale)}
            </Text>
            {item.data_state === 'ready' ? (
              <div className="equity-series-meta">
                <span>{item.first_date} → {item.last_date}</span>
                <span>{locale === 'zh' ? '最新' : 'Latest'} {formatNumber(item.latest_equity)}</span>
                <span className={Number(item.return_pct) < 0 ? 'negative' : 'positive'}>{formatPercent(item.return_pct)}</span>
                {item.mode === 'REAL' && item.return_status !== 'ready' && <Tag color="warning">{locale === 'zh' ? '划转流水待核对' : 'Transfers awaiting reconciliation'}</Tag>}
              </div>
            ) : (
              <Text type="secondary">
                {locale === 'zh' ? '该配置在当前标的和时间范围内没有权益快照。' : 'No equity snapshots for this configuration and symbol.'}
              </Text>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

export default React.memo(EquityCompareChart);
