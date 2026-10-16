from copy import deepcopy

import pytest

from test_visible_position_management import isolated, tool, SYMBOL, Venue
from test_exclusive_position_management import ExclusiveExchange, make_service
from backend import database
from backend.utils import visible_management as management
from backend.utils.position_management import ensure_visible_management, management_action
from backend.utils.position_protection import ConcurrentProtectionUpdate


def test_historical_manager_does_not_block_authorized_connections():
    first = management.claim_management(tool('mcp:connection-1791515539173'), SYMBOL, 'LONG')
    second = management.claim_management(tool('mcp:another'), SYMBOL, 'LONG')
    assert first['claim_id'] == second['claim_id']
    assert second['owner'] == 'mcp:another'
    assert second['original_owner'] == 'mcp:connection-1791515539173'
    assert management.resource_management_status(tool('agent'), SYMBOL, 'LONG')['state'] == 'shared'
    for target in (tool(read_only=True), tool(enabled=False), tool(symbols=['BTC/USDT:USDT'])):
        with pytest.raises(ValueError):
            management.claim_management(target, SYMBOL, 'LONG')


def test_oneway_and_confirmed_physical_keys_share_resources():
    a = tool('a', Venue(hedged=False), physical_account_id='same-real-account')
    b = tool('b', Venue(hedged=False), physical_account_id='same-real-account')
    b.exchange.apiKey = 'another-key-for-the-confirmed-account'
    first = management.claim_management(a, SYMBOL, 'LONG')
    second = management.claim_management(b, SYMBOL, 'SHORT')
    assert first['claim_id'] == second['claim_id']
    assert first['side'] == second['side'] == 'BOTH'


def test_both_actors_manage_one_plan_and_stale_worker_cannot_overwrite():
    venue = ExclusiveExchange()
    a = make_service(venue, 'a', policy='visible_first_claim')
    b = make_service(venue, 'b', policy='visible_first_claim')
    ensure_visible_management(a.mt, SYMBOL, 'LONG')
    stale = deepcopy(a._load(SYMBOL, 'LONG'))
    ensure_visible_management(b.mt, SYMBOL, 'LONG')
    fresh = b._load(SYMBOL, 'LONG')
    fresh['stop_loss'] = 2300
    b._save(fresh)
    assert a._load(SYMBOL, 'LONG')['stop_loss'] == 2300
    stale['stop_loss'] = 2200
    with pytest.raises(ConcurrentProtectionUpdate):
        a._save(stale)
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM real_protection_plans WHERE symbol=%s', (SYMBOL,)).fetchone()[0] == 1


def test_cross_connection_close_uses_actual_position():
    venue = ExclusiveExchange()
    a = make_service(venue, 'a', policy='visible_first_claim')
    b = make_service(venue, 'b', policy='visible_first_claim')
    ensure_visible_management(a.mt, SYMBOL, 'LONG')
    with management_action(b.mt, SYMBOL, 'LONG', 'b-close-existing') as claim:
        result = b.close(SYMBOL, 'LONG', .01, 'limit', price=2565, operation_id='b-close-existing', contract_version=2)
        claim['outcome_status'] = result['status']
    assert result['status'] == 'submitted'
    assert len(venue.writes) == 1


@pytest.mark.parametrize('operation_id', ['different-request', 'original-unknown'])
def test_unknown_submission_still_blocks_reissuing_on_another_connection(operation_id):
    import json
    a = tool('a')
    first = management.claim_management(a, SYMBOL, 'LONG')
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO visible_management_actions(claim_id,owner,version,operation_id,state,payload) VALUES(%s,%s,%s,%s,%s,%s)',
                     (first['claim_id'], 'a', first['version'], 'original-unknown', 'unknown', json.dumps({'symbol': SYMBOL})))
        conn.commit()
    b = tool('b')
    shared = management.claim_management(b, SYMBOL, 'LONG')
    with pytest.raises(ValueError, match='requires verification'):
        with management.financial_reservation(b, shared, operation_id):
            pytest.fail('Unresolved original write was bypassed')


