import { useCallback, useEffect, useRef } from 'react';

// Read requests may be superseded by a filter, pagination or refresh action.
// Callers use the signal for both transport cancellation and result publication.
export function useLatestRequest() {
  const current = useRef(null);
  const mounted = useRef(false);
  const cancel = useCallback(() => current.current?.abort(), []);
  const begin = useCallback(() => {
    if (!mounted.current) return null;
    current.current?.abort();
    current.current = new AbortController();
    return current.current.signal;
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; cancel(); };
  }, [cancel]);
  return { begin, cancel };
}
