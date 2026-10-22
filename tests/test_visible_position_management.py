"""Offline authority lifecycle and physical inventory invariants."""
from copy import deepcopy
import json
import multiprocessing
from types import SimpleNamespace

import pytest

from backend import database
from backend.utils import visible_management as management
from backend.utils.management_policy import normalize_management
from backend.utils.position_management import ensure_visible_management, management_action
from backend.utils.spot_management import visible_inventory


SYMBOL = 'ETH/USDT:USDT'


class Venue:
    id = 'binanceusdm'
    apiKey = 'offline-only-key'

    def __init__(self, hedged=True, spot=False):
        self.hedged, self.spot = hedged, spot
        self.markets = {s: {'symbol': s, 'base': 'ETH', 'spot': spot, 'swap': not spot,
                           'linear': True, 'contract': not spot, 'contractSize': 1}
                        for s in (['ETH/USDT', 'ETH/USDC'] if spot else [SYMBOL])}
        self.balance = {'ETH': {'total': 1.2, 'free': 1.2, 'used': 0}}
        self.orders = []

    def market(self, symbol):
        return self.markets[symbol]

    def fetch_position_mode(self, symbol):
        return {'hedged': self.hedged}

    def fetch_positions(self, symbols=None):
        return []

    def fetch_balance(self):
        return deepcopy(self.balance)

    def fetch_open_orders(self, symbol, **kwargs):
        return [deepcopy(o) for o in self.orders if o['symbol'] == symbol]


def tool(owner='owner', venue=None, **updates):
    venue = venue or Venue()
    cfg = {'config_id': owner, 'mode': 'SPOT_DCA' if venue.spot else 'REAL',
           'market_type': 'spot' if venue.spot else 'swap', 'symbol': 'ETH/USDT' if venue.spot else SYMBOL,
           'symbols': list(venue.markets), 'enabled': True, 'position_management_policy': 'visible_first_claim', **updates}
    return SimpleNamespace(config_id=owner, runtime_config=cfg, exchange=venue, market_type=cfg['market_type'])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, financial_pg):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'visible.sqlite'))
    pass  # financial_pg installs the complete native migration schema.


def test_default_and_legacy_policy_mapping():
    assert normalize_management({'mode': 'REAL'})['position_management_policy'] == 'visible_first_claim'
    assert normalize_management({'position_management_policy': 'strict_owned'})['position_management_policy'] == 'visible_first_claim'


def test_query_does_not_claim_or_mutate_and_reports_competition():
    mt = tool()
    assert management.resource_management_status(mt, SYMBOL, 'LONG')['can_claim']
    assert management.management_status('owner')['scopes'] == []
    claim = management.claim_management(mt, SYMBOL, 'LONG')
    other = tool('other')
    projected = management.resource_management_status(other, SYMBOL, 'LONG')
    assert projected['state'] == 'shared' and projected['owner'] == 'owner'
    assert projected['claim_id'] == claim['claim_id']
    assert management.claim_management(other, SYMBOL, 'LONG')['claim_id'] == claim['claim_id']


def test_hedge_unused_direction_is_not_preclaimed_but_oneway_shares_both():
    mt = tool()
    management.claim_management(mt, SYMBOL, 'LONG')
    assert management.claim_management(tool('other'), SYMBOL, 'SHORT')['side'] == 'SHORT'
    mt.exchange.hedged = False
    assert management.claim_management(mt, SYMBOL, 'LONG')['side'] == 'BOTH'


def test_visible_direct_request_manages_only_its_hedge_target():
    from test_exclusive_position_management import ExclusiveExchange, make_service
    venue = ExclusiveExchange()
    venue.quantities['SHORT'] = .02
    long_owner = make_service(venue, 'long-owner', policy='visible_first_claim')
    short_owner = make_service(venue, 'short-owner', policy='visible_first_claim')
    ensure_visible_management(long_owner.mt, SYMBOL, 'LONG')
    ensure_visible_management(short_owner.mt, SYMBOL, 'SHORT')
    result = long_owner.close(SYMBOL, 'LONG', .01, 'limit', price=2565,
                              operation_id='only-long', contract_version=2)
    assert result['status'] == 'submitted'
    assert management.management_status('short-owner')['scopes'][0]['side'] == 'SHORT'
    assert len(venue.writes) == 1


