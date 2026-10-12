import time

import pytest

from test_independent_exits import local_db, live_service


@pytest.mark.parametrize('readable', [True, False])
def test_cancel_missing_ack_fill_gets_bounded_readback(live_service, readable):
    service, exchange = live_service
    stop = service.close('ETH/USDT', 'LONG', 1, 'stop_market', trigger_price=90)
    cancel, fetch = exchange.cancel_order, exchange.fetch_order
    cancellations, followups = [], []
    def cancel_missing(*args, **kwargs):
        cancellations.append(args[0])
        result = cancel(*args, **kwargs)
        result['filled'] = None
        return result
    def query(*args, **kwargs):
        result = fetch(*args, **kwargs)
        if cancellations:
            followups.append(getattr(exchange, 'timeout', None))
            if not readable:
                result.update(status='open', filled=0)
        return result
    exchange.cancel_order, exchange.fetch_order = cancel_missing, query
    started = time.monotonic()
    if readable:
        service.cancel('ETH/USDT', stop['id'], operation_id='cancel-missing')
        assert len(followups) == 1
    else:
        with pytest.raises(RuntimeError, match='cumulative fill'):
            service.cancel('ETH/USDT', stop['id'], operation_id='cancel-missing')
        assert len(followups) == 3
    assert time.monotonic() - started < 2
    assert all(0 < timeout <= 2000 for timeout in followups)
    assert not hasattr(exchange, 'timeout')
    assert cancellations == [stop['id']] and len(exchange.calls) == 2


def test_background_respects_backoff_but_explicit_read_can_refresh(live_service):
    service, exchange = live_service
    stop = service.close('ETH/USDT', 'LONG', 1, 'stop_market', trigger_price=90)
    plan = service._load('ETH/USDT:USDT', 'LONG')
    plan['exits'][0].update(fill_pending=True, query_error='temporary unavailable', next_retry_at=time.time() + 60)
    service._save(plan)
    fetch, reads = exchange.fetch_order, []
    def query(*args, **kwargs):
        reads.append(args[0])
        return fetch(*args, **kwargs)
    exchange.fetch_order = query
    service.reconcile_all(respect_backoff=True)
    assert reads == []
    plan = service._load('ETH/USDT:USDT', 'LONG')
    service.collect_readonly(plan)
    assert reads == [stop['id']]
    assert not plan['exits'][0].get('fill_pending')
    assert not plan['exits'][0].get('next_retry_at')
