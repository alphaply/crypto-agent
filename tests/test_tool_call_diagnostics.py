import json

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage

from backend.utils.llm_utils import build_chat_model
from backend.utils.reasoning_diagnostics import capture_request, current_request, response_diagnostics


def test_diagnostics_use_wire_overrides_without_recording_arguments_or_secrets():
    token = current_request.set(None)
    try:
        diagnostic = capture_request({
            'model': 'gemini-4-argon', 'tools': [{'secret': 'do-not-log'}],
            'tool_choice': 'required', 'extra_body': {'tools': [], 'tool_choice': 'none'},
        }, protocol='openai', base_url='https://api.b.ai/v1')
        result = response_diagnostics(AIMessage(content='Waiting'), diagnostic)
        assert result['tools_sent'] == 0 and result['tool_choice'] == 'none'
        assert result['tool_evidence_state'] == 'no_tools_sent'
        assert 'do-not-log' not in json.dumps(result)
    finally:
        current_request.reset(token)


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('use_call', [False, True])
def test_bai_wire_calls_are_distinct_from_json_in_content(streaming, use_call):
    tool = {'type': 'function', 'function': {'name': 'diagnostic_echo',
            'parameters': {'type': 'object', 'properties': {'marker': {'type': 'string'}}, 'required': ['marker']}}}
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        message = {'role': 'assistant', 'content': '' if use_call else '```json\n{"marker":"example"}\n```'}
        if use_call:
            message['tool_calls'] = [{'id': 'probe-1', 'index': 0, 'type': 'function',
                                      'function': {'name': 'diagnostic_echo', 'arguments': '{"marker":"example"}'}}]
        choice = {'index': 0, 'finish_reason': 'tool_calls' if use_call else 'stop',
                  'delta' if streaming else 'message': message}
        body = {'id': 'offline', 'model': 'gemini-4-argon', 'created': 1, 'choices': [choice]}
        if streaming:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=f'data: {json.dumps(body)}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json=body)

    token = current_request.set(None)
    try:
        model = build_chat_model(model='gemini-4-argon', api_key='offline', base_url='https://api.b.ai/v1', streaming=streaming)
        with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
            model.client = openai.OpenAI(api_key='offline', http_client=transport, max_retries=0).chat.completions
            response = model.bind_tools([tool]).invoke('offline test')
        diagnostic = response.additional_kwargs['reasoning_diagnostics']
        assert captured[0]['tools'] and len(captured) == 1
        assert diagnostic['tools_sent'] == 1
        assert diagnostic['tool_calls_returned'] == int(use_call)
        assert diagnostic['tool_evidence_state'] == ('calls_returned' if use_call else 'no_calls')
        assert len(response.tool_calls) == int(use_call)
    finally:
        current_request.reset(token)


def test_invalid_calls_are_not_reported_as_no_action():
    response = AIMessage(content='', invalid_tool_calls=[{'name': 'diagnostic_echo', 'args': '{', 'id': 'broken', 'error': 'invalid JSON'}])
    result = response_diagnostics(response, {'tools_sent': 1})
    assert result['tool_evidence_state'] == 'invalid_calls'
    assert result['invalid_tool_calls'] == 1