def test_other_connection_edits_and_cancels_original_order_without_recreating():
    venue = ExclusiveExchange()
    target = venue.external_order('original-tp', price=2600)
    a = make_service(venue, 'a', policy='exclusive_scope')
    b = make_service(venue, 'b', policy='visible_first_claim')
    ensure_visible_management(a.mt, SYMBOL, 'LONG')
    stale = deepcopy(a._load(SYMBOL, 'LONG'))
    result = b.edit(SYMBOL, 'regular:original-tp', price=2580,
                    reason='Update shared target', operation_id='b-edits-a-target')
    assert result['status'] == 'completed' and target['price'] == 2580
    with pytest.raises(ConcurrentProtectionUpdate):
        a._save(stale)
    a.reconcile_all()
    assert target['price'] == 2580
    b.cancel(SYMBOL, 'regular:original-tp', operation_id='b-cancels-a-target')
    a.reconcile_all()
    assert target['status'] == 'canceled'
    assert not [write for write in venue.writes if write[0] == 'create']


def _hold_shared_resource(db_path, entered, release):
    from backend.utils.shared_management import resource_lock
    database.DB_NAME = db_path
    with resource_lock(tool('original-worker'), SYMBOL):
        entered.set()
        assert release.wait(15)


def test_maintenance_and_other_profile_share_a_cross_process_boundary():
    import multiprocessing
    ctx = multiprocessing.get_context('spawn')
    entered, release = ctx.Event(), ctx.Event()
    worker = ctx.Process(target=_hold_shared_resource, args=(database.DB_NAME, entered, release))
    worker.start()
    try:
        assert entered.wait(15)
        other = tool('second-profile')
        claim = management.claim_management(other, SYMBOL, 'LONG')
        with pytest.raises(ValueError, match='running'):
            with management.financial_reservation(other, claim, 'other-live-write'):
                pytest.fail('Foreground bypassed active maintenance')
    finally:
        release.set()
        worker.join(20)
        if worker.is_alive():
            worker.terminate()
        assert worker.exitcode == 0


def test_profiles_share_cross_quote_spot_reservations_without_copying_baseline():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from test_mcp_spot_exits import FakeSpotExchange
    from backend.mcp import spot_orders
    from backend.utils.spot_management import visible_inventory
    venue = FakeSpotExchange()
    venue.spot = True
    venue.markets = {s: {**venue.market(s), 'quote': s.split('/')[1]} for s in ('BTC/USDT', 'BTC/USDC')}
    venue.fetch_open_orders = lambda symbol: [deepcopy(o) for o in venue.orders.values() if o['symbol'] == symbol and o['status'] == 'open']
    a, b = tool('spot-a', venue, market_type='spot'), tool('spot-b', venue, market_type='spot')
    a.runtime_config.update(mode='SPOT_DCA', symbols=list(venue.markets))
    b.runtime_config.update(mode='SPOT_DCA', symbols=list(venue.markets))
    a.market_type = b.market_type = 'spot'
    first = management.claim_management(a, 'BTC/USDT', 'BASE')
    second = management.claim_management(b, 'BTC/USDC', 'BASE')
    assert first['claim_id'] == second['claim_id']
    entered, release = Event(), Event()
    def create_first():
        with management.financial_reservation(a, first, 'a-reserves-stock'):
            entered.set()
            assert release.wait(10)
            return spot_orders._create(a, venue.market('BTC/USDT'),
                {'amount': 7, 'exit_type': 'limit', 'price': 110, 'reason': 'first exit'}, 'a-reserves-stock')
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(create_first)
        try:
            assert entered.wait(10)
            with pytest.raises(ValueError, match='running'):
                with management.financial_reservation(b, second, 'b-competes-for-stock'):
                    pytest.fail('Cross quote request bypassed shared asset lock')
        finally:
            release.set()
        assert future.result(timeout=10)['status'] == 'open'
    with management.financial_reservation(b, second, 'b-oversells-stock'):
        with pytest.raises(ValueError, match='exceeds|reserved'):
            spot_orders._create(b, venue.market('BTC/USDC'),
                {'amount': 4, 'exit_type': 'limit', 'price': 110, 'reason': 'excess exit'}, 'b-oversells-stock')
        second['outcome_status'] = 'failed'
    stock = visible_inventory(b, 'BTC/USDC', claim=second)
    assert stock['available_quantity'] == pytest.approx(2.99)
    assert stock['baseline_quantity'] == pytest.approx(9.99)
    assert stock['verified_owned'] == 0
    assert len(venue.writes) == 1
