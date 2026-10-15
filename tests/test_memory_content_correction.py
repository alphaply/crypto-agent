"""Content is prose: old regex/schema correction loops are retired."""
import pytest
from backend.agent import memory_agent as memory
from test_context_projections import CFG, sources, simulation
from test_memory_projection_contract import response, scripted_model


@pytest.mark.parametrize('text', ['最近撤单成功；保留四小时收盘条件。', '本轮持仓减少，下一轮重新评估。', '普通文本，不是JSON'])
def test_prose_is_saved_without_content_correction(sources, monkeypatch, text):
    calls, costs, audits = scripted_model(monkeypatch, response(text))
    result = memory.run_memory_review('历史素材', CFG, operation_id='prose')
    assert result.summary == text and result.status == 'completed'
    assert len(calls) == len(costs) == 1