def test_same_physical_account_multiple_keys_conflict():
    first = tool(physical_account_id='confirmed-account')
    claim = management.claim_management(first, SYMBOL, 'LONG')
    second = tool('other', physical_account_id='confirmed-account')
    second.exchange.apiKey = 'other-offline-key'
    assert management.claim_management(second, SYMBOL, 'LONG')['claim_id'] == claim['claim_id']


def test_same_credential_cannot_bypass_owner_with_a_conflicting_account_alias():
    management.claim_management(tool(physical_account_id='confirmed-one'), SYMBOL, 'LONG')
    with pytest.raises(ValueError, match='physical account identity conflicts'):
        management.claim_management(tool('other', physical_account_id='conflicting-two'), SYMBOL, 'LONG')
    assert len(management.management_status('owner')['scopes']) == 1
    assert management.management_status('other')['scopes'] == []


def test_physical_labels_are_venue_scoped_and_binance_spot_swap_share_identity():
    swap = tool(physical_account_id='account-label')
    spot = tool(venue=Venue(spot=True), physical_account_id='account-label')
    spot.exchange.id = 'binance'
    okx = tool('other', physical_account_id='account-label')
    okx.exchange.id = 'okx'
    assert management.physical_account_scope(swap) == management.physical_account_scope(spot)
    assert management.physical_account_scope(swap) != management.physical_account_scope(okx)
    management.claim_management(swap, SYMBOL, 'LONG')
    assert management.claim_management(okx, SYMBOL, 'LONG')['owner'] == 'other'


def test_readonly_disabled_and_outside_symbol_cannot_claim():
    for mt in (tool(read_only=True), tool(enabled=False), tool(symbol='BTC/USDT:USDT', symbols=['BTC/USDT:USDT'])):
        with pytest.raises(ValueError):
            management.claim_management(mt, SYMBOL, 'LONG')
    assert management.management_status('owner')['scopes'] == []


def test_wrong_order_response_does_not_claim_an_unrelated_direction():
    mt = tool()
    mt.exchange.fetch_order=lambda *a,**k: {'id':'wrong','symbol':SYMBOL,'side':'sell','info':{'positionSide':'SHORT'}}
    with pytest.raises(ValueError,match='Exact order identity'):
        with management_action(mt,SYMBOL,None,'wrong-target',order_id='regular:requested'):
            pytest.fail('wrong order identity acquired authority')
    assert management.management_status('owner')['scopes']==[]


def test_authority_survives_flat_pause_and_explicit_release(monkeypatch):
    mt = tool()
    claim = management.claim_management(mt, SYMBOL, 'LONG')
    mt.runtime_config['enabled'] = False
    assert management.management_status('owner')['scopes'][0]['claim_id'] == claim['claim_id']
    monkeypatch.setattr('backend.utils.market_data.MarketTool', lambda **kw: mt)
    result = management.release_management_scope('owner', claim['claim_id'], confirmed=True)
    assert result['status'] == 'released'
    mt.runtime_config['enabled'] = True
    assert management.claim_management(tool('other'), SYMBOL, 'LONG')['owner'] == 'other'


@pytest.mark.parametrize('bound', [False, True])
def test_legacy_migration_preserves_source_but_no_exclusivity(bound):
    account = {'profile_id': 'account', 'exchange': 'binance', 'api_key': 'offline-only-key', 'secret': 'offline'}
    cfg = {'config_id': 'owner', 'mode': 'REAL', 'symbol': SYMBOL, 'exchange_profile_id': 'account',
           'exit_mode': 'independent_exits', 'position_management_policy': 'exclusive_scope',
           'management_scopes': [{'symbol': SYMBOL, 'side': 'LONG'}]}
    snapshot = {'agents': [cfg], 'exchange_profiles': [account]}
    legacy = management._legacy_scope(tool())
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO position_management_scopes(account_scope,symbol,side,owner,payload) VALUES(%s,%s,%s,%s,%s)',
                     (legacy, SYMBOL, 'LONG', 'owner', json.dumps({'hedged': True})))
        if bound:
            plan = {'account_scope': legacy, 'symbol': SYMBOL, 'side': 'LONG', 'state': 'DONE', 'ever_filled': True,
                    'entries': [{'order_id': 'historical-owned-order', 'status': 'closed'}]}
            conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)',
                         ('owner', SYMBOL, 'LONG', json.dumps(plan)))
        management.migrate_existing_claims(conn, snapshot)
        claim = management._decode(conn.execute('SELECT * FROM visible_management_claims').fetchone())
        assert claim['resource_bound'] is bound
        conn.commit()
    shared = management.claim_management(tool('other'), SYMBOL, 'LONG')
    assert shared['claim_id'] == claim['claim_id']
    assert shared['original_owner'] == 'owner'


