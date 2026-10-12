import json

import anthropic
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.utils.llm_utils import build_chat_model, extract_reasoning_content, extract_reasoning_token_count
from backend.utils.reasoning_diagnostics import capture_request, response_diagnostics, extract_diagnostics


def chat_response(tokens=0):
    return {'id': 'synthetic-chat', 'object': 'chat.completion', 'created': 1, 'model': 'claude-sonnet-5.5',
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '42', 'reasoning_content': ''}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30,
                      'completion_tokens_details': {'reasoning_tokens': tokens}}}


def native_response(tokens=145, tools=False):
    blocks = [{'type': 'thinking', 'thinking': '', 'signature': 'synthetic-offline-signature'}]
    blocks += [{'type': 'tool_use', 'id': 'synthetic-tool', 'name': 'read', 'input': {'id': 'original'}}] if tools else [{'type': 'text', 'text': '42'}]
    return {'id': 'synthetic-native', 'type': 'message', 'role': 'assistant', 'model': 'claude-sonnet-5.5',
            'content': blocks, 'stop_reason': 'tool_use' if tools else 'end_turn', 'stop_sequence': None,
            'usage': {'input_tokens': 10, 'output_tokens': 200, 'output_tokens_details': {'thinking_tokens': tokens}}}


@pytest.mark.parametrize('mode', ['auto', 'openai', 'anthropic'])
def test_final_wire_request_and_sanitized_diagnostics(mode):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=native_response() if mode == 'anthropic' else chat_response())
    model = build_chat_model(model='claude-sonnet-5.5', api_key='offline-secret-never-log',
                             base_url='https://api.bankofai.io/v1', reasoning_effort='high', compatibility_mode=mode,
                             extra_body={'output_config': {'effort': 'low'}} if mode == 'anthropic' else {'reasoning_effort': 'low'})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    if mode == 'anthropic':
        model.__dict__['_client'] = anthropic.Anthropic(api_key='offline-secret-never-log', http_client=client, max_retries=0)
    else:
        import openai
        model.client = openai.OpenAI(api_key='offline-secret-never-log', http_client=client, max_retries=0).chat.completions
    message = model.invoke('private synthetic prompt')
    diagnostic = extract_diagnostics(message)
    assert diagnostic['requested_effort'] == 'high'
    assert diagnostic['protocol'] == ('anthropic' if mode == 'anthropic' else 'openai')
    params = diagnostic['sent_parameters']
    assert (params['output_config']['effort'] if mode == 'anthropic' else params['reasoning_effort']) == 'low'
    assert (captured[0]['output_config']['effort'] if mode == 'anthropic' else captured[0]['reasoning_effort']) == 'low'
    assert diagnostic['request_outcome'] == 'accepted' and diagnostic['capability'] == 'unverified'
    encoded = json.dumps(diagnostic)
    assert all(value not in encoded for value in ['offline-secret-never-log', 'private synthetic prompt', 'Authorization', 'synthetic-offline-signature'])
    assert len(captured) == 1


@pytest.mark.parametrize('tokens', [145, 148, 139])
def test_synthetic_native_empty_thinking_reports_usage_without_text(tokens):
    from backend.utils.llm_utils import ChatAnthropic
    model = ChatAnthropic(model='claude-sonnet-5.5', api_key='offline')
    model._get_request_payload([HumanMessage(content='offline')], thinking={'type': 'adaptive'}, output_config={'effort': 'high'})
    message = model._format_output(anthropic.types.Message.model_validate(native_response(tokens))).generations[0].message
    assert extract_reasoning_content(message) == ''
    assert extract_reasoning_token_count(message) == tokens
    assert extract_diagnostics(message)['evidence_state'] == 'usage_only'


