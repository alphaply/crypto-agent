import json
import pytest
from langchain_core.messages import AIMessage

from backend.app.services import news_service
from backend.app.services.news_digest import build_digest_repair_prompt, render_digest
from backend.database import agent_runs, polymarket as db_polymarket
from backend.utils import llm_utils


def _items(count=2):
    return [{'url': f'https://example.test/{i}'} for i in range(1, count + 1)]


def test_render_digest_extracts_fenced_and_prefixed_json():
    payload = {
        'overview': '市场维持震荡观望。',
        'highlights': [{'fact': 'BTC 测试关键阻力位。', 'impact': '短线波动率可能放大。', 'references': [1]}],
        'upcoming': [],
    }
    fenced = f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"
    prefixed = f"以下是整理后的 JSON 简报：\n{json.dumps(payload, ensure_ascii=False)}\n请查收。"

    for text in (fenced, prefixed):
        out = render_digest(text, _items(1))
        assert '市场维持震荡观望。' in out
        assert '重要事件与影响' in out
        assert '[1](https://example.test/1)' in out


def test_render_digest_normalizes_harmless_schema_variations():
    payload = {
        'overview': '宏观预期偏谨慎。',
        'extra_root': 'ignored',
        'highlights': [
            {
                'fact': '美联储官员发表偏鹰派讲话。',
                'impact': '降息预期有所降温。',
                'references': [1, '1', ' 2 '],
                'category': 'macro_policy',
            }
        ],
        'upcoming': [
            {
                'event': '美国 PCE 数据发布',
                'time': '   ',
                'references': '2',
                'extra_item': True,
            }
        ],
    }
    out = render_digest(json.dumps(payload, ensure_ascii=False), _items(2))
    assert '[1](https://example.test/1) [2](https://example.test/2)' in out
    assert '美国 PCE 数据发布：时间未明确 [2](https://example.test/2)' in out

    no_upcoming = {
        'overview': '仅保留核心消息。',
        'highlights': [{'fact': '交易所上新合约。', 'impact': '关注流动性变化。', 'references': 1}],
        'upcoming': None,
    }
    out_missing = render_digest(json.dumps(no_upcoming, ensure_ascii=False), _items(1))
    assert '关键后续时间' not in out_missing