def test_transfer_keeps_original_identity_and_recovery_is_narrow(monkeypatch):
    source, target = tool(), tool('receiver')
    claim = management.claim_management(source, SYMBOL, 'LONG')
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO visible_management_actions(claim_id,owner,version,operation_id,state,payload) VALUES(%s,%s,%s,%s,%s,%s)',
                     (claim['claim_id'], 'owner', claim['version'], 'original-child', 'unknown', json.dumps({'symbol': SYMBOL})))
        conn.commit()
    monkeypatch.setattr('backend.utils.market_data.MarketTool', lambda config_id: source if config_id == 'owner' else target)
    result = management.transfer_management_scope('owner', claim['claim_id'], 'receiver', accepted=True)
    assert result['status'] == 'transferred'
    grant = management.management_recovery_targets('receiver')[0]
    assert grant['operation_id'] == 'original-child' and grant['source_owner'] == 'owner'
    assert management.lookup_management_recovery('unrelated', grant['recovery_id']) is None
    with pytest.raises(ValueError):
        with management.financial_reservation(source, claim, 'another-operation'):
            pytest.fail('old owner passed the claim version fence')


def test_swap_handoff_query_uses_receiver_account_after_source_configuration_retirement(monkeypatch):
    from test_exclusive_position_management import ExclusiveExchange, make_service
    from backend.utils.trade_operations import run_once
    venue = ExclusiveExchange()
    source = make_service(venue, 'owner', policy='visible_first_claim')
    receiver = make_service(venue, 'receiver', policy='visible_first_claim')
    ensure_visible_management(source.mt, SYMBOL, 'LONG')
    venue.timeout_create = True
    request = {'tool': 'close', 'side': 'LONG', 'amount': .01, 'order_type': 'limit', 'price': 2565}
    run_once('owner', SYMBOL, 'old-close', request,
             lambda: source.close(SYMBOL, 'LONG', .01, 'limit', price=2565, operation_id='old-close', contract_version=2))
    claim = management.management_status('owner')['scopes'][0]
    monkeypatch.setattr('backend.utils.market_data.MarketTool', lambda config_id: source.mt if config_id == 'owner' else receiver.mt)
    moved = management.transfer_management_scope('owner', claim['claim_id'], 'receiver', accepted=True)
    grant = next(g for g in moved['recovery_targets'] if g['operation_id'] == 'old-close')
    with database.get_db_conn() as conn:
        conn.execute("DELETE FROM real_protection_plans WHERE config_id='owner'")
        conn.commit()
    def receiver_only(config_id):
        assert config_id == 'receiver', 'retired source configuration was used for recovery credentials'
        return receiver.mt
    monkeypatch.setattr('backend.utils.market_data.MarketTool', receiver_only)
    before = deepcopy(venue.writes)
    result = management.query_management_recovery('receiver', grant['recovery_id'])
    assert result['result']['status'] in {'submitted', 'completed'}, result
    assert result['result']['query']['status'] != 'failed'
    assert venue.writes == before
    assert management.lookup_management_recovery('unrelated', grant['recovery_id']) is None


def test_mcp_retirement_only_accepts_mapped_original_unknown_operations(monkeypatch):
    from backend.mcp import store
    from backend.mcp.config_guard import active_lifecycle
    source, receiver = tool('mcp:source'), tool('mcp:receiver')
    claim = management.claim_management(source, SYMBOL, 'LONG')
    with database.get_db_conn() as conn:
        store.initialize_schema(conn)
        conn.execute('INSERT INTO mcp_operations(profile_id,operation_id,fingerprint,principal,state,result,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)', ('source','original','hash','private-principal','unknown','{}',0,0))
        conn.execute('INSERT INTO mcp_operation_links(profile_id,operation_id,principal,internal_operation_id,symbol) VALUES(%s,%s,%s,%s,%s)', ('source','original','private-principal','original-internal',SYMBOL))
        conn.execute('INSERT INTO trade_operation_requests(config_id,operation_id,request_json) VALUES(%s,%s,%s)',
                     ('mcp:source','original-internal',json.dumps({'contract_version':3,'action':'close','arguments':{'side':'LONG'}})))
        conn.commit()
        assert active_lifecycle(conn, {'profile_id':'source','market_type':'swap'})
    monkeypatch.setattr('backend.utils.market_data.MarketTool', lambda config_id: source if config_id == 'mcp:source' else receiver)
    moved = management.transfer_management_scope('mcp:source', claim['claim_id'], 'mcp:receiver', accepted=True)
    assert any(g['operation_id']=='original-internal' for g in moved['recovery_targets'])
    with database.get_db_conn() as conn:
        assert not active_lifecycle(conn, {'profile_id':'source','market_type':'swap'})
        conn.execute('INSERT INTO mcp_operations(profile_id,operation_id,fingerprint,principal,state,result,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)', ('source','other-private','hash','private-principal','unknown','{}',0,0))
        conn.execute('INSERT INTO mcp_operation_links(profile_id,operation_id,principal,internal_operation_id,symbol) VALUES(%s,%s,%s,%s,%s)', ('source','other-private','private-principal','unaccepted-internal',SYMBOL))
        conn.commit()
        assert active_lifecycle(conn, {'profile_id':'source','market_type':'swap'})


