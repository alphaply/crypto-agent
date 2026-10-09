import { useEffect, useState } from 'react';
import { Alert, Button, Empty, Select, Space, Spin, Typography } from 'antd';
import { api } from '../lib/api';
import { MCP_SYMBOL_PAGE_SIZE, mcpSelectedSymbolLabels, mcpSymbolRequest } from '../lib/mcpSymbols';

// Parents key this editor by account and market so query, paging and pending
// requests cannot carry over to a different trading context.
export default function MarketSymbolPicker({ profileId, exchange, marketType = 'swap', endpoint = '/config/market-symbols', value, onChange, locale = 'zh' }) {
  const zh = locale === 'zh';
  const enabled = Boolean(profileId || exchange);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [quote, setQuote] = useState('');
  const [page, setPage] = useState(0);
  const [retry, setRetry] = useState(0);
  const [result, setResult] = useState(null);
  const [failure, setFailure] = useState(null);
  const requestKey = JSON.stringify([endpoint, profileId, exchange, marketType, value, query, quote, page, retry]);
  const current = result?.requestKey === requestKey ? result : null;
  const error = failure?.requestKey === requestKey ? failure.message : '';
  const searching = search.trim() !== query;
  const loading = enabled && ((!current && !error) || searching);
  const labels = mcpSelectedSymbolLabels(result);
  useEffect(() => {
    const timer = window.setTimeout(() => { setQuery(search.trim()); setPage(0); }, 250);
    return () => window.clearTimeout(timer);
  }, [search]);
  useEffect(() => {
    if (!enabled) return undefined;
    let active = true;
    const controller = new AbortController();
    api.get(endpoint, {
      params: { ...mcpSymbolRequest({ profileId, marketType, value: value ? [value] : [], query, quote, page }), exchange: exchange || undefined },
      signal: controller.signal, silent: true,
    }).then(({ data }) => {
      if (active) setResult({ ...data, requestKey });
    }).catch((err) => {
      if (active) setFailure({ requestKey, message: err.message });
    });
    return () => { active = false; controller.abort(); };
  }, [enabled, endpoint, profileId, exchange, marketType, value, query, quote, page, requestKey]);
  return <Space direction="vertical" size="small" style={{ width: '100%', minWidth: 0 }}>
    <Space wrap><TagLabel marketType={marketType} zh={zh} /><Select aria-label={zh ? '计价币筛选' : 'Quote currency filter'} value={quote || undefined} placeholder={zh ? '全部计价币' : 'All quotes'} allowClear disabled={!enabled} style={{ minWidth: 120 }} options={(result?.quote_currencies || []).map((item) => ({ value: item, label: item }))} onChange={(next) => { setQuote(next || ''); setPage(0); }} /></Space>
    <Select showSearch allowClear aria-label={zh ? '交易标的' : 'Trading symbol'} filterOption={false} searchValue={search} onSearch={(next) => setSearch(next.slice(0, 100))} value={value || undefined} labelRender={({ value: symbol }) => labels.get(symbol) || symbol} disabled={!enabled} loading={loading} options={searching ? [] : (current?.symbols || []).map((item) => ({ value: item.symbol, label: item.display_name || item.symbol }))} placeholder={enabled ? (zh ? '搜索币种或交易对，如 BTC、ETH/USDT' : 'Search an asset or pair, e.g. BTC, ETH/USDT') : (zh ? '请先选择交易账户' : 'Choose an account first')} onChange={(next) => { onChange(next || ''); setSearch(''); setQuery(''); setPage(0); }} style={{ width: '100%' }} notFoundContent={loading ? <Spin size="small" /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={zh ? '未找到可交易标的' : 'No tradable markets found'} />} />
    {error && <Alert type="error" showIcon title={zh ? '标的加载失败' : 'Market lookup failed'} description={error} action={<Button size="small" onClick={() => setRetry((currentRetry) => currentRetry + 1)}>{zh ? '重试' : 'Retry'}</Button>} />}
    {!!current?.invalid_symbols?.length && <Alert type="warning" showIcon title={zh ? '已选标的不在当前账户市场目录中，请重新选择。' : 'The selected market is unavailable for this account. Please select it again.'} />}
    {current && (current.total > MCP_SYMBOL_PAGE_SIZE || page > 0) && <Space wrap><Button size="small" disabled={page === 0 || loading} onClick={() => setPage((old) => old - 1)}>{zh ? '上一页' : 'Previous'}</Button><Typography.Text type="secondary">{zh ? `第 ${page + 1} 页 · 共 ${current.total} 个` : `Page ${page + 1} · ${current.total} markets`}</Typography.Text><Button size="small" disabled={!current.has_more || loading} onClick={() => setPage((old) => old + 1)}>{zh ? '下一页' : 'Next'}</Button></Space>}
  </Space>;
}

function TagLabel({ marketType, zh }) {
  return <Typography.Text type="secondary">{marketType === 'spot' ? (zh ? '现货市场' : 'Spot markets') : (zh ? '线性永续合约' : 'Linear perpetual markets')}</Typography.Text>;
}