def test_chat_zero_usage_is_requested_no_evidence_and_disabled_is_explicit():
    from backend.utils.llm_utils import ReasoningChatOpenAI
    model = ReasoningChatOpenAI(model='claude-sonnet-5.5', api_key='offline', reasoning_effort='high')
    model._get_request_payload([HumanMessage(content='offline')])
    message = model._create_chat_result(chat_response()).generations[0].message
    assert extract_diagnostics(message)['evidence_state'] == 'requested_no_evidence'
    diagnostic = capture_request({'model': 'example', 'reasoning_effort': 'none'}, protocol='openai', base_url='https://unknown.example')
    assert response_diagnostics(AIMessage(content='done'), diagnostic)['evidence_state'] == 'disabled'
    with pytest.raises(ValueError, match='关闭推理'):
        build_chat_model(model='claude-sonnet-5.5', api_key='offline', base_url='https://example.invalid', reasoning_effort='none')


def test_native_signature_tool_continuation_is_preserved_and_not_retried():
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=native_response(tools=len(captured) == 1))
    model = build_chat_model(model='claude-sonnet-5.5', api_key='offline', base_url='https://api.bankofai.io/v1',
                             reasoning_effort='high', compatibility_mode='anthropic')
    model.__dict__['_client'] = anthropic.Anthropic(api_key='offline', http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
    first = model.invoke([HumanMessage(content='read')])
    second = model.invoke([HumanMessage(content='read'), first, ToolMessage(content='verified', tool_call_id='synthetic-tool')])
    serialized = json.dumps(captured[1]['messages'])
    assert 'synthetic-offline-signature' in serialized and 'tool_result' in serialized
    assert extract_diagnostics(second)['model'] == 'claude-sonnet-5.5'
    assert len(captured) == 2


@pytest.mark.parametrize('tokens', [143, 186, 143])
def test_native_final_sse_usage_survives_stream_assembly(tokens):
    events = [
        ('message_start', {'message': {'id': 'synthetic-stream', 'type': 'message', 'role': 'assistant',
             'model': 'claude-sonnet-5.5', 'content': [], 'stop_reason': None, 'stop_sequence': None,
             'usage': {'input_tokens': 10, 'output_tokens': 0}}}),
        ('content_block_start', {'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}}),
        ('content_block_delta', {'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'synthetic-stream-signature'}}),
        ('content_block_stop', {'index': 0}),
        ('content_block_start', {'index': 1, 'content_block': {'type': 'text', 'text': ''}}),
        ('content_block_delta', {'index': 1, 'delta': {'type': 'text_delta', 'text': '42'}}),
        ('content_block_stop', {'index': 1}),
        ('message_delta', {'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                           'usage': {'output_tokens': 200, 'output_tokens_details': {'thinking_tokens': tokens}}}),
        ('message_stop', {}),
    ]
    wire = ''.join(f'event: {kind}\ndata: {json.dumps({"type": kind, **value})}\n\n' for kind, value in events)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=wire, headers={'content-type': 'text/event-stream'})
    model = build_chat_model(model='claude-sonnet-5.5', api_key='offline', base_url='https://api.bankofai.io/v1',
                             reasoning_effort='high', compatibility_mode='anthropic', streaming=True)
    model.__dict__['_client'] = anthropic.Anthropic(api_key='offline', http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
    result = model.invoke('offline')
    assert extract_reasoning_token_count(result) == tokens
    assert extract_diagnostics(result)['reported_reasoning_tokens'] == tokens
    assert extract_diagnostics(result)['evidence_state'] == 'usage_only'
    assert 'synthetic-stream-signature' in json.dumps(result.content)
    assert len(calls) == 1


def test_temperature_fallback_records_actual_request_without_retrying_missing_text():
    import openai
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return httpx.Response(400, json={'error': {'message': 'invalid temperature; only 1 is allowed', 'type': 'invalid_request_error'}})
        return httpx.Response(200, json=chat_response())
    model = build_chat_model(model='claude-sonnet-5.5', api_key='offline', base_url='https://api.bankofai.io/v1', reasoning_effort='high', temperature=.3)
    model.client = openai.OpenAI(api_key='offline', http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0).chat.completions
    result = model.invoke('offline')
    assert [r['temperature'] for r in captured] == [.3, 1]
    assert extract_diagnostics(result)['sent_parameters']['temperature'] == 1
    assert extract_diagnostics(result)['evidence_state'] == 'requested_no_evidence'
    assert len(captured) == 2