@pytest.mark.parametrize('handoff', [False, True])
def test_v3_attached_unknown_uses_native_identity_in_normal_and_handoff_recovery(monkeypatch, handoff):
    from test_exclusive_position_management import ExclusiveExchange, make_service
    from backend.config import config
    from backend.agent import agent_tools
    from backend.agent.unified_dispatch import _attached_close
    from backend.agent.trading_contracts import UnifiedCloseAction
    from backend.utils.position_protection import PositionProtection
    from backend.utils.trade_operations import run_once
    venue = ExclusiveExchange()
    source = make_service(venue, 'owner', policy='visible_first_claim')
    receiver = make_service(venue, 'receiver', policy='visible_first_claim')
    source.mt.runtime_config['exit_mode'] = receiver.mt.runtime_config['exit_mode'] = 'attached_optional'
    monkeypatch.setattr(config, 'get_config_by_id', lambda cid: source.mt.runtime_config if cid == 'owner' else receiver.mt.runtime_config)
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda config_id: source.mt if config_id == 'owner' else receiver.mt)
    monkeypatch.setattr('backend.utils.market_data.MarketTool', lambda config_id: source.mt if config_id == 'owner' else receiver.mt)
    action = UnifiedCloseAction.model_validate({'action': 'close', 'side': 'LONG', 'amount': .01,
                                               'order_type': 'limit', 'price': 2565, 'reason': 'native close'})
    venue.timeout_create = True
    def dispatch():
        with management_action(source.mt, SYMBOL, 'LONG', 'native-close') as claim:
            result = _attached_close(action, 'owner', SYMBOL, 'native-close', source.mt, PositionProtection(source.mt))
            claim['outcome_status'] = result['status']
            return result
    original = run_once('owner', SYMBOL, 'native-close',
                        {'contract_version': 3, 'action': 'close', 'arguments': action.model_dump(exclude_none=True)}, dispatch)
    assert original['status'] == 'unknown'
    writes = deepcopy(venue.writes)
    claim = management.management_status('owner')['scopes'][0]
    if handoff:
        moved = management.transfer_management_scope('owner', claim['claim_id'], 'receiver', accepted=True)
        grant = next(g for g in moved['recovery_targets'] if g['operation_id'] == 'native-close')
        with database.get_db_conn() as conn:
            conn.execute("DELETE FROM real_protection_plans WHERE config_id='owner'")
            conn.commit()
        monkeypatch.setattr(config, 'get_config_by_id', lambda cid: None if cid == 'owner' else receiver.mt.runtime_config)
        def receiver_only(config_id):
            assert config_id == 'receiver'
            return receiver.mt
        monkeypatch.setattr('backend.utils.market_data.MarketTool', receiver_only)
        result = management.query_management_recovery('receiver', grant['recovery_id'])['result']
        assert result['status'] in {'submitted', 'completed'}, result
    else:
        assert management.reconcile_visible_actions('owner') == 1
    assert venue.writes == writes
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT state FROM visible_management_actions').fetchone()[0] == 'completed'


def _spot_tool(venue, owner='owner'):
    venue.spot = True
    venue.markets = {'BTC/USDT': venue.market('BTC/USDT')}
    venue.fetch_open_orders = lambda symbol: [deepcopy(o) for o in venue.orders.values()
                                             if o['symbol'] == symbol and o['status'] == 'open']
    return SimpleNamespace(config_id=owner, exchange=venue, market_type='spot',
                           runtime_config={'config_id': owner, 'mode': 'SPOT_DCA', 'market_type': 'spot',
                                           'symbol': 'BTC/USDT', 'enabled': True, 'position_management_policy': 'visible_first_claim'})


