import json
from types import SimpleNamespace

import pytest


def test_prompt_open_orders_drops_stale_local_orders_and_marks_live_orders():
    from backend.utils.trade_context import project_trade_state, render_orders, render_positions

    plan = {
        'symbol': 'ETH/USDT',
        'account_scope': 'acct',
        'side': 'LONG',
        'episode_id': 'ep-1',
        'execution_mode': 'independent_exits',
        'state': 'ACTIVE',
        'contract_size': 1,
        'exits': [
            {
                'id': 'sl-1',
                'client_id': 'c-sl-1',
                'order_type': 'stop_market',
                'status': 'open',
                'waiting_trigger': True,
                'fill_verified': True,
                'filled': 0,
                'amount': 0.5,
                'quantity_mode': 'fixed',
                'trigger_price': 2000,
            }
        ],
        'entries': [],
    }
    clean_empty_account = {
        'hedged': True,
        'real_positions': [],
        'real_open_orders': [],
    }
    state = project_trade_state(
        clean_empty_account,
        config_id='cfg',
        account_scope='acct',
        symbol='ETH/USDT',
        plans=[plan],
        contract_size=1,
    )
    assert state.orders == []
    assert not any(i['reason_code'] == 'order_snapshot_conflict' for i in state.issues)
    assert '无挂单。' in render_orders(state)

    live_order_account = {
        'hedged': True,
        'real_positions': [{'side': 'LONG', 'amount': 0.5, 'entry_price': 2200}],
        'real_open_orders': [
            {
                'order_id': 'regular:999',
                'pos_side': 'LONG',
                'order_type': 'limit',
                'status': 'open',
                'price': 2400,
                'remaining': 0.5,
            }
        ],
    }
    live_state = project_trade_state(
        live_order_account,
        config_id='cfg',
        account_scope='acct',
        symbol='ETH/USDT',
        plans=[],
        contract_size=1,
    )
    assert len(live_state.positions) == 1
    assert live_state.positions[0]['ownership'] == 'task'
    assert not any(i['reason_code'] == 'ownership_unverified' for i in live_state.issues)
    assert len(live_state.orders) == 1
    assert live_state.orders[0]['ownership'] == 'task'
    assert live_state.orders[0]['managed'] is True
    assert live_state.orders[0]['verified'] is True
    assert 'ETH' not in render_positions(live_state) or '多头' in render_positions(live_state)

    partial_account = {
        'hedged': True,
        'real_positions': [],
        'real_open_orders': [],
        'evidence_state': {'trigger_orders': {'error': 'timeout'}},
    }
    partial_state = project_trade_state(
        partial_account,
        config_id='cfg',
        account_scope='acct',
        symbol='ETH/USDT',
        plans=[plan],
        contract_size=1,
    )
    assert len(partial_state.orders) == 1
    assert partial_state.orders[0]['order_id'] == 'algo:sl-1'


