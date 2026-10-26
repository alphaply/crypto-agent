"""Cost calibration changes valuation only; real order evidence stays intact."""
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend import config_store, database, storage
from backend.app.api import config as routes
from backend.app.core.deps import get_current_user
from backend.app.services import config_service, spot_cost_basis_service as service
from backend.config import Config
from backend.database.spot_cost_basis import buys_after_cost_basis, read_spot_buy_totals, read_spot_cost_basis
from backend.utils.spot_config_guard import SpotConfigConflict, assert_spot_config_change_allowed


@pytest.fixture
def api(pg, monkeypatch):
    monkeypatch.setattr(config_store, 'load_dotenv', lambda *args, **kwargs: None)
    config_store.save_runtime_snapshot(
        {'enable_scheduler': False},
        [
            {'config_id': 'spot', 'mode': 'SPOT_DCA', 'symbol': 'BTC/USDT',
             'symbols': ['BTC/USDT', 'ETH/USDT'], 'exchange_profile_id': 'account-a'},
            {'config_id': 'other', 'mode': 'SPOT_DCA', 'symbol': 'BTC/USDT',
             'symbols': ['BTC/USDT'], 'exchange_profile_id': 'account-a'},
            {'config_id': 'strategy', 'mode': 'STRATEGY', 'symbol': 'BTC/USDT',
             'exchange_profile_id': 'account-a'},
        ],
        exchange_profiles_payload=[
            {'profile_id': 'account-a', 'exchange': 'binance', 'market_type': 'spot',
             'api_key': 'fixture-key', 'secret': 'fixture-secret'},
        ],
        validate_snapshot=Config.validate_snapshot,
    )
    runtime = Config()
    monkeypatch.setattr(service, 'global_config', runtime)
    monkeypatch.setattr(config_service, 'global_config', runtime)
    calls = []
    real_sync = service._sync_pending_buys
    monkeypatch.setattr(service, '_sync_pending_buys', lambda *args: calls.append(args))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'test-admin'}
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, runtime=runtime, calls=calls, app=app, real_sync=real_sync)


def save(api, *, config_id='spot', symbol='BTC/USDT', quantity=1, avg_cost=100):
    return api.client.patch(f'/api/config/{config_id}/spot-cost-basis', json={
        'symbol': symbol, 'quantity': quantity, 'avg_cost': avg_cost,
    })


def basis(config_id='spot', symbol='BTC/USDT'):
    with database.get_db_conn() as conn:
        return read_spot_cost_basis(conn, config_id, symbol)


def seed_buy(order_id='old', *, config_id='spot', symbol='BTC/USDT', quantity=.5, cost=60):
    with database.get_db_conn() as conn:
        conn.execute('''INSERT INTO orders
            (order_id,config_id,symbol,trade_mode,status,side,filled_amount,filled_cost,avg_fill_price)
            VALUES (%s,%s,%s,'SPOT_DCA','FILLED','buy',%s,%s,%s)''',
            (order_id, config_id, symbol, quantity, cost, cost / quantity))
        conn.commit()


def test_save_captures_owned_buy_baseline_without_changing_orders_or_budget(api):
    seed_buy()
    seed_buy(config_id='other', quantity=9, cost=900)
    with database.get_db_conn() as conn:
        conn.execute('''INSERT INTO spot_budget_reservations
            (config_id,operation_id,cycle_id,symbol,quote_cost,status,order_id,created_at,updated_at)
            VALUES ('spot','old-operation','cycle','BTC/USDT',60,'submitted','old',1,1)''')
        conn.commit()
        orders_before = conn.execute('SELECT * FROM orders ORDER BY id').fetchall()
        budget_before = conn.execute('SELECT * FROM spot_budget_reservations').fetchall()
    response = save(api)
    assert response.status_code == 200, response.text
    assert response.json()['cost_basis']['quantity'] == 1
    saved = basis()
    assert saved['quantity'] == 1 and saved['avg_cost'] == 100
    assert saved['order_baselines'] == {'old': {'qty': .5, 'cost': 60, 'status': 'FILLED'}}
    assert api.calls == [('spot', 'BTC/USDT')]
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT * FROM orders ORDER BY id').fetchall() == orders_before
        assert conn.execute('SELECT * FROM spot_budget_reservations').fetchall() == budget_before
    assert basis('other') is None


def test_repeat_save_replaces_previous_start_and_captures_intervening_buys(api):
    seed_buy()
    assert save(api).status_code == 200
    first = basis()
    seed_buy('new', quantity=.2, cost=24)
    assert save(api, quantity=1.2, avg_cost=110).status_code == 200
    second = basis()
    assert second['quantity'] == 1.2 and second['avg_cost'] == 110
    assert second['updated_at'] != first['updated_at']
    assert set(second['order_baselines']) == {'old', 'new'}
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM spot_cost_basis').fetchone()[0] == 1


def test_costs_are_separate_for_each_symbol_and_task(api):
    assert save(api, symbol='BTC/USDT', quantity=1, avg_cost=100).status_code == 200
    assert save(api, symbol='ETH/USDT', quantity=2, avg_cost=20).status_code == 200
    assert save(api, config_id='other', quantity=3, avg_cost=50).status_code == 200
    assert basis()['quantity'] == 1
    assert basis(symbol='ETH/USDT')['avg_cost'] == 20
    assert basis('other')['quantity'] == 3