def test_render_digest_clamps_sections_only_when_requested():
    payload = {
        'overview': '多项事件集中发布。',
        'highlights': [
            {'fact': f'事件 {i}', 'impact': f'影响 {i}', 'references': [1]}
            for i in range(1, 7)
        ],
        'upcoming': [
            {'event': f'预告 {i}', 'time': '2026-11-25 13:30 UTC', 'references': [1]}
            for i in range(1, 5)
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False)
    with pytest.raises(ValueError, match='Invalid news digest section'):
        render_digest(raw, _items(1))

    clamped = render_digest(raw, _items(1), clamp_sections=True)
    assert '事件 5' in clamped and '事件 6' not in clamped
    assert '预告 3' in clamped and '预告 4' not in clamped


@pytest.mark.parametrize('mutator', [
    lambda d: d['highlights'][0].update(references=[99]),
    lambda d: d['highlights'][0].update(references=[True]),
    lambda d: d['highlights'][0].update(references=[]),
    lambda d: d['highlights'][0].update(references=['abc']),
    lambda d: d['highlights'][0].update(fact='参见 https://invented.example'),
    lambda d: d['highlights'][0].update(fact='禁止内嵌编号[1]'),
    lambda d: d['highlights'][0].update(impact='<script>alert(1)</script>'),
    lambda d: d.update(highlights=[]),
])
def test_render_digest_still_rejects_security_and_invalid_references(mutator):
    payload = {
        'overview': '市场简报总览。',
        'highlights': [{'fact': '核心事件', 'impact': '潜在影响', 'references': [1]}],
        'upcoming': [],
    }
    mutator(payload)
    with pytest.raises(ValueError):
        render_digest(json.dumps(payload, ensure_ascii=False), _items(1))


def test_build_digest_repair_prompt_targets_failure_reason():
    msg = build_digest_repair_prompt(ValueError('Invalid news digest reference'), 5)
    assert '1 到 5' in msg and '引用编号无效' in msg
    assert '正文字段包含禁止内容' in build_digest_repair_prompt(ValueError('Invalid news digest text'), 3)
    assert '段落结构不合规' in build_digest_repair_prompt(ValueError('Invalid news digest section'), 3)
    assert '缺少必要字段' in build_digest_repair_prompt(ValueError('Invalid news digest item'), 3)


def test_summarize_self_repairs_once_before_succeeding(monkeypatch):
    monkeypatch.setattr(agent_runs, 'start_agent_run', lambda *a, **k: 'call-run-1')
    monkeypatch.setattr(agent_runs, 'finish_agent_run', lambda *a, **k: None)

    recorded_prompts = []
    valid_json = json.dumps({
        'overview': '市场保持观望。',
        'highlights': [{'fact': 'ETH 现货买盘增强。', 'impact': '短线支撑可能抬升。', 'references': [1]}],
        'upcoming': [],
    }, ensure_ascii=False)
    invalid_first = json.dumps({
        'overview': '市场保持观望[1]。',
        'highlights': [{'fact': 'ETH 现货买盘增强。', 'impact': '短线支撑可能抬升。', 'references': [1]}],
        'upcoming': [],
    }, ensure_ascii=False)

    class RepairingModel:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            recorded_prompts.append(messages)
            if self.calls == 1:
                return AIMessage(content=invalid_first)
            return AIMessage(content=f'```json\n{valid_json}\n```')

    model = RepairingModel()
    monkeypatch.setattr(llm_utils, 'build_chat_model', lambda **kw: model)

    digest = news_service._summarize(
        [{'title': 'ETH buy flow', 'url': 'https://example.test/eth', 'published_at': '2026-11-24T00:00:00Z'}],
        {'provider_id': 'p1', 'model': 'm1', 'api_key': 'secret-key-123456', '_summary_instructions': ''},
    )
    assert model.calls == 2
    assert len(recorded_prompts[1]) == len(recorded_prompts[0]) + 2
    assert '正文字段包含禁止内容' in recorded_prompts[1][-1].content
    assert '市场保持观望。' in digest
    assert '[1](https://example.test/eth)' in digest


def test_process_refresh_falls_back_when_primary_and_repair_both_fail(monkeypatch):
    monkeypatch.setattr(agent_runs, 'start_agent_run', lambda *a, **k: 'call-run-fallback')
    monkeypatch.setattr(agent_runs, 'finish_agent_run', lambda *a, **k: None)

    invoked_models = []
    valid_json = json.dumps({
        'overview': '备选模型成功生成简报。',
        'highlights': [{'fact': 'BTC 现货ETF净流入。', 'impact': '可能支撑现货需求。', 'references': [1]}],
        'upcoming': [],
    }, ensure_ascii=False)

    class RoutingModel:
        def __init__(self, model_name):
            self.model_name = model_name

        def invoke(self, messages):
            invoked_models.append(self.model_name)
            if self.model_name == 'primary-model':
                return AIMessage(content='not json at all')
            return AIMessage(content=valid_json)

    monkeypatch.setattr(llm_utils, 'build_chat_model', lambda **kw: RoutingModel(kw['model']))

    published = []
    failed_errors = []

    class DummyProgress:
        value = {'stage': 'init'}

        def update(self, **kwargs):
            if 'stage' in kwargs:
                self.value['stage'] = kwargs['stage']

        def model_call(self, *a, **k):
            pass

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
            'fallback_summarizer_provider_ids': ['backup'],
        },
        'polymarket': {'enabled': False},
        'llm_providers': [
            {'provider_id': 'primary', 'model': 'primary-model', 'api_key': 'key-111111', 'api_protocol': 'chat'},
            {'provider_id': 'backup', 'model': 'backup-model', 'api_key': 'key-222222', 'api_protocol': 'chat'},
        ],
    }

    monkeypatch.setattr(
        news_service,
        'collect_candidates',
        lambda *a: ([{'title': 'BTC ETF', 'url': 'https://example.test/btc', 'category': 'crypto'}], {'coindesk': {'status': 'ok'}}, {}),
    )
    monkeypatch.setattr(news_service.database_news, 'update_state', lambda *a, **k: None)
    monkeypatch.setattr(news_service.database_news, 'publish_events', lambda *a: True)
    monkeypatch.setattr(news_service.database_news, 'latest_snapshot', lambda: None)
    monkeypatch.setattr(news_service.database_news, 'now_iso', lambda: '2026-11-24T00:00:00Z')
    monkeypatch.setattr(news_service.database_news, 'publish_snapshot', lambda payload, owner: published.append(payload) or True)
    monkeypatch.setattr(db_polymarket, 'fail_review', lambda *a, **k: None)

    news_service._process_refresh('owner-repair-fallback', snapshot, DummyProgress())
    assert invoked_models == ['primary-model', 'primary-model', 'backup-model']
    assert not failed_errors
    assert len(published) == 1
    assert published[0]['summary_model'] == 'backup-model'
    assert '备选模型成功生成简报。' in published[0]['digest']
