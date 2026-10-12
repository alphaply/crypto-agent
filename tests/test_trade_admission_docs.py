"""Documentation examples use the published schema, including default omission."""
import json
import re
from pathlib import Path

from backend.agent.trading_contracts import CanonicalTradeActionsSchema
from backend.utils.trade_receipts import TradeReceipt


def test_documented_batch_and_receipt_examples_match_contracts():
    document = (Path(__file__).parents[1] / 'docs/TRADE_RECEIPTS_AND_RECOVERY.md').read_text(encoding='utf-8')
    batches = 0
    for raw in re.findall(r'```json\s*(.*?)\s*```', document, re.S):
        example = json.loads(raw)
        if 'actions' in example:
            normalized = CanonicalTradeActionsSchema.model_validate(example).model_dump(exclude_none=True)
            assert normalized == example
            batches += 1
        elif 'execution' in example:
            TradeReceipt.model_validate(example)
    assert batches == 3
