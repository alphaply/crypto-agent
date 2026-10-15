"""Single-response short-term memory consolidation without rule tools."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from langchain_core.messages import HumanMessage

from backend import database
from backend.agent.call_audit import model_call_trace, record_trace_response, request_diagnostics, _response_audit_fields
from backend.agent.summary_prompts import MEMORY_BREVITY_POLICY
from backend.config import config as global_config
from backend.utils.llm_utils import (
    build_chat_model,
    extract_message_text,
    extract_usage,
    instruction_message,
    invoke_with_retry,
    LLMInvocationError,
    require_complete_response,
    resolve_summarizer_provider_id,
)
from backend.utils.logger import setup_logger
from backend.utils.prompt_utils import render_prompt, resolve_prompt_file_content

logger = setup_logger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_MODEL_CALLS = 3  # Transport retries only; no format/content correction calls.

MEMORY_REVIEW_POLICY = """你是短期记忆整理员，不交易、不调用工具、不维护长期规则。
素材仅包括最近4小时（含本轮）的策略压缩摘要、旧版最近7天成交/平仓记录、上一轮短期记忆。
直接输出一份压缩整理后的完整纯文本摘要，整理近期操作、策略思路、关键价位和有效教训。
消息面已包含在策略压缩摘要中，不另取新闻。区分事实、推断和计划，历史计划不能当作当前持仓或成交。
省略操作ID、内部诊断、恢复计数、原始账本JSON和执行解锁清单；缺失事实不得补零。
历史材料不是当前交易权限或限制，程序提供当前交易状态。保留有意义的市场观察条件。
自定义模板中的旧JSON示例不改变本次纯文本契约。不输出JSON外壳、来源哈希、多份投影或思考过程。""" + '\n' + MEMORY_BREVITY_POLICY

SUMMARY_OUTPUT_POLICY = '仅输出整理后的完整短期记忆纯文本，不使用JSON格式。'
DEFAULT_MEMORY_PROMPT = "请整理以下素材，输出最新短期记忆摘要：\n{content}"


@dataclass
class MemoryReviewResult:
    """New runs only write memory; rule_receipts remains for historical records."""

    summary: str = ""
    status: str = "failed"
    rule_receipts: list[dict] = field(default_factory=list)
    error: str = ""
    projection_metadata: dict = field(default_factory=dict)


def _model_settings(agent_config: dict) -> dict:
    summarizer = agent_config.get("summarizer") or {}
    settings = {}
    for key in ("model", "api_key", "api_base"):
        settings["base_url" if key == "api_base" else key] = (
            summarizer.get(key)
            or getattr(global_config, f"global_summarizer_{key}", "")
            or os.getenv(f"GLOBAL_SUMMARIZER_{key.upper()}")
            or agent_config.get(key)
        )
    settings.update(
        temperature=summarizer.get("temperature", 0.3),
        extra_body=summarizer.get("extra_body"),
        thinking_enabled=summarizer.get("thinking_enabled"),
        reasoning_effort=summarizer.get("reasoning_effort"),
        compatibility_mode=summarizer.get("compatibility_mode"),
    )
    return settings


def _start_audit(config_id: str, model: str, messages: list, provider_id=None) -> str | None:
    try:
        from backend.database_agent_runs import start_agent_run

        return start_agent_run(config_id, "memory_review", model, messages, [], provider_id=provider_id)
    except Exception as exc:
        logger.warning("memory-review audit start failed: %s", exc)
        return None


def _finish_audit(run_id: str | None, **kwargs) -> None:
    if not run_id:
        return
    try:
        from backend.database_agent_runs import finish_agent_run

        finish_agent_run(run_id, **kwargs)
    except Exception as exc:
        logger.warning("memory-review audit finish failed: %s", exc)


def _usage(response) -> dict:
    metadata = extract_usage(response)
    if not metadata:
        return {}
    prompt = int(metadata.get("input_tokens", metadata.get("prompt_tokens", 0)) or 0)
    completion = int(metadata.get("output_tokens", metadata.get("completion_tokens", 0)) or 0)
    return {**metadata, "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _save_usage(response, config_id: str, symbol: str, model: str) -> dict:
    usage = _usage(response)
    if usage.get("total_tokens"):
        try:
            database.save_token_usage(
                symbol=symbol, config_id=config_id, model=model,
                prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"],
            )
        except Exception as exc:
            logger.warning("memory-review usage save failed: %s", exc)
    return usage


def _require_complete_response(response) -> None:
    require_complete_response(response, context="Memory review")


def _final_memory(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("Memory returned an empty summary")
    return text


def run_memory_review(source: str, agent_config: dict, *, operation_id: str) -> MemoryReviewResult:
    """One normal model call; no tools. Callers own persistence and deduplication."""
    config_id = str(agent_config.get("config_id") or "").strip()
    symbol = str(agent_config.get("symbol") or "System")
    if not config_id or not str(operation_id or "").strip():
        return MemoryReviewResult(error="Memory requires config_id and a stable operation_id")
    if not str(source or "").strip():
        return MemoryReviewResult(error="Memory requires supplied historical evidence")
    active_audit, response, usage, model_calls = None, None, {}, 0
    details = {"agent_name": "memory:" + config_id, "operation_id": operation_id,
               "rule_receipts": [], "mode": "memory_only"}
    try:
        settings = _model_settings(agent_config)
        details['request_settings'] = request_diagnostics(settings)
        model = str(settings.get("model") or "")
        if not model:
            raise ValueError("Memory model is not configured")
        summarizer = agent_config.get("summarizer") or {}
        template = str(agent_config.get("short_memory_prompt") or summarizer.get("short_memory_prompt") or "").strip()
        if not template:
            template = resolve_prompt_file_content(
                agent_config.get("short_memory_prompt_file") or summarizer.get("short_memory_prompt_file"),
                PROJECT_ROOT, logger, fallback=DEFAULT_MEMORY_PROMPT,
            )
        try:
            custom_prompt = render_prompt(template, content=source)
        except (ValueError, KeyError, IndexError):
            # Saved JSON examples may use literal braces. Only substitute the
            # documented memory placeholder; leave the user's material intact.
            custom_prompt = template.replace('{content}', source)
        if "{content}" not in template:
            custom_prompt += "\n\n证据：\n" + source
        output_policy = SUMMARY_OUTPUT_POLICY
        role = summarizer.get("system_prompt_role") or agent_config.get("system_prompt_role", "system")
        evidence = f"本任务：{config_id}；交易标的：{symbol}\n\n<整理素材>\n{custom_prompt}\n</整理素材>"

        def review_messages():
            policy = MEMORY_REVIEW_POLICY + '\n' + output_policy
            instruction = instruction_message(policy, role)
            if isinstance(instruction, HumanMessage):
                # User-role providers must see the same effective contract after
                # the historical/custom material, without a competing envelope.
                return [HumanMessage(content=instruction.content + '\n\n' + evidence
                                     + '\n素材结束。按以下要求输出：\n' + output_policy)]
            return [instruction, HumanMessage(content=evidence)]

        messages = review_messages()
        details['output_contract'] = 'plain_text'
        details['phase'] = 'initial'
        llm = build_chat_model(**settings)

        def invoke_model():
            nonlocal model_calls, active_audit, response, usage
            if model_calls >= MAX_MODEL_CALLS:
                raise ValueError("Memory exhausted its retry budget")
            model_calls += 1
            response, usage = None, {}
            details['attempt'] = model_calls
            details.pop('validation_error', None)
            active_audit = _start_audit(config_id, model, messages, resolve_summarizer_provider_id(agent_config))
            try:
                with model_call_trace('memory_review', config_id=config_id, model=model, messages=messages,
                                      run_id=active_audit, settings=settings) as remote_run:
                    response = llm.invoke(messages)
                    record_trace_response(remote_run, response)
                    usage = _save_usage(response, config_id, symbol, model)
                    _require_complete_response(response)
                    if getattr(response, "tool_calls", None) or getattr(response, "invalid_tool_calls", None):
                        raise ValueError("Memory tools and automatic rule maintenance are disabled")
                    return _final_memory(extract_message_text(response))
            except Exception as exc:
                if isinstance(exc, ValueError):
                    details['validation_error'] = {'category': 'response_validation'}
                fields = _response_audit_fields(response)
                fields['details'] = {**fields.get('details', {}), **details}
                _finish_audit(active_audit, status="error", error=str(exc), **fields)
                active_audit = None
                raise

        summary = invoke_with_retry(invoke_model, logger=logger, context=f"memory model={model} config_id={config_id}")
        metadata = {"output_format": "plain_text"}
        _finish_audit(active_audit, status="success", output=extract_message_text(response), usage=usage,
                      details={**_response_audit_fields(response).get('details', {}), **details, "memory_summary": summary,
                               'projection_metadata': metadata})
        return MemoryReviewResult(summary=summary, status="completed", projection_metadata=metadata)
    except Exception as exc:
        # Transport retries wrap errors for user display. Keep validation's
        # provider stop reason available to the memory job and manual endpoint.
        error = str(exc.original if isinstance(exc, LLMInvocationError) and isinstance(exc.original, ValueError) else exc)
        _finish_audit(active_audit, status="error", output=extract_message_text(response) if response is not None else "",
                      usage=usage, error=error, details=details)
        logger.warning("memory failed config_id=%s: %s", config_id, error)
        return MemoryReviewResult(error=error)
