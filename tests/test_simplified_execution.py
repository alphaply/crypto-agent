"""Offline regression for direct acknowledgements and background-only verification."""
from copy import deepcopy
from threading import Event, Thread
import pytest

from backend.utils.trade_summary import render_trade_receipt
from backend.utils.position_protection import _lock
from test_trade_tool_integration import simulation, real_short, call, recover
from test_exclusive_position_management import managed, SYMBOL
from test_execution_plan_index import index_db, Exchange


@pytest.mark.parametrize('outcome', ['accepted', 'timeout', 'rejected'])
def test_no_exchange_reads_after_submission_or_replay(real_short, monkeypatch, outcome):
    import ccxt
    service, exchange = real_short
    submitted = False
    writes = []
    create = exchange.create_order
    def submit(*args, **kwargs):
        nonlocal submitted
        writes.append(args)
        if outcome == 'rejected':
            submitted = True
            raise ccxt.InvalidOrder('{"code":-2010,"msg":"synthetic rejection"}')
        result = create(*args, **kwargs)
        submitted = True
        if outcome == 'timeout':
            raise ccxt.RequestTimeout('ACK lost')
        return result
    monkeypatch.setattr(exchange, 'create_order', submit)
    for name in ('fetch_order', 'fetch_positions', 'fetch_open_orders'):
        if hasattr(exchange, name):
            original = getattr(exchange, name)
            def read(*args, _original=original, **kwargs):
                assert not submitted, 'Tool performed post-submission verification'
                return _original(*args, **kwargs)
            monkeypatch.setattr(exchange, name, read)
    args = dict(side='SHORT', order_type='stop_market', amount=.2, trigger_price=110, reason='protect')
    result = call('close', args, 'no-poll')
    assert result['status'] == {'accepted': 'submitted', 'timeout': 'unknown', 'rejected': 'failed'}[outcome]
    again = call('close', args, 'no-poll')
    assert len(writes) == 1 and again['status'] == result['status']
    submitted = False
    if outcome == 'timeout':
        assert recover('no-poll')['status'] == 'submitted'


def test_discovery_shares_execution_lock(managed, monkeypatch):
    from backend.utils import position_management as management
    service, _ = managed
    entered, finished = Event(), Event()
    errors = []
    monkeypatch.setattr(management, '_discover', lambda *args: entered.set())
    def discover():
        try:
            management.ensure_managed(service, SYMBOL)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()
    with _lock(service.account_scope):
        worker = Thread(target=discover)
        worker.start()
        assert not entered.wait(.1)
    assert finished.wait(2)
    worker.join()
    assert entered.is_set() and not errors


def test_historical_rebuild_does_not_block_verified_ownership(index_db, monkeypatch):
    from backend.utils.execution_ledger import ExecutionLedger
    ledger = ExecutionLedger(Exchange(), 'cfg', 'ETH/USDT')
    ledger.index_state = {'complete': False, 'ownership_ready': True, 'phase': 'history'}
    monkeypatch.setattr(ledger, 'index_saved_plans', lambda: pytest.fail('No foreground historical reindex'))
    assert ledger.sync(start_ms=0, now_ms=100000)['complete']


def test_model_text_keeps_partial_outcomes_and_internal_receipt():
    raw = {'results': [
        {'action': 'cancel', 'status': 'completed', 'result': {'order_id': 'regular:one'}},
        {'action': 'open', 'status': 'unknown', 'result': {'symbol': 'ETH/USDT', 'amount': .08, 'price': 2335, 'error': '网络超时', 'client_id': 'internal-client'}},
        {'action': 'open', 'status': 'not_executed'},
    ]}
    before = deepcopy(raw)
    text = render_trade_receipt(raw)
    assert '撤单成功' in text and '网络超时' in text and '请勿重复提交' in text and '未执行' in text
    assert 'internal-client' not in text and raw == before
    assert len(text.encode()) < 4096

