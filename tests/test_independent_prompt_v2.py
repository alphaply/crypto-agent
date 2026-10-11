from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.agent.decision_context import DecisionMemory, render_decision_prompt
from backend.utils.prompt_utils import resolve_prompt_template


@pytest.mark.parametrize('mode', ['REAL', 'STRATEGY'])
@pytest.mark.parametrize('prompt_file', [None, 'real.txt', 'strategy.txt'])
def test_independent_defaults_allow_cross_run_exit(mode, prompt_file):
    root = Path(__file__).resolve().parents[1]
    prompt_file = str(root / 'backend' / 'agent' / 'prompts' / prompt_file) if prompt_file else None
    template = resolve_prompt_template({'mode': mode, 'exit_mode': 'independent_exits', 'prompt_file': prompt_file}, mode, root, Mock())
    text = render_decision_prompt(template, DecisionMemory('', ''), balance=100,
                                  exit_mode='independent_exits')
    assert '可先入场、成交后另设退出' in text
    assert '已成交可在本轮 close，也可后续轮次' in text
    assert '保护无法落实或约束冲突就等待' not in text
    assert '固定档不会自动按比例缩量或补量' in text


def test_attached_defaults_and_custom_source_are_preserved(tmp_path):
    root = Path(__file__).resolve().parents[1]
    attached = resolve_prompt_template({'exit_mode': 'attached_required'}, 'REAL', root, Mock())
    assert '保护无法落实或约束冲突就等待' in attached
    source = tmp_path / 'real.txt'
    source.write_text('我的交易原则：{symbol}', encoding='utf-8')
    template = resolve_prompt_template({'exit_mode': 'independent_exits', 'prompt_file': str(source)}, 'REAL', root, Mock())
    assert template == '我的交易原则：{symbol}'
    text = render_decision_prompt(template, DecisionMemory('', ''), symbol='ETH/USDT', exit_mode='independent_exits')
    assert '我的交易原则：ETH/USDT' in text and '当前退出模式为 independent_exits' in text
    assert source.read_text(encoding='utf-8') == template


def test_user_edit_of_bundled_filename_is_preserved(tmp_path):
    source = tmp_path / 'backend' / 'agent' / 'prompts' / 'real.txt'
    source.parent.mkdir(parents=True)
    source.write_text('用户通过后台修改的默认文件：{symbol}', encoding='utf-8')
    template = resolve_prompt_template({'exit_mode': 'independent_exits', 'prompt_file': str(source)}, 'REAL', tmp_path, Mock())
    assert template == source.read_text(encoding='utf-8')
    assert '用户通过后台修改' in template
