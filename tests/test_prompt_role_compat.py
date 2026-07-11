from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from backend.agent import chat_graph
from backend.utils.llm_utils import instruction_message


def test_instruction_message_defaults_to_system_and_supports_user_compatibility():
    assert isinstance(instruction_message("rules"), SystemMessage)
    assert isinstance(instruction_message("rules", "user"), HumanMessage)


def test_user_prompt_role_merges_instructions_with_the_first_user_turn():
    messages = chat_graph._trim_chat_messages(
        "market instructions",
        [HumanMessage(content="analyze BTC"), AIMessage(content="BTC analysis")],
        system_prompt_role="user",
    )

    assert isinstance(messages[0], HumanMessage)
    assert "market instructions" in messages[0].content
    assert "analyze BTC" in messages[0].content
    assert not any(isinstance(message, SystemMessage) for message in messages)