def test_spot_transfer_recovers_and_cancels_original_native_exit_without_source_config(monkeypatch):
    import ccxt
    from test_mcp_spot_exits import FakeSpotExchange
    from backend.mcp import spot_orders
    venue = FakeSpotExchange()
    source, receiver = _spot_tool(venue), _spot_tool(venue, 'receiver')
    claim = management.claim_management(source, 'BTC/USDT', 'BASE')
    venue.error = ccxt.NetworkError('response lost after acceptance')
    with management.financial_reservation(source, claim, 'original-exit'):
        created = spot_orders._create(source, venue.market('BTC/USDT'),
                                     {'amount': 1, 'exit_type': 'limit', 'price': 90, 'reason': 'original'}, 'original-exit')
    assert created['status'] == 'unknown' and len(venue.writes) == 1
    monkeypatch.setattr('backend.utils.market_data.MarketTool', lambda config_id: source if config_id == 'owner' else receiver)
    result = management.transfer_management_scope('owner', claim['claim_id'], 'receiver', accepted=True)
    assert result['status'] == 'transferred'
    grant = next(g for g in result['recovery_targets'] if g.get('recovery_kind') == 'spot_exit')
    recovery = management.query_management_recovery('receiver', grant['recovery_id'])
    recovered_id = recovery['result']['exchange_id']
    assert venue.orders[recovered_id]['clientOrderId'] == created['exit_id']
    assert len(venue.writes) == 1
    inventory = visible_inventory(receiver, 'BTC/USDT')
    assert inventory['reserved_quantity'] == 1
    cancelled = spot_orders._cancel(receiver, venue.market('BTC/USDT'), {'exit_id': created['exit_id'], 'reason': 'accepted handoff'})
    assert cancelled['status'] == 'cancelled'
    assert spot_orders._rows('owner')[0]['config_id'] == 'owner'
    assert spot_orders._rows('receiver') == []


def test_visible_spot_historical_fees_and_external_fills_remain_separate():
    from test_mcp_spot_exits import FakeSpotExchange
    venue = FakeSpotExchange()
    venue.balance_extra = 2
    venue.trades.append({'id': 'external', 'order': 'manual-buy', 'symbol': 'BTC/USDT', 'side': 'buy',
                         'amount': 2, 'fee': {'cost': 0, 'currency': 'USDT'}})
    mt = _spot_tool(venue)
    database.save_order_log('buy-1', 'BTC/USDT', 'Agent', 'buy', 100, 0, 0, 'historical buy',
                            trade_mode='SPOT_DCA', config_id='owner', amount=10, status='FILLED')
    claim = management.claim_management(mt, 'BTC/USDT', 'BASE')
    inventory = visible_inventory(mt, 'BTC/USDT', claim=claim)
    assert inventory['verified_owned'] == pytest.approx(9.99)
    assert inventory['fee_adjustment'] == pytest.approx(.01)
    assert inventory['external_holdings'] == pytest.approx(2)
    assert inventory['managed_quantity'] == pytest.approx(11.99)


def test_owned_spot_snapshot_reference_can_cancel_exact_native_order(monkeypatch):
    from test_mcp_spot_exits import FakeSpotExchange
    from backend.mcp import spot_orders
    from backend.utils.spot_context import scope_spot_accounts
    from backend.utils.spot_management import execute_spot_action
    from backend.utils.trade_operations import financial_ack_only

    venue = FakeSpotExchange()
    mt = _spot_tool(venue)
    venue.orders['pending-buy'] = {
        'id': 'pending-buy', 'symbol': 'BTC/USDT', 'side': 'buy', 'type': 'limit',
        'amount': 1, 'filled': .25, 'remaining': .75, 'price': 100, 'status': 'open',
    }
    database.save_order_log('pending-buy', 'BTC/USDT', 'test', 'buy', 100, 0, 0,
                            'owned entry', trade_mode='SPOT_DCA', config_id='owner', amount=1)
    snapshot = {'real_open_orders': [{
        **venue.orders['pending-buy'], 'order_id': 'regular:pending-buy',
        'exchange_order_id': 'pending-buy', 'exchange_channel': 'regular',
    }]}
    scoped = scope_spot_accounts('owner', {'BTC/USDT': snapshot})['BTC/USDT']
    assert len(scoped['real_open_orders']) == 1
    assert scoped['external_open_orders'] == []
    assert venue.writes == []

    original_cancel = venue.cancel_order

    def cancel_with_ack(identity, symbol):
        original_cancel(identity, symbol)
        return deepcopy(venue.orders[identity])

    venue.cancel_order = cancel_with_ack
    monkeypatch.setattr(spot_orders, '_tool', lambda *a: (mt, venue.market('BTC/USDT')))
    token = financial_ack_only.set(True)
    try:
        result = execute_spot_action('owner', 'BTC/USDT', {
            'action': 'cancel', 'order_id': scoped['real_open_orders'][0]['order_id'],
            'reason': 'cancel the displayed task order',
        }, 'cancel-displayed-spot-order')
    finally:
        financial_ack_only.reset(token)

    assert result['status'] == 'completed'
    assert result['order_id'] == 'regular:pending-buy'
    assert result['price'] == 100
    assert result['amount'] == 1
    assert result['filled_amount'] == .25
    assert venue.writes == [('cancel', 'pending-buy')]
    assert venue.orders['pending-buy']['status'] == 'canceled'
    assert venue.orders['buy-1']['status'] == 'closed'