def test_independent_exits_reconcile_records_stop_loss_even_with_stale_ownership(monkeypatch):
    from backend.utils import execution_metrics
    from backend.utils.independent_exits import IndependentExits

    observed_calls = []
    saved_plans = []

    monkeypatch.setattr(
        execution_metrics,
        'observe',
        lambda service, plan, pos, reference=None: observed_calls.append(
            {'state': plan['state'], 'exit_reason': plan.get('exit_reason'), 'pos': pos}
        ),
    )

    service = IndependentExits.__new__(IndependentExits)
    service.account_scope = 'acct'
    service.config_id = 'mcp:profile-1'
    service.ex = SimpleNamespace(
        id='binanceusdm',
        fetch_positions=lambda symbols: [],
        market=lambda symbol: {'symbol': symbol, 'contractSize': 1},
    )
    service.mt = SimpleNamespace(
        config_id='mcp:profile-1',
        exchange=service.ex,
        runtime_config={'position_management_policy': 'exclusive_scope'},
    )
    monkeypatch.setattr(service, '_save', lambda p: saved_plans.append(dict(p)))
    monkeypatch.setattr(service, '_cancel', lambda record, plan, is_stop: None)
    monkeypatch.setattr(service, '_query', lambda record, symbol: {'id': record['id'], 'status': 'closed', 'filled': 1.0})

    plan = {
        'symbol': 'ETH/USDT:USDT',
        'side': 'LONG',
        'account_scope': 'acct',
        'episode_id': 'ep-sl',
        'execution_mode': 'independent_exits',
        'state': 'ACTIVE',
        'ever_filled': True,
        'ownership_error': 'Exclusive position evidence is incomplete',
        'management': {'policy': 'exclusive_scope', 'sync_status': 'partial'},
        'entries': [{'id': 'e1', 'client_id': 'ce1', 'status': 'closed', 'filled': 1.0, 'amount': 1.0, 'fill_verified': True}],
        'exits': [
            {
                'id': 'sl1',
                'client_id': 'csl1',
                'exit_type': 'stop_market',
                'order_type': 'stop_market',
                'quantity_mode': 'all',
                'cleanup_on_fill': True,
                'status': 'closed',
                'filled': 1.0,
                'amount': 1.0,
                'fill_verified': True,
            }
        ],
    }

    service._reconcile(plan)
    assert plan['state'] == 'DONE'
    assert plan['error'] is None
    assert 'ownership_error' not in plan
    assert plan['exit_reason'] == 'stop_loss'
    assert observed_calls == [{'state': 'DONE', 'exit_reason': 'stop_loss', 'pos': None}]

    observed_calls.clear()
    fixed_plan = {
        **plan,
        'state': 'ACTIVE',
        'exit_reason': None,
        'exits': [{**plan['exits'][0], 'quantity_mode': 'fixed', 'cleanup_on_fill': False}],
    }
    service._reconcile(fixed_plan)
    assert fixed_plan['exit_reason'] == 'stop_loss'
    assert len(observed_calls) == 1 and observed_calls[0]['exit_reason'] == 'stop_loss'


def test_relaxed_management_checks_and_clean_prompt_labels():
    from backend.mcp.guard import assert_position_owner
    from backend.utils.trade_context import DecisionTradeState
    from backend.utils.trade_context_format import _management, render_orders, render_positions
    from backend.utils.visible_management import _existing_owner

    fake_mt = SimpleNamespace(
        config_id='task-b',
        runtime_config={'position_management_policy': 'exclusive_scope'},
    )
    assert_position_owner(fake_mt, 'ETH/USDT', plan_override={'ownership_error': 'old', 'management': {'sync_status': 'partial'}})

    class FakeConn:
        def execute(self, sql, params=()):
            class Rows:
                def __init__(self, rows):
                    self._rows = rows

                def __iter__(self):
                    return iter(self._rows)

            if 'pg_tables' in sql:
                return Rows([('position_management_scopes',)])
            return Rows([{'owner': 'task-a', 'side': 'LONG'}, {'owner': 'task-b', 'side': 'LONG'}])

    owner = _existing_owner(FakeConn(), SimpleNamespace(config_id='task-b', exchange=SimpleNamespace(id='binance')), 'ETH/USDT:USDT', 'LONG')
    assert owner == 'task-b'

    assert _management({'management_state': 'shared'}) == ''
    assert _management({'management': {'state': 'owned'}}) == ''

    state = DecisionTradeState(
        scope={'symbol': 'ETH/USDT'},
        positions=[{
            'side': 'LONG',
            'amount': 0.5,
            'entry_price': 2000,
            'unrealized_pnl': 10,
            'ownership': 'task',
            'management_state': 'shared',
        }],
        orders=[{
            'order_id': 'regular:101',
            'side': 'LONG',
            'role': 'exit',
            'order_type': 'stop_market',
            'status': 'waiting_trigger',
            'trigger_price': 1950,
            'quantity_mode': 'all',
            'remaining': None,
            'ownership': 'task',
            'management_state': 'shared',
            'verified': True,
        }],
        coverage={},
        issues=[],
        evidence_state={},
    )
    combined = render_positions(state) + '\n' + render_orders(state)
    for noise in ['授权共管', '已纳管', '其他管理者占用', '可自动认领']:
        assert noise not in combined


