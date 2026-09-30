from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.utils import exit_policy


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'policy.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)


def test_backward_compatible_defaults_and_explicit_entry_policy():
    assert exit_policy.effective_exit_mode({'mode': 'REAL'}) == 'attached_optional'
    assert exit_policy.effective_exit_mode({'mode': 'STRATEGY'}) == 'attached_required'
    for mode, sl, tp, valid in [('attached_optional', None, None, True),
                                ('attached_required', None, 120, False),
                                ('attached_required', 90, 120, True),
                                ('independent_exits', None, None, True),
                                ('independent_exits', 90, None, False)]:
        if valid:
            exit_policy.validate_entry_protection({'exit_mode': mode}, SimpleNamespace(stop_loss=sl, take_profit=tp))
        else:
            with pytest.raises(ValueError):
                exit_policy.validate_entry_protection({'exit_mode': mode}, SimpleNamespace(stop_loss=sl, take_profit=tp))


@pytest.mark.parametrize('blocker', ['position', 'exit', 'unknown_operation', 'corrupt_plan'])
def test_exit_mode_change_rejects_each_active_or_uncertain_lifecycle(local_db, blocker):
    previous = {'config_id': 'cfg', 'mode': 'STRATEGY', 'exit_mode': 'independent_exits'}
    updated = {**previous, 'exit_mode': 'attached_required'}
    with database.get_db_conn() as conn:
        if blocker == 'position':
            conn.execute("INSERT INTO mock_positions(config_id,symbol,side,episode_id,quantity,status) VALUES('cfg','ETH/USDT','LONG','episode',1,'ACTIVE')")
        elif blocker == 'exit':
            conn.execute("INSERT INTO mock_exit_orders(order_id,config_id,symbol,pos_side,episode_id,exit_type,amount,remaining,status,timestamp,created_at) VALUES('exit','cfg','ETH/USDT','LONG','episode','stop_market',1,1,'OPEN','now',0)")
        elif blocker == 'unknown_operation':
            conn.execute("INSERT INTO trade_action_runs VALUES('cfg','operation','ETH/USDT','hash','unknown',NULL,0,0)")
        else:
            conn.execute("INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES('cfg','ETH/USDT','LONG','corrupt-json')")
        conn.commit()
    with pytest.raises(exit_policy.ExitModeConflict):
        exit_policy.assert_exit_mode_change_allowed(previous, updated)
    # Keeping the same policy is allowed even while positions exist.
    exit_policy.assert_exit_mode_change_allowed(previous, previous)


def test_real_policy_change_requires_fresh_empty_position_and_both_order_sources(local_db, monkeypatch):
    from backend.utils import market_data
    exchange = Mock()
    exchange.fetch_positions.return_value = []
    exchange.fetch_open_orders.side_effect = [[], [{'id': 'trigger-order'}]]
    monkeypatch.setattr(market_data, 'MarketTool', lambda **_: SimpleNamespace(exchange=exchange))
    before = {'config_id': 'cfg', 'mode': 'REAL', 'symbol': 'ETH/USDT', 'exit_mode': 'attached_optional'}
    after = {**before, 'exit_mode': 'independent_exits'}
    with pytest.raises(exit_policy.ExitModeConflict):
        exit_policy.assert_exit_mode_change_allowed(before, after)
    exchange.fetch_open_orders.side_effect = None
    exchange.fetch_open_orders.return_value = []
    exit_policy.assert_exit_mode_change_allowed(before, after)
    exchange.fetch_positions.side_effect = TimeoutError('Cannot verify')
    with pytest.raises(exit_policy.ExitModeConflict):
        exit_policy.assert_exit_mode_change_allowed(before, after)


@pytest.mark.parametrize('mode_field', [{}, {'exit_mode': None}])
def test_settings_preserve_existing_default_and_new_task_is_required(monkeypatch, mode_field):
    from backend.app.services import config_service
    import backend.utils.llm_utils as llm_utils
    previous = {'config_id': 'old', 'mode': 'REAL'}
    monkeypatch.setattr(config_service.global_config, 'get_config_by_id', lambda cid: previous if cid == 'old' else None)
    save = Mock()
    monkeypatch.setattr(config_service, 'save_runtime_snapshot', save)
    monkeypatch.setattr(config_service.global_config, 'reload_config', lambda: None)
    monkeypatch.setattr(llm_utils, 'sync_langsmith_environment', lambda: None)
    config_service.save_config_payload({}, [{'config_id': 'old', 'mode': 'REAL', **mode_field}, {'config_id': 'new', 'mode': 'REAL', **mode_field}])
    assert [row['exit_mode'] for row in save.call_args.args[1]] == ['attached_optional', 'attached_required']