def test_regular_spot_cancel_keeps_inventory_reserved_until_fills_are_verified(monkeypatch):
    from test_mcp_spot_exits import FakeSpotExchange
    from backend.mcp import spot_orders
    from backend.utils.spot_management import execute_spot_action,recover_spot_action
    venue=FakeSpotExchange()
    mt=_spot_tool(venue)
    venue.orders['external']={'id':'external','symbol':'BTC/USDT','side':'sell','type':'limit','amount':1,'filled':0,'remaining':1,'price':100,'status':'open'}
    original_cancel=venue.cancel_order
    def cancel_unknown(identity,symbol):
        original_cancel(identity,symbol)
        venue.orders[identity]['filled']=None
    venue.cancel_order=cancel_unknown
    venue.fetch_balance=lambda: {'BTC':{'total':9.99,'free':9.99,'used':0}}
    monkeypatch.setattr(spot_orders,'_tool',lambda *a:(mt,venue.market('BTC/USDT')))
    result=execute_spot_action('owner','BTC/USDT',{'action':'cancel','order_id':'regular:external','reason':'cancel'},'cancel-external')
    assert result['status']=='pending' and result['filled_amount'] is None
    assert result['price'] == 100
    inventory=visible_inventory(mt,'BTC/USDT')
    assert inventory['reserved_quantity']==1 and inventory['available_quantity']==0
    venue.orders['external']['filled']=0
    resolved=recover_spot_action(mt,'owner','BTC/USDT','cancel-external',{},result)
    assert resolved['status']=='completed'
    assert resolved['price'] == 100
    assert visible_inventory(mt,'BTC/USDT')['available_quantity']==pytest.approx(9.99)
    assert len(venue.writes)==1


def test_spot_buy_native_edit_enforces_shared_increment_budget_and_updates_commitment(monkeypatch):
    from test_mcp_spot_exits import FakeSpotExchange
    from backend.mcp import spot_orders
    from backend.utils.spot_management import execute_spot_action
    from backend.utils.spot_execution import current_spot_cycle_id,spot_budget_status
    venue=FakeSpotExchange()
    mt=_spot_tool(venue)
    mt.runtime_config.update(dca_amount=10,dca_budget=100)
    venue.has={'editOrder':True}
    venue.orders['buy-1'].update(status='open',type='limit',price=10,amount=1,filled=0,remaining=1)
    venue.fetch_balance=lambda: {'BTC':{'total':9.99,'free':9.99,'used':0},'USDT':{'total':1000,'free':1000,'used':0}}
    def edit(identity,symbol,kind,side,amount,price):
        venue.writes.append(('edit',identity,amount,price))
        venue.orders[identity].update(amount=amount,price=price,remaining=amount)
        return deepcopy(venue.orders[identity])
    venue.edit_order=edit
    database.save_order_log('buy-1','BTC/USDT','Agent','buy',10,0,0,'original',trade_mode='SPOT_DCA',config_id='owner',amount=1,status='OPEN')
    monkeypatch.setattr(spot_orders,'_tool',lambda *a:(mt,venue.market('BTC/USDT')))
    token=current_spot_cycle_id.set('same-parent')
    try:
        with pytest.raises(ValueError,match='共享的本轮预算'):
            execute_spot_action('owner','BTC/USDT',{'action':'edit','order_id':'regular:buy-1','remaining_amount':100,'reason':'too much'},'oversized')
        assert venue.writes==[]
        result=execute_spot_action('owner','BTC/USDT',{'action':'edit','order_id':'regular:buy-1','remaining_amount':1.5,'reason':'add five'},'edit-five')
        assert result['status']=='completed'
        budget=spot_budget_status('owner',mt.runtime_config)
        assert budget['period_spent']==pytest.approx(5) and budget['lifetime_committed']==pytest.approx(15)
        execute_spot_action('owner','BTC/USDT',{'action':'edit','order_id':'regular:buy-1','remaining_amount':2,'reason':'add five'},'edit-other-five')
        with pytest.raises(ValueError,match='共享的本轮预算'):
            execute_spot_action('owner','BTC/USDT',{'action':'edit','order_id':'regular:buy-1','remaining_amount':2.1,'reason':'exceeds shared'},'over-parent')
        assert len(venue.writes)==2
    finally:
        current_spot_cycle_id.reset(token)


