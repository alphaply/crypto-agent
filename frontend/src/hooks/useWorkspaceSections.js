import { useEffect, useState } from 'react';
import { api } from '../lib/api';

function useSection(configId, section, key, params) {
  const [cache, setCache] = useState({});
  useEffect(() => {
    if (!configId) return undefined;
    const controller = new AbortController();
    setCache(previous => ({ ...previous, [configId]: { ...previous[configId], loading: true, error: '', key } }));
    api.get(`/public/workspace/${encodeURIComponent(configId)}`, {
      params: { ...JSON.parse(params), section }, signal: controller.signal, silent: true, timeout: 30000,
    }).then(({ data }) => {
      if (!controller.signal.aborted) setCache(previous => ({ ...previous, [configId]: { data, key, loading: false, updatedAt: Date.now() } }));
    }).catch(error => {
      if (!controller.signal.aborted) setCache(previous => ({ ...previous, [configId]: { ...previous[configId], key, loading: false, error: error.message } }));
    });
    return () => controller.abort();
  }, [configId, section, key, params]);
  const result = cache[configId] || {};
  return { ...result, loading: result.key !== key || result.loading };
}

export function useWorkspaceSections(configId, revision, timeframe, symbol, marketRefresh) {
  const context = useSection(configId, 'context', String(revision), '{}');
  const account = useSection(configId, 'account', `${revision}:${marketRefresh}`, '{}');
  const chart = useSection(configId, 'chart', `${revision}:${marketRefresh}`, JSON.stringify({ timeframe, symbol }));
  return { workspace: { ...context.data, position: account.data?.position, kline: chart.data?.kline,
    timeframe: chart.data?.timeframe, market_timeframes: context.data?.market_timeframes,
    sections: { context, account, chart }, refreshKey: revision }, context, account, chart };
}
