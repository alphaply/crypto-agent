from pathlib import Path

from backend.utils.trading_policy import TRADE_PROMPT_BODY

REAL_TRADE_PROMPT_TEMPLATE = "你是实盘合约交易Agent，负责调用工具执行并管理仓位。\n" + TRADE_PROMPT_BODY
STRATEGY_PROMPT_TEMPLATE = "你是模拟合约策略Agent，负责调用模拟工具并管理计划。\n" + TRADE_PROMPT_BODY

# Keep the default and selectable bundled spot template identical.
SPOT_DCA_PROMPT_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "agent" / "prompts" / "dca.txt"
).read_text(encoding="utf-8")

PROMPT_MAP = {
    "REAL": REAL_TRADE_PROMPT_TEMPLATE,
    "STRATEGY": STRATEGY_PROMPT_TEMPLATE,
    "SPOT_DCA": SPOT_DCA_PROMPT_TEMPLATE,
}