@pytest.mark.parametrize('rejected',[False,True])
def test_spot_edit_ack_requires_expected_parameters_and_rejection_releases_only_delta(monkeypatch,rejected):
    import ccxt
    from test_mcp_spot_exits import FakeSpotExchange
    from backend.mcp import spot_orders
    from backend.utils.spot_management import execute_spot_action,recover_spot_action
    from backend.utils.spot_execution import current_spot_cycle_id,spot_budget_status
    venue=FakeSpotExchange();mt=_spot_tool(venue)
    mt.runtime_config.update(dca_amount=10,dca_budget=100);venue.has={'editOrder':True}
    venue.orders['buy-1'].update(status='open',type='limit',price=10,amount=1,filled=0,remaining=1)
    venue.fetch_balance=lambda: {'BTC':{'total':9.99,'free':9.99,'used':0},'USDT':{'total':1000,'free':1000,'used':0}}
    def edit(*args):
        venue.writes.append(('edit',))
        if rejected:
            raise ccxt.InvalidOrder('definite rejection')
        return deepcopy(venue.orders['buy-1'])
    venue.edit_order=edit
    database.save_order_log('buy-1','BTC/USDT','Agent','buy',10,0,0,'original',trade_mode='SPOT_DCA',config_id='owner',amount=1,status='OPEN')
    monkeypatch.setattr(spot_orders,'_tool',lambda *a:(mt,venue.market('BTC/USDT')))
    token=current_spot_cycle_id.set('edit-parent')
    try:
        result=execute_spot_action('owner','BTC/USDT',{'action':'edit','order_id':'regular:buy-1','remaining_amount':1.5,'reason':'add'},'edit-exact')
        budget=spot_budget_status('owner',mt.runtime_config)
        if rejected:
            assert result['status']=='failed' and not budget['pending_unknown']
            assert budget['period_spent']==0 and budget['lifetime_committed']==10
        else:
            assert result['status']=='pending' and budget['pending_unknown']
            venue.orders['buy-1'].update(amount=1.5,filled=.1,remaining=1.4)
            resolved=recover_spot_action(mt,'owner','BTC/USDT','edit-exact',{},result)
            assert resolved['status']=='completed' and resolved['filled_amount']==pytest.approx(.1)
            venue.apiKey='a-different-physical-account'
            wrong=recover_spot_action(mt,'owner','BTC/USDT','edit-exact',{},resolved)
            assert wrong['status']=='completed' and wrong['query']['status']=='failed'
        assert len(venue.writes)==1
    finally:
        current_spot_cycle_id.reset(token)


def test_external_buy_reduction_does_not_require_new_allowance_or_create_agent_history(monkeypatch):
    from test_mcp_spot_exits import FakeSpotExchange
    from backend.mcp import spot_orders
    from backend.utils.spot_management import execute_spot_action
    venue=FakeSpotExchange();mt=_spot_tool(venue)
    mt.runtime_config.update(dca_amount=0,dca_budget=0);venue.has={'editOrder':True}
    venue.orders['buy-1'].update(status='open',type='limit',price=10,amount=1,filled=0,remaining=1)
    venue.fetch_balance=lambda:{'BTC':{'total':9.99,'free':9.99,'used':0}}
    def edit(identity,symbol,kind,side,amount,price):
        venue.writes.append(('edit',))
        venue.orders[identity].update(amount=amount,price=price,remaining=amount)
        return deepcopy(venue.orders[identity])
    venue.edit_order=edit
    monkeypatch.setattr(spot_orders,'_tool',lambda *a:(mt,venue.market('BTC/USDT')))
    result=execute_spot_action('owner','BTC/USDT',{'action':'edit','order_id':'regular:buy-1','remaining_amount':.5,'reason':'reduce'},'reduce-external')
    assert result['status']=='completed' and len(venue.writes)==1
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE config_id='owner'").fetchone()[0]==0


