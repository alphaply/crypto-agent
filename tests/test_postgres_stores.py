"""Native-store regressions against an explicitly selected disposable PostgreSQL DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql
import pytest

from backend import storage


@pytest.fixture
def pg(monkeypatch, tmp_path):
    address = os.getenv('TEST_POSTGRES_URL')
    if not address:
        pytest.skip('Set TEST_POSTGRES_URL to an isolated PostgreSQL test database')
    parsed = urlsplit(address)
    if 'test' not in parsed.path.lower():
        pytest.fail('TEST_POSTGRES_URL must identify a disposable database whose name contains test')
    # No production dotenv or credentials may enter these tests, including imports.
    import dotenv
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *args, **kwargs: False)
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'postgres-test-master-key')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    schema = 'stores_' + uuid.uuid4().hex
    with psycopg.connect(address, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    query = dict(parse_qsl(parsed.query))
    query['options'] = f'-c search_path={schema}'
    scoped = urlunsplit(parsed._replace(query=urlencode(query, quote_via=quote)))
    monkeypatch.setenv('DATABASE_URL', scoped)
    storage.close_pool()
    try:
        with storage.get_connection() as conn:
            baseline = Path('backend/storage/migrations/0001_application.sql').read_text(encoding='utf-8')
            conn.execute(baseline)
            conn.commit()
        yield storage.get_connection
    finally:
        storage.close_pool()
        with psycopg.connect(address, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_balance_prices_and_scoped_spot_fills(pg):
    from backend.database_balance import BalanceHistoryStore
    from backend.database_orders import OrderPersistenceStore
    from backend.database_pricing import PricingStore
    stamp = lambda: '2026-10-15 08:00:00'
    prices = PricingStore(pg)
    prices.upsert('模型', 0.123456789123456, 5.5)
    prices.upsert('模型', 0.223456789123456, 7.5)
    assert prices.get_all()['模型']['input_price_per_m'] == 0.223456789123456
    assert 'storage_id' not in prices.get_all()['模型']
    balances = BalanceHistoryStore(pg, stamp)
    balances.save_snapshot('BTC/USDT', 100, -2.5, 'one')
    history = balances.get_history('BTC/USDT', config_id='one')[0]
    assert history['total_equity'] == 97.5 and 'storage_id' not in history
    orders = OrderPersistenceStore(pg, stamp)
    orders.upsert_spot_fill('same-order', 'one', 'BTC/USDT', 'FILLED', 1, 20, 20)
    orders.upsert_spot_fill('same-order', 'two', 'BTC/USDT', 'FILLED', 2, 40, 20)
    orders.upsert_spot_fill('same-order', 'one', 'BTC/USDT', 'FILLED', 3, 60, 20)
    with pg() as conn:
        rows = conn.execute('SELECT config_id,filled_qty FROM spot_order_fills ORDER BY config_id').fetchall()
    assert [tuple(row) for row in rows] == [('one', 3), ('two', 2)]
    assert prices.delete('模型') == 1


def test_rule_idempotency_revision_and_rollback(pg):
    from backend.database_rules import change_trading_rules, list_trading_rules, RuleConflictError
    changes = [{'action': 'add', 'content': '中文规则'}]
    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(lambda _: change_trading_rules('task', changes, actor='human', operation_id='same'), range(2)))
    assert results[0] == results[1]
    rule_id = results[0][0]['rule_id']
    assert len(list_trading_rules('task')) == 1
    assert 'storage_id' not in results[0][0] and 'storage_id' not in list_trading_rules('task')[0]
    with pytest.raises(RuleConflictError):
        change_trading_rules('task', [{'action': 'update', 'rule_id': rule_id, 'content': 'bad', 'expected_revision': 0}], actor='human')
    assert list_trading_rules('task')[0]['content'] == '中文规则'


def test_news_leases_and_cache_preserve_identity(pg):
    from backend import database_news as news
    with ThreadPoolExecutor(2) as workers:
        owners = list(workers.map(lambda _: news.acquire_lease('news'), range(2)))
    assert sum(owner is not None for owner in owners) == 1
    owner = next(owner for owner in owners if owner)
    assert news.acquire_lease('pricing') is not None
    assert news.publish_snapshot({'headlines': ['新消息']}, owner)
    assert news.latest_snapshot()['headlines'] == ['新消息']
    news.save_score('fingerprint', {'score': 1})
    with pg() as conn:
        identity = conn.execute('SELECT storage_id FROM news_score_cache').fetchone()[0]
    news.save_score('fingerprint', {'score': 2})
    assert news.get_score('fingerprint') == {'score': 2}
    with pg() as conn:
        assert conn.execute('SELECT storage_id FROM news_score_cache').fetchone()[0] == identity
    news.save_announcement({'id': 'notice', 'published_at': news.now_iso(), 'title': '公告'})
    news.save_announcement({'id': 'notice', 'published_at': news.now_iso(), 'title': 'duplicate'})
    assert len(news.announcements('2020')) == 1
    news.save_processing_run(owner, {'status': 'running', 'started_at': news.now_iso()})
    assert news.processing_run(owner)['status'] == 'running'
    assert news.update_state('news', {'status': 'done'}, owner=owner, release=True)


def test_independent_account_reservations_and_operation_replay(pg):
    from backend.database_independent import MockIndependentTrading
    op = SimpleNamespace(action='BUY_LIMIT', amount=60, entry_price=100, stop_loss=None,
                         take_profit=None, reason='test', valid_duration_hours=24)
    trader = MockIndependentTrading('account', 'BTC/USDT')
    with ThreadPoolExecutor(2) as workers:
        receipts = list(workers.map(lambda _: trader.open(op, operation_id='same'), range(2)))
    assert receipts[0] == receipts[1]
    with pytest.raises(ValueError, match='available simulated funds'):
        MockIndependentTrading('account', 'ETH/USDT').open(op, operation_id='another')
    with pg() as conn:
        assert conn.execute('SELECT count(*) FROM mock_orders').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM mock_trade_operations').fetchone()[0] == 1


def test_concurrent_account_credits_do_not_lose_updates(pg):
    from backend.database_mock import MockTradingStore
    mock = MockTradingStore(pg, lambda: datetime.now(timezone.utc), lambda: 1, lambda **kw: None, logging.getLogger('test'))
    mock.get_account('account', 'BTC/USDT')
    with ThreadPoolExecutor(4) as workers:
        list(workers.map(lambda _: mock.update_account_balance('account', 'BTC/USDT', 11), range(12)))
    account = mock.get_account('account', 'BTC/USDT')
    assert account['balance'] == 10132 and 'storage_id' not in account
    with pg() as conn:
        assert conn.execute('SELECT count(*) FROM mock_balance_history').fetchone()[0] == 12


def test_trade_observations_scope_and_bad_row_savepoint(pg):
    from backend.database_trade import TradeHistoryStore
    store = TradeHistoryStore(pg, logging.getLogger('test'))
    trade = dict(id='fill', order='order', timestamp=1792015200000, symbol='BTC/USDT', side='buy',
                 price=10, amount=2, cost=20, realizedPnl=1)
    store.save_trades([trade])
    store.save_trades([trade], config_id='task')
    store.save_trades([trade], config_id='task')
    bad = {**trade, 'id': 'bad', 'price': 'invalid'}
    store.save_trades([bad, {**trade, 'id': 'good'}], config_id='task')
    history = store.list_trades('BTC/USDT')
    assert {row['trade_id'] for row in history} == {'fill', 'good'}
    assert all('storage_id' not in row for row in history)


def test_chat_and_memory_and_summary_returning(pg, monkeypatch):
    from backend.database_chat import ChatSessionStore
    from backend.database_memory import SummaryMemoryStore
    from backend.database_summary import SummaryStore
    from backend.config import config
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: None)
    chats = ChatSessionStore(pg, lambda: '2026-10-15 08:00:00')
    chats.create_session('a', 'task', 'BTC/USDT', '会话')
    chats.create_session('b', 'task', 'BTC/USDT', 'second')
    claim = chats.claim_title_summary('a', 10)
    assert claim and chats.claim_title_summary('a', 10) is None
    assert chats.update_title('a', 'new title', summary_token=claim)
    assert not chats.update_title('a', 'stale', summary_token='bad')
    assert chats.delete_session('a') == 1 and chats.get_session('b')
    assert 'storage_id' not in chats.get_session('b')
    summaries = SummaryStore(pg, lambda: '2026-10-15 08:00:00', logging.getLogger('test'))
    identifier = summaries.save_summary('BTC/USDT', 'agent', 'content', '', config_id='task', run_id='run', enqueue_memory=False)
    assert isinstance(identifier, int)
    assert summaries.save_summary('BTC/USDT', 'agent', 'content', '', config_id='task', run_id='run') == identifier
    memories = SummaryMemoryStore(pg, lambda: datetime.now(timezone.utc), logging.getLogger('test'))
    memories.save_short_memory('2026-10-15 00:00:00', '2026-10-15 01:00:00', 'BTC/USDT', 'task', '中文记忆', '', 1, run_id='run')
    first = memories.get_short_memory('task', '2026-10-15 00:00:00')
    assert 'storage_id' not in first
    memories.save_short_memory('2026-10-15 00:00:00', '2026-10-15 01:00:00', 'BTC/USDT', 'task', 'changed', '', 1, run_id='run2')
    assert memories.get_short_memory('task', '2026-10-15 00:00:00')['version'] == first['version']


def test_usage_large_counters_and_finish_idempotency(pg):
    from backend.database_usage import start_usage, finish_usage
    with pg() as conn:
        start_usage(conn, 'run', config_id='task', purpose='chat', model='test', provider_id=None,
                    started_at='2026-10-15T00:00:00+00:00', prices={'input_price_per_m': 1, 'output_price_per_m': 2})
        result = finish_usage(conn, 'run', {'prompt_tokens': 3000000000, 'completion_tokens': 2}, 'success', '2026-10-15T00:01:00+00:00')
        assert result['total_tokens'] == 3000000002 and 'storage_id' not in result
        assert finish_usage(conn, 'run', {}, 'error', 'later')['status'] == 'success'
        conn.commit()


def test_config_import_target_key_round_trip_and_hook_rollback(pg, monkeypatch, tmp_path):
    from backend import config_store
    monkeypatch.setattr(config_store, 'load_dotenv', lambda *a, **k: False)
    package = {'version': 1, 'app_settings': {'enable_scheduler': False},
               'agents': [{'config_id': 'original', 'symbol': 'BTC/USDT', 'mode': 'STRATEGY',
                           'llm_provider_id': 'provider', 'enabled': False}],
               'llm_providers': [{'provider_id': 'provider', 'name': '原模型', 'model': 'test-model',
                                  '_secrets': {'api_key': 'fixture-key'}}],
               'exchange_profiles': [], 'global_secrets': {},
               'env': {'CONFIG_MASTER_KEY': 'different-old-key'}}
    config_store.import_full_snapshot(package, write_env=False, prompt_files={'custom.txt': '我的提示词'},
                                      model_pricing=[{'model': 'test-model', 'input_price_per_m': 1.25, 'output_price_per_m': 2.5}])
    loaded = config_store.load_runtime_snapshot()
    assert loaded['llm_providers'][0]['api_key'] == 'fixture-key'
    assert loaded['agents'][0]['config_id'] == 'original'
    assert os.environ['CONFIG_MASTER_KEY'] == 'postgres-test-master-key'
    assert (tmp_path / 'prompts' / 'custom.txt').read_text(encoding='utf-8') == '我的提示词'
    exported = config_store.export_full_snapshot()
    assert exported['llm_providers'][0]['_secrets'] == {'api_key': 'fixture-key'}
    with pg() as conn:
        encrypted = conn.execute('SELECT encrypted_value FROM secret_store').fetchone()[0]
        assert 'fixture-key' not in encrypted
        assert conn.execute('SELECT count(*) FROM orders').fetchone()[0] == 0
    def fail_hook(conn):
        conn.execute("INSERT INTO app_settings(key,value_json,updated_at) VALUES ('hook','1','now')")
        raise ValueError('hook rejected')
    with pytest.raises(ValueError, match='hook rejected'):
        config_store.import_full_snapshot({**package, 'app_settings': {'enable_scheduler': True}},
                                          transaction_hook=fail_hook, prompt_files={'custom.txt': 'do not write'})
    assert config_store.load_runtime_snapshot()['enable_scheduler'] is False
    assert (tmp_path / 'prompts' / 'custom.txt').read_text(encoding='utf-8') == '我的提示词'
    with pytest.raises(ValueError, match='version'):
        config_store.import_full_snapshot({'version': 2}, prompt_files={'custom.txt': 'bad version'})


def test_stats_and_dashboard_date_and_like_queries(pg, monkeypatch):
    from backend import database
    from backend.app.services import stats_service, dashboard_service
    database._balance_history_store.save_snapshot('BTC/USDT', 100, 1, 'task')
    database._order_persistence_store.save_order_log('order', 'BTC/USDT', 'agent', 'BUY_LIMIT', 10, None, None, '', config_id='task')
    result = stats_service.get_financial_stats_payload('BTC/USDT')
    assert result['daily_equity'][0]['total_equity'] == 101
    assert database.get_agent_trade_stats('task')['buy_count'] == 1
    assert dashboard_service.build_config_compare_rows('BTC/USDT', [{'config_id': 'task'}])[0]['long_count'] == 1


def test_redacted_package_has_no_fabricated_secrets_and_env_cannot_replace_database(pg, monkeypatch, tmp_path):
    from backend import config_store
    monkeypatch.setattr(config_store, 'BASE_DIR', tmp_path)
    package = {'version': 1, 'agents': [], 'app_settings': {'enable_scheduler': False},
               'llm_providers': [{'provider_id': 'redacted', 'name': 'Redacted', 'model': 'model', '_secrets': {}}],
               'env': {'DATABASE_URL': 'postgresql://not-selected/database', 'PORT': '7860'}}
    result = config_store.import_full_snapshot(package, write_env=True)
    assert result['env_written'] == ['PORT']
    assert 'DATABASE_URL' not in (tmp_path / '.env').read_text(encoding='utf-8')
    assert config_store.export_full_snapshot()['llm_providers'][0]['_secrets'] == {}
    with pg() as conn:
        assert conn.execute('SELECT count(*) FROM secret_store').fetchone()[0] == 0


def test_failed_candidate_validation_does_not_write_config_or_prompts(pg, tmp_path):
    from backend import config_store
    from backend.config import Config
    config_store.save_runtime_snapshot({'enable_scheduler': False, 'langchain_project': 'preserved'}, [])
    package = {'version': 1, 'app_settings': {'enable_scheduler': True},
               'agents': [{'config_id': 'invalid', 'mode': 'SPOT_DCA', 'symbols': ['BTC/USDT'],
                           'exchange_profile_id': 'public-only', 'enabled': False}],
               'exchange_profiles': [{'profile_id': 'public-only', 'name': 'Public only', 'exchange': 'binance', 'market_type': 'spot'}]}
    with pytest.raises(ValueError, match='missing API credentials'):
        config_store.import_full_snapshot(package, validate_snapshot=Config.validate_snapshot,
                                          prompt_files={'never.txt': 'must not be written'})
    assert config_store.load_runtime_snapshot()['langchain_project'] == 'preserved'
    assert not (tmp_path / 'prompts' / 'never.txt').exists()


def test_import_reports_post_commit_file_failure(pg, monkeypatch):
    from backend import config_store
    from backend.app.services import common
    monkeypatch.setattr(common, 'prompt_dir', lambda: (_ for _ in ()).throw(OSError('injected disk failure')))
    with pytest.raises(RuntimeError, match='database import committed; Prompt files'):
        config_store.import_full_snapshot({'version': 1, 'agents': [], 'app_settings': {'langchain_project': 'committed'}},
                                          prompt_files={'a.txt': 'text'})
    assert config_store.load_runtime_snapshot()['langchain_project'] == 'committed'


def test_snapshot_read_cannot_mix_two_committed_configs(pg, monkeypatch):
    from backend import config_store
    config_store.save_runtime_snapshot({'langchain_project': 'old'}, [{'config_id': 'old', 'symbol': 'BTC/USDT', 'mode': 'STRATEGY'}])
    started, written = threading.Event(), threading.Event()
    original = config_store._has_runtime_storage
    def after_snapshot(conn):
        result = original(conn)
        if threading.current_thread().name.startswith('snapshot-reader'):
            started.set()
            assert written.wait(5)
        return result
    monkeypatch.setattr(config_store, '_has_runtime_storage', after_snapshot)
    with ThreadPoolExecutor(1, thread_name_prefix='snapshot-reader') as workers:
        reader = workers.submit(config_store.load_runtime_snapshot)
        assert started.wait(5)
        try:
            config_store.save_runtime_snapshot({'langchain_project': 'new'}, [{'config_id': 'new', 'symbol': 'BTC/USDT', 'mode': 'STRATEGY'}])
        finally:
            written.set()
        snapshot = reader.result()
    assert snapshot['langchain_project'] == 'old'
    assert snapshot['agents'][0]['config_id'] == 'old'


def test_delayed_startup_seed_does_not_overwrite_import(pg, monkeypatch):
    from backend import config_store
    started, imported = threading.Event(), threading.Event()
    def seed_snapshot():
        started.set()
        assert imported.wait(5)
        return {'agents': [], 'langchain_project': 'environment'}
    monkeypatch.setattr(config_store, '_env_snapshot', seed_snapshot)
    with ThreadPoolExecutor(1) as workers:
        seed = workers.submit(config_store.ensure_runtime_config_initialized)
        assert started.wait(5)
        try:
            config_store.save_runtime_snapshot({'langchain_project': 'imported'}, [])
        finally:
            imported.set()
        seed.result()
    assert config_store.load_runtime_snapshot()['langchain_project'] == 'imported'


def test_database_failure_is_not_an_empty_dashboard(pg, monkeypatch):
    from backend.app.services import dashboard_service
    from contextlib import contextmanager
    @contextmanager
    def unavailable():
        raise storage.OperationalError('database unavailable')
        yield
    monkeypatch.setattr(dashboard_service, 'get_db_conn', unavailable)
    with pytest.raises(storage.OperationalError, match='unavailable'):
        dashboard_service.get_dashboard_data(None)


