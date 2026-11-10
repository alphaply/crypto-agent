"""Check schemas at the HTTP boundary without calling a model or trading."""
import json

import httpx
import openai
import pytest

from backend.agent import chat_graph, tool_registry
from backend.agent.unified_trading import unified_schema_for_config
from backend.utils.llm_utils import build_chat_model


CFG = {'mode': 'REAL', 'exit_mode': 'independent_exits', 'symbol': 'ETH/USDT'}


def assert_explicit_object_types(schema, path='parameters'):
    """Gemini requires OBJECT locally, including inside union branches."""
    if not isinstance(schema, dict):
        return
    if 'properties' in schema or 'required' in schema:
        assert schema.get('type') == 'object', f'{path}: properties/required require object type'
    for keyword in ('properties', '$defs'):
        for name, child in schema.get(keyword, {}).items():
            assert_explicit_object_types(child, f'{path}.{keyword}.{name}')
    for keyword in ('anyOf', 'oneOf', 'allOf'):
        for index, child in enumerate(schema.get(keyword, [])):
            assert_explicit_object_types(child, f'{path}.{keyword}[{index}]')
    for keyword in ('items', 'if', 'then', 'else', 'not', 'additionalProperties'):
        if keyword in schema:
            assert_explicit_object_types(schema[keyword], f'{path}.{keyword}')


@pytest.mark.parametrize('cfg,engine', [
    (CFG, 'live'),
    ({**CFG, 'mode': 'STRATEGY'}, 'live'),
    ({**CFG, 'exit_mode': 'attached_required'}, 'live'),
    ({**CFG, 'mode': 'STRATEGY', 'exit_mode': 'attached_optional'}, 'live'),
    ({'mode': 'SPOT_DCA', 'symbol': 'ETH/USDT'}, 'live'),
    ({'mode': 'PAPER', 'markets': ['spot']}, 'paper'),
    ({'mode': 'PAPER', 'markets': ['spot', 'swap']}, 'paper'),
])
def test_trade_schema_object_branches_are_explicit_for_each_mode(cfg, engine):
    assert_explicit_object_types(unified_schema_for_config(cfg, engine=engine))


@pytest.mark.parametrize('surface', ['decision', 'chat'])
@pytest.mark.parametrize('model_name', ['gemini-4-argon', 'deepseek-chat'])
def test_streamed_http_request_preserves_typed_edit_alternatives(surface, model_name):
    tools = (chat_graph._get_chat_tools(CFG) if surface == 'chat'
             else tool_registry.get_trade_tools_for_mode(CFG['mode'], CFG))
    captured = []

    def receive(request):
        payload = json.loads(request.content)
        captured.append(payload)
        assert payload['model'] == model_name and payload['stream'] is True
        functions = [tool['function'] for tool in payload['tools']]
        # The reported function_declarations[2] is the edit tool. Check exactly
        # those three alternatives before examining the rest of the catalog.
        assert functions[2]['name'] == 'edit_order'
        edit = functions[2]['parameters']
        assert [branch['required'] for branch in edit['anyOf']] == [
            ['remaining_amount'], ['price'], ['trigger_price'],
        ]
        for index, branch in enumerate(edit['anyOf']):
            assert_explicit_object_types(branch, f'edit_order.parameters.anyOf[{index}]')
            field = branch['required'][0]
            assert branch['properties'][field] == {'type': 'number', 'exclusiveMinimum': 0}
        assert 'order_id' in edit['required'] and 'reason' in edit['required']
        for function in functions:
            assert_explicit_object_types(function['parameters'], function['name'])
        chunk = {'id': 'offline', 'object': 'chat.completion.chunk', 'created': 1,
                 'model': model_name, 'choices': [{'index': 0, 'finish_reason': 'stop',
                 'delta': {'role': 'assistant', 'content': 'schema accepted'}}]}
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              content=f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n')

    model = build_chat_model(model=model_name, api_key='offline', base_url='https://gateway.invalid/v1',
                             streaming=True)
    with httpx.Client(transport=httpx.MockTransport(receive)) as transport:
        client = openai.OpenAI(api_key='offline', http_client=transport, max_retries=0)
        model.client = client.chat.completions
        chunks = list(model.bind_tools(tools).stream('offline schema check'))
    assert len(captured) == 1
    assert ''.join(chunk.content for chunk in chunks) == 'schema accepted'