def test_manual_native_exit_amount_can_be_observed_and_cancelled_and_blocks_flat_release(monkeypatch):
    from test_mcp_spot_exits import FakeSpotExchange
    from backend.mcp import spot_orders
    venue=FakeSpotExchange('okx')
    mt=_spot_tool(venue)
    claim=management.claim_management(mt,'BTC/USDT','BASE')
    with management.financial_reservation(mt,claim,'native'):
        original=spot_orders._create(mt,venue.market('BTC/USDT'),{'amount':1,'exit_type':'stop_loss','stop_loss':90,'reason':'native'},'native')
    venue.algos[original['exchange_id']]['sz']='2'
    inventory=visible_inventory(mt,'BTC/USDT')
    assert inventory['reserved_quantity']==2
    row=spot_orders._rows('owner')[0]
    assert row['amount']==2 and row['payload']['requested_amount']==1
    venue.balance_extra=-9.99
    monkeypatch.setattr('backend.utils.market_data.MarketTool',lambda **k:mt)
    result=management.release_management_scope('owner',claim['claim_id'],confirmed=True)
    assert result['status']=='blocked' and result['resources']['orders'][0]['native']
    cancelled=spot_orders._cancel(mt,venue.market('BTC/USDT'),{'exit_id':original['exit_id'],'reason':'remove live native'})
    assert cancelled['status']=='cancelled'


def test_explicit_spot_release_allows_configuration_retirement_with_historical_buys(monkeypatch):
    from test_mcp_spot_exits import FakeSpotExchange
    from backend.utils.spot_config_guard import assert_spot_config_change_allowed
    venue = FakeSpotExchange()
    mt = _spot_tool(venue)
    database.save_order_log('buy-1', 'BTC/USDT', 'Agent', 'buy', 100, 0, 0, 'historical buy',
                            trade_mode='SPOT_DCA', config_id='owner', amount=10, status='FILLED')
    claim = management.claim_management(mt, 'BTC/USDT', 'BASE')
    with pytest.raises(ValueError):
        assert_spot_config_change_allowed(mt.runtime_config, None)
    venue.balance_extra = -9.99
    monkeypatch.setattr('backend.utils.market_data.MarketTool', lambda **kwargs: mt)
    assert management.release_management_scope('owner', claim['claim_id'], confirmed=True)['status'] == 'released'
    assert_spot_config_change_allowed(mt.runtime_config, None)
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE config_id='owner'").fetchone()[0] == 1


def test_spot_baseline_origin_and_oco_crossquote_reservation():
    venue = Venue(spot=True)
    venue.balance = {'ETH': {'total': 1.2, 'free': .8, 'used': .4}}
    venue.orders = [dict(id=str(i), symbol='ETH/USDC', side='sell', amount=.4, filled=0, remaining=.4,
                         status='open', info={'orderListId': 55}) for i in (1, 2)]
    mt = tool(venue=venue)
    assert visible_inventory(mt, 'ETH/USDT')['claim_owner'] is None
    claim = management.claim_management(mt, 'ETH/USDT', 'BASE')
    stock = visible_inventory(mt, 'ETH/USDT', claim=claim)
    assert stock['verified_owned'] == 0 and stock['managed_quantity'] == 1.2
    assert stock['reserved_quantity'] == .4 and stock['available_quantity'] == pytest.approx(.8)
    assert management.claim_management(tool('other', venue=venue), 'ETH/USDC', 'BASE')['claim_id'] == claim['claim_id']


def _race_claim(db_path, owner, output):
    database.DB_NAME = db_path
    try:
        output.put(('owned', management.claim_management(tool(owner), SYMBOL, 'LONG')['owner']))
    except management.ManagementConflict:
        output.put(('conflict', owner))


def test_cross_process_claim_only_one_wins():
    context = multiprocessing.get_context('spawn')
    output = context.Queue()
    processes = [context.Process(target=_race_claim, args=(database.DB_NAME, owner, output)) for owner in ('first', 'second')]
    for process in processes:
        process.start()
    results = [output.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert sorted(r[0] for r in results) == ['owned', 'owned']