def test_news_settings_removes_cv_sources_and_cleans_fallback_models():
    from backend.app.schemas.news import NewsSettings, default_news_sources

    defaults = default_news_sources()
    assert not any(s.get('kind') == 'cryptocurrency_cv' or str(s.get('id', '')).startswith('cv_') for s in defaults)

    migrated = NewsSettings.model_validate({
        'summarizer_provider_id': 'p1',
        'fallback_summarizer_provider_ids': ['p1', ' p2 ', '', 'p2', 'p3'],
        'sources': [
            {'id': 'cv_general', 'name': 'Crypto News · general', 'kind': 'cryptocurrency_cv', 'url': 'https://cryptocurrency.cv/api/news'},
            {'id': 'cv_institutional', 'name': 'Crypto News · institutional', 'kind': 'cryptocurrency_cv', 'url': 'https://cryptocurrency.cv/api/news'},
            {'id': 'coindesk', 'name': 'CoinDesk', 'kind': 'rss', 'url': 'https://www.coindesk.com/arc/outboundfeeds/rss/'},
        ],
    })
    assert [s.id for s in migrated.sources] == ['coindesk']
    assert migrated.fallback_summarizer_provider_ids == ['p2', 'p3']


def test_news_summary_fallback_uses_backup_provider(monkeypatch):
    from backend.app.schemas.news import NewsSettings
    from backend.app.services import news_service
    from backend.database import polymarket as db_polymarket

    failed_errors = []

    class DummyProgress:
        value = {'stage': 'init'}

        def update(self, **kwargs):
            if 'stage' in kwargs:
                self.value['stage'] = kwargs['stage']

        def candidates(self, items):
            pass

        def selected(self, indices):
            pass

        def item(self, *args, **kwargs):
            pass

        def finish(self, *args, **kwargs):
            pass

        def fail(self, exc):
            failed_errors.append(exc)

    snapshot = {
        'news': {
            'enabled': True,
            'scoring_mode': 'off',
            'summarizer_provider_id': 'primary',
            'fallback_summarizer_provider_ids': ['jev', 'backup'],
        },
        'polymarket': {'enabled': False},
        'llm_providers': [
            {'provider_id': 'primary', 'model': 'gpt-primary', 'api_key': 'k1', 'api_protocol': 'chat'},
            {'provider_id': 'jev', 'model': 'jev-v1', 'api_key': 'k2', 'api_protocol': 'decisions'},
            {'provider_id': 'backup', 'model': 'gpt-backup', 'api_key': 'k3', 'api_protocol': 'chat'},
        ],
    }

    settings = NewsSettings.model_validate(snapshot['news'])
    providers = news_service._summary_providers(snapshot, settings)
    assert [p['provider_id'] for p in providers] == ['primary', 'backup']

    published = []
    calls = []

    monkeypatch.setattr(news_service, 'collect_candidates', lambda *a: ([{'title': 'BTC rally', 'category': 'crypto'}], {'coindesk': {'status': 'ok'}}, {}))
    monkeypatch.setattr(news_service.database_news, 'update_state', lambda *a, **k: None)
    monkeypatch.setattr(news_service.database_news, 'publish_events', lambda *a: True)
    monkeypatch.setattr(news_service.database_news, 'latest_snapshot', lambda: None)
    monkeypatch.setattr(news_service.database_news, 'now_iso', lambda: '2026-11-24T00:00:00Z')
    monkeypatch.setattr(news_service.database_news, 'publish_snapshot', lambda payload, owner: published.append(payload) or True)
    monkeypatch.setattr(db_polymarket, 'fail_review', lambda *a, **k: None)

    def fake_summarize(items, provider):
        calls.append(provider['provider_id'])
        if provider['provider_id'] == 'primary':
            raise RuntimeError('primary upstream 503')
        return 'backup summary ok'

    monkeypatch.setattr(news_service, '_summarize', fake_summarize)
    news_service._process_refresh('owner-1', snapshot, DummyProgress())
    assert calls == ['primary', 'backup']
    assert not failed_errors
    assert len(published) == 1
    assert published[0]['digest'] == 'backup summary ok'
    assert published[0]['summary_model'] == 'gpt-backup'

    monkeypatch.setattr(news_service, '_summarize', lambda *a: (_ for _ in ()).throw(RuntimeError('all down')))
    news_service._process_refresh('owner-2', snapshot, DummyProgress())
    assert len(failed_errors) == 1
    assert 'All news summary models failed' in str(failed_errors[0])