def test_zero_holding_persists_an_explicit_zero_baseline(api):
    seed_buy()
    assert save(api).status_code == 200
    assert save(api, quantity=0, avg_cost=0).status_code == 200
    saved = basis()
    assert saved is not None and saved['quantity'] == saved['avg_cost'] == 0
    assert 'old' in saved['order_baselines']


@pytest.mark.parametrize('quantity,avg_cost', [(-1, 100), (1, -100), (1, 0), (None, 100)])
def test_invalid_inputs_are_rejected_without_writing(api, quantity, avg_cost):
    response = save(api, quantity=quantity, avg_cost=avg_cost)
    assert response.status_code == 422, response.text
    assert basis() is None and api.calls == []


def test_overflowing_total_cost_is_rejected(api):
    response = save(api, quantity=1e308, avg_cost=1e308)
    assert response.status_code == 400, response.text
    assert basis() is None and api.calls == []


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity'])
def test_nonfinite_values_are_rejected(api, value):
    response = save(api, quantity=value)
    assert response.status_code == 422, response.text
    assert basis() is None and api.calls == []


def test_authentication_is_required(api):
    api.app.dependency_overrides.clear()
    assert save(api).status_code == 401
    assert basis() is None and api.calls == []


@pytest.mark.parametrize('config_id,symbol,status', [
    ('missing', 'BTC/USDT', 404),
    ('strategy', 'BTC/USDT', 400),
    ('spot', 'SOL/USDT', 400),
])
def test_task_and_symbol_scope_are_checked(api, config_id, symbol, status):
    response = save(api, config_id=config_id, symbol=symbol)
    assert response.status_code == status, response.text
    assert basis() is None and api.calls == []


def test_sync_failure_preserves_previous_basis(api, monkeypatch):
    assert save(api).status_code == 200
    before = basis()
    def unavailable(*args):
        raise service.SpotCostBasisUnavailable('买入成交暂不可核验')
    monkeypatch.setattr(service, '_sync_pending_buys', unavailable)
    response = save(api, quantity=2, avg_cost=200)
    assert response.status_code == 502
    assert basis() == before


@pytest.mark.parametrize('status', ['reserved', 'unknown', 'submitted'])
def test_unreconciled_buy_cannot_be_absorbed_as_a_new_purchase_after_save(api, status):
    with database.get_db_conn() as conn:
        conn.execute('''INSERT INTO spot_budget_reservations
            (config_id,operation_id,cycle_id,symbol,quote_cost,status,order_id,created_at,updated_at)
            VALUES ('spot','lost-order','cycle','BTC/USDT',60,%s,'missing-order',1,1)''', (status,))
        conn.commit()
    response = save(api)
    assert response.status_code == 502, response.text
    assert basis() is None
    # The unresolved order belongs to BTC only; ETH can still be calibrated.
    assert save(api, symbol='ETH/USDT').status_code == 200


def test_all_pending_orders_are_synchronized_without_a_hundred_order_cutoff(api, monkeypatch):
    with database.get_db_conn() as conn:
        conn.cursor().executemany('''INSERT INTO orders
            (order_id,config_id,symbol,trade_mode,status,side)
            VALUES (%s,'spot','BTC/USDT','SPOT_DCA',%s,'buy')''',
            [(str(index), 'UNKNOWN' if index == 100 else 'OPEN') for index in range(101)])
        conn.commit()
    calls = []
    def fetch_order(order_id, symbol):
        calls.append((order_id, symbol))
        return {'id': order_id, 'symbol': symbol, 'status': 'open', 'filled': .1,
                'cost': 10, 'average': 100}
    monkeypatch.setattr(service, '_sync_pending_buys', api.real_sync)
    # No balance or ticker methods: calibration only needs existing buy evidence.
    monkeypatch.setattr(service, 'MarketTool', lambda **kwargs: SimpleNamespace(
        exchange=SimpleNamespace(fetch_order=fetch_order)))
    response = save(api, quantity=20)
    assert response.status_code == 200, response.text
    assert len(calls) == 101
    saved = basis()
    assert len(saved['order_baselines']) == 101
    assert all(item == {'qty': .1, 'cost': 10, 'status': 'PARTIAL'}
               for item in saved['order_baselines'].values())


def test_exchange_sync_failure_does_not_replace_an_existing_basis(api, monkeypatch):
    assert save(api).status_code == 200
    before = basis()
    with database.get_db_conn() as conn:
        conn.execute('''INSERT INTO orders (order_id,config_id,symbol,trade_mode,status,side)
            VALUES ('pending','spot','BTC/USDT','SPOT_DCA','OPEN','buy')''')
        conn.commit()
    def fetch_order(*args):
        raise TimeoutError('test exchange unavailable')
    monkeypatch.setattr(service, '_sync_pending_buys', api.real_sync)
    monkeypatch.setattr(service, 'MarketTool', lambda **kwargs: SimpleNamespace(
        exchange=SimpleNamespace(fetch_order=fetch_order)))
    response = save(api, quantity=10)
    assert response.status_code == 502, response.text
    assert basis() == before


