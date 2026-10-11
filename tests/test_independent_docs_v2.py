"""Validate the copyable MCP v2 examples against the production input models."""
import json
from pathlib import Path
import re

from backend.agent.trading_contracts import CANONICAL_MODELS, CanonicalTradeActionsSchema


def test_documented_canonical_examples_parse():
    text = Path('docs/MCP.md').read_text(encoding='utf-8').split('### 独立退出规范工具 v2', 1)[1].split('## 5.', 1)[0]
    examples = [json.loads(value) for value in re.findall(r'```json\n(.*?)\n```', text, re.S)]
    assert len(examples) >= 5
    for example in examples:
        args = dict(example['arguments'])
        for identity in ('profile_id', 'symbol', 'operation_id'):
            assert args.pop(identity)
        model = CanonicalTradeActionsSchema if example['tool'] == 'execute_trade_actions' else CANONICAL_MODELS[example['tool']]
        model.model_validate(args)