def test_polymarket_decision_handles_extra_removed_expired_items_and_fallback(monkeypatch):
    from backend.app.services import polymarket_review as review

    candidates = {'btc-nov': {'slug': 'btc-nov'}, 'keep-old': {'slug': 'keep-old'}}
    previous = ['keep-old', 'drop-eligible', 'expired-old']
    raw = """```json
    {
      "selected": [
        {"slug": "keep-old", "reason": "仍然高度相关"},
        {"slug": "btc-nov", "reason": "BTC价格波动核心事件"}
      ],
      "removed": [
        {"slug": "drop-eligible", "reason": "相关性减弱"},
        {"slug": "rejected-candidate-1", "reason": "候选未入选"}
      ]
    }
    ```"""
    slugs, decisions = review._decision(raw, {**candidates, 'drop-eligible': {'slug': 'drop-eligible'}}, previous)
    assert slugs == ['keep-old', 'btc-nov']
    by_slug = {d['slug']: d for d in decisions}
    assert 'rejected-candidate-1' not in by_slug
    assert by_slug['keep-old']['action'] == 'keep'
    assert by_slug['btc-nov']['action'] == 'add'
    assert by_slug['drop-eligible']['action'] == 'remove'
    assert by_slug['expired-old'] == {
        'slug': 'expired-old',
        'action': 'remove',
        'reason': '事件已到期、关闭或流动性不足，自动移出监控列表。',
    }

    # Wait: keep-old IS in eligible candidates so omitting it without reason MUST still fail:
    with pytest.raises(review.ReviewError, match='缺少移除理由'):
        review._decision(
            json.dumps({'selected': [{'slug': 'btc-nov', 'reason': 'ok'}], 'removed': []}),
            {**candidates, 'keep-old': {'slug': 'keep-old'}},
            ['keep-old'],
        )

    # Contradiction: same slug in selected and removed
    with pytest.raises(review.ReviewError, match='移除事件与当前列表不一致'):
        review._decision(
            json.dumps({
                'selected': [{'slug': 'btc-nov', 'reason': 'ok'}],
                'removed': [{'slug': 'btc-nov', 'reason': 'no'}],
            }),
            candidates,
            ['btc-nov'],
        )

    seen_models = []

    def fake_generate(provider, *args, **kwargs):
        seen_models.append(provider['provider_id'])
        if provider['provider_id'] == 'p1':
            raise TimeoutError('p1 timeout')
        return 'ok-from-p2'

    monkeypatch.setattr(review, 'generate', fake_generate)
    out = review._generate_with_fallback(
        [{'provider_id': 'p1', 'model': 'm1'}, {'provider_id': 'p2', 'model': 'm2'}],
        'sys',
        {},
        'polymarket_review',
        timeout_seconds=10,
    )
    assert out == 'ok-from-p2'
    assert seen_models == ['p1', 'p2']


def test_news_context_disables_cryptocurrency_cv_by_default(monkeypatch):
    from backend.utils import news_context, polymarket

    monkeypatch.delenv('CRYPTOCURRENCY_CV_ENABLED', raising=False)
    monkeypatch.setattr(news_context, '_fetch_cryptocurrency_cv', lambda *a, **k: (_ for _ in ()).throw(AssertionError('cv called')))
    monkeypatch.setattr(news_context, '_fetch_bls_calendar', lambda *a, **k: [])
    monkeypatch.setattr(news_context, '_fetch_bea_calendar', lambda *a, **k: [])
    monkeypatch.setattr(news_context, '_fetch_fomc_calendar', lambda *a, **k: [])
    monkeypatch.setattr(news_context, '_fetch_feed_url', lambda *a, **k: [])
    monkeypatch.setattr(news_context, '_fetch_policy_source', lambda *a, **k: [])
    monkeypatch.setattr(news_context, '_fetch_treasury_market_news', lambda *a, **k: [])
    monkeypatch.setattr(polymarket, 'get_polymarket_context', lambda *a, **k: {'enabled': False, 'events': []})

    result = news_context._fetch_legacy_news_risk_context('BTC/USDT')
    assert 'cryptocurrency.cv' not in result['source']