@pytest.mark.parametrize('order_status', ['FILLED', 'CANCELLED'])
def test_calibration_reconciles_terminal_order_with_older_partial_fill_snapshot(api, monkeypatch, order_status):
    seed_buy(quantity=.4, cost=40)
    with database.get_db_conn() as conn:
        conn.execute("UPDATE orders SET status=%s WHERE config_id='spot' AND order_id='old'", (order_status,))
        conn.commit()
    # Spot cancellation commits orders first and the fill snapshot separately.
    # Model that real intermediate state seen by a different web process.
    database.upsert_spot_order_fill('old', 'spot', 'BTC/USDT', 'PARTIAL', .2, 20, 100)
    calls = []

    def fetch_order(order_id, symbol):
        calls.append((order_id, symbol))
        return {
            'id': order_id, 'status': 'closed' if order_status == 'FILLED' else 'canceled',
            'filled': .4, 'cost': 40, 'average': 100,
        }

    monkeypatch.setattr(service, '_sync_pending_buys', api.real_sync)
    monkeypatch.setattr(service, 'MarketTool', lambda **kwargs: SimpleNamespace(
        exchange=SimpleNamespace(fetch_order=fetch_order)))
    response = save(api)
    assert response.status_code == 200, response.text
    assert calls == [('old', 'BTC/USDT')]
    saved = basis()
    assert saved['order_baselines']['old'] == {'qty': .4, 'cost': 40, 'status': order_status}
    # The original cancellation's delayed second commit must not add the
    # already covered .2 fill to the user-entered holding a second time.
    database.upsert_spot_order_fill('old', 'spot', 'BTC/USDT', order_status, .4, 40, 100)
    with database.get_db_conn() as conn:
        totals = read_spot_buy_totals(conn, 'spot', 'BTC/USDT')
    assert buys_after_cost_basis(totals, saved['order_baselines']) == (0, 0)


def test_equivalent_terminal_order_and_fill_statuses_do_not_require_exchange_sync(api, monkeypatch):
    seed_buy()
    database.upsert_spot_order_fill('old', 'spot', 'BTC/USDT', 'closed', .5, 60, 120)
    monkeypatch.setattr(service, '_sync_pending_buys', api.real_sync)

    def unexpected_market(**kwargs):
        raise AssertionError('Terminal evidence must not need an exchange request')

    monkeypatch.setattr(service, 'MarketTool', unexpected_market)
    response = save(api)
    assert response.status_code == 200, response.text
    assert basis()['order_baselines']['old']['status'] == 'CLOSED'


def test_account_changed_during_sync_does_not_write_basis(api, monkeypatch):
    def change_account(*args):
        with database.get_db_conn() as conn:
            row = conn.execute("SELECT data_json FROM agent_configs WHERE config_id='spot'").fetchone()
            payload = json.loads(row[0])
            payload['exchange_profile_id'] = 'changed-account'
            conn.execute("UPDATE agent_configs SET data_json=%s WHERE config_id='spot'", (json.dumps(payload),))
            conn.commit()
    monkeypatch.setattr(service, '_sync_pending_buys', change_account)
    assert save(api).status_code == 409
    assert basis() is None


def test_credentials_changed_within_same_profile_during_sync_are_rejected(api, monkeypatch):
    def rotate_credentials(*args):
        with database.get_db_conn() as conn:
            config_store._apply_secret_updates(conn, 'exchange_profile', 'account-a', {
                'api_key': {'value': 'rotated-fixture-key'},
            })
            conn.commit()
    monkeypatch.setattr(service, '_sync_pending_buys', rotate_credentials)
    response = save(api)
    assert response.status_code == 409, response.text
    assert basis() is None


def test_manual_basis_blocks_detaching_asset_or_account_but_allows_reorder(api):
    assert save(api, symbol='ETH/USDT').status_code == 200
    previous = api.runtime.get_config_by_id('spot')
    with pytest.raises(SpotConfigConflict):
        assert_spot_config_change_allowed(previous, {**previous, 'symbols': ['BTC/USDT']})
    with pytest.raises(SpotConfigConflict):
        assert_spot_config_change_allowed(previous, {**previous, 'exchange_profile_id': 'account-b'})
    assert_spot_config_change_allowed(previous, {
        **previous, 'symbols': ['ETH/USDT', 'BTC/USDT'], 'symbol': 'ETH/USDT',
    })


def test_explicit_task_deletion_removes_only_its_cost_bases(api):
    assert save(api).status_code == 200
    assert save(api, symbol='ETH/USDT').status_code == 200
    assert save(api, config_id='other').status_code == 200
    assert database.get_config_dependency_counts('spot')['spot_cost_basis'] == 2
    response = api.client.delete('/api/config/spot')
    assert response.status_code == 200, response.text
    assert response.json()['cleanup']['spot_cost_basis_deleted'] == 2
    assert basis() is None and basis(symbol='ETH/USDT') is None
    assert basis('other') is not None
