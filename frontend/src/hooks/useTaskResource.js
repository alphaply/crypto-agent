import { useEffect, useState } from 'react';
import { api } from '../lib/api';

export function useTaskResource(path, params, refreshKey) {
  const [state, setState] = useState({});
  const [revision, setRevision] = useState(0);
  const key = JSON.stringify(params);
  const identity = `${path}:${key}`;
  const requestKey = `${identity}:${refreshKey}:${revision}`;
  useEffect(() => {
    const controller = new AbortController();
    api.get(path, { params: JSON.parse(key), signal: controller.signal, silent: true }).then(({ data }) => {
      if (!controller.signal.aborted) setState({ identity, requestKey, data });
    }).catch(error => {
      if (!controller.signal.aborted) setState(previous => ({ ...(previous.identity === identity ? previous : {}), identity, requestKey, error: error.message }));
    });
    return () => controller.abort();
  }, [path, key, identity, requestKey]);
  const loading = state.requestKey !== requestKey;
  return { ...(state.identity === identity ? state : {}), loading, error: loading ? '' : state.error, retry: () => setRevision(value => value + 1) };
}
