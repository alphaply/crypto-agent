import os
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import (
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)

_LANGSMITH_LOGGED_STATE: tuple[str, str, bool] | None = None
_BAI_API_HOSTS = {"api.b.ai", "api.bankofai.io"}
_BAI_DEFAULT_HEADERS = {"User-Agent": "crypto-agent/0.1.0"}


def instruction_message(content: str, prompt_role: str | None = "system"):
    """Create a model-compatible instruction message from a provider's role setting."""
    if str(prompt_role or "system").strip().lower() == "user":
        return HumanMessage(content=content)
    return SystemMessage(content=content)


def sync_langsmith_environment() -> Dict[str, str]:
    global _LANGSMITH_LOGGED_STATE
    try:
        from backend.config import config as global_config

        tracing_enabled = bool(getattr(global_config, "langchain_tracing", False))
        project_name = str(getattr(global_config, "langchain_project", "crypto-agent") or "crypto-agent").strip()
        api_key = str(getattr(global_config, "langchain_api_key", "") or "").strip()
    except Exception:
        tracing_enabled = False
        project_name = str(os.getenv("LANGSMITH_PROJECT") or os.getenv("LANGCHAIN_PROJECT") or "crypto-agent").strip()
        api_key = str(os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY") or "").strip()

    tracing_value = "true" if tracing_enabled else "false"
    os.environ["LANGSMITH_TRACING"] = tracing_value
    os.environ["LANGCHAIN_TRACING_V2"] = tracing_value

    if project_name:
        os.environ["LANGSMITH_PROJECT"] = project_name
        os.environ["LANGCHAIN_PROJECT"] = project_name
    else:
        os.environ.pop("LANGSMITH_PROJECT", None)
        os.environ.pop("LANGCHAIN_PROJECT", None)

    if api_key:
        os.environ["LANGSMITH_API_KEY"] = api_key
        os.environ["LANGCHAIN_API_KEY"] = api_key
    else:
        os.environ.pop("LANGSMITH_API_KEY", None)
        os.environ.pop("LANGCHAIN_API_KEY", None)

    state = (tracing_value, project_name, bool(api_key))
    if state != _LANGSMITH_LOGGED_STATE:
        try:
            from backend.utils.logger import setup_logger

            setup_logger("LLM").info(
                f"LangSmith tracing={tracing_value} project={project_name or '-'} api_key_set={bool(api_key)}"
            )
        except Exception:
            pass
        _LANGSMITH_LOGGED_STATE = state

    return {
        "LANGSMITH_TRACING": tracing_value,
        "LANGCHAIN_TRACING_V2": tracing_value,
        "LANGSMITH_PROJECT": os.getenv("LANGSMITH_PROJECT", ""),
        "LANGCHAIN_PROJECT": os.getenv("LANGCHAIN_PROJECT", ""),
        "LANGSMITH_API_KEY": os.getenv("LANGSMITH_API_KEY", ""),
        "LANGCHAIN_API_KEY": os.getenv("LANGCHAIN_API_KEY", ""),
    }


def get_llm_timeout_seconds() -> float:
    try:
        from backend.config import config as global_config

        return float(getattr(global_config, "llm_timeout_seconds", 120))
    except Exception:
        return float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))


def get_llm_max_retries() -> int:
    try:
        from backend.config import config as global_config

        return int(getattr(global_config, "llm_max_retries", 2))
    except Exception:
        return int(os.getenv("LLM_MAX_RETRIES", "2"))


class LLMInvocationError(Exception):
    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        retryable: bool,
        attempts: int,
        original: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.attempts = attempts
        self.original = original


def _join_reasoning_parts(parts: List[str], *, block_boundaries: bool = False) -> str:
    cleaned = [str(part) for part in parts if str(part or "")]
    if not cleaned:
        return ""
    if block_boundaries:
        return "\n\n".join(part.strip() for part in cleaned if part.strip())
    return "".join(cleaned)


def _coerce_reasoning_text(value: Any) -> str:
    """Normalize provider-specific reasoning payloads into displayable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_coerce_reasoning_text(item) for item in value]
        # Providers commonly return one dictionary per reasoning summary block.
        # Preserve those boundaries so a stage title cannot run into its body.
        return _join_reasoning_parts(parts, block_boundaries=any(isinstance(item, dict) for item in value))
    if isinstance(value, dict):
        for key in (
            "reasoning_content",
            "reasoning",
            "thinking",
            "analysis",
            "summary",
            "summary_text",
            "text",
            "content",
        ):
            if key in value:
                text = _coerce_reasoning_text(value.get(key))
                if text:
                    return text
        return ""
    return str(value)


def extract_reasoning_content(message: BaseMessage | Any) -> str:
    """Extract displayable reasoning through LangChain's standard content blocks."""
    try:
        parts = [
            _coerce_reasoning_text(block)
            for block in (getattr(message, "content_blocks", None) or [])
            if isinstance(block, dict) and block.get("type") == "reasoning"
        ]
        reasoning = _join_reasoning_parts(parts, block_boundaries=True)
        if reasoning:
            return reasoning
    except Exception:
        # Provider-native fallbacks below also support older compatible gateways.
        pass

    for container_name in ("additional_kwargs", "response_metadata"):
        container = getattr(message, container_name, None) or {}
        if isinstance(container, dict):
            for key in ("reasoning_content", "reasoning", "thinking", "analysis", "summary"):
                text = _coerce_reasoning_text(container.get(key))
                if text:
                    return text

    direct = _coerce_reasoning_text(getattr(message, "reasoning_content", None))
    if direct:
        return direct

    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("type") or "").lower()
            if block_type in {"reasoning", "reasoning_content", "thinking", "analysis"}:
                text = _coerce_reasoning_text(item)
                if text:
                    parts.append(text)
        return _join_reasoning_parts(parts, block_boundaries=True)
    return ""


def extract_message_text(message: BaseMessage | Any) -> str:
    """Return only user-visible text, excluding reasoning and tool blocks."""
    text = getattr(message, "text", None)
    if isinstance(text, str) and text:
        return text

    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    parts: List[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type") or "").lower()
        if block_type in {"text", "output_text"}:
            parts.append(_coerce_reasoning_text(item.get("text")))
    return "".join(parts)


def extract_reasoning_token_count(message: BaseMessage | Any) -> int:
    """Read provider-normalized reasoning token usage when no summary is exposed."""
    usage = getattr(message, "usage_metadata", None) or {}
    details = usage.get("output_token_details") or usage.get("completion_tokens_details") or {}
    for key in ("reasoning", "reasoning_tokens", "thinking", "thinking_tokens"):
        value = details.get(key)
        if value is not None:
            try:
                return max(int(value), 0)
            except (TypeError, ValueError):
                pass

    metadata = getattr(message, "response_metadata", None) or {}
    token_usage = metadata.get("token_usage") or metadata.get("usage") or {}
    details = token_usage.get("completion_tokens_details") or token_usage.get("output_tokens_details") or {}
    for key in ("reasoning_tokens", "reasoning", "thinking_tokens", "thinking"):
        value = details.get(key)
        if value is not None:
            try:
                return max(int(value), 0)
            except (TypeError, ValueError):
                pass
    return 0


def resolve_compatibility_mode(
    compatibility_mode: Optional[str],
    *,
    model: Optional[str],
    base_url: Optional[str],
) -> str:
    """Resolve the request dialect without relying on a provider's model slug alone."""
    explicit = str(compatibility_mode or "auto").strip().lower()
    if explicit in {"openai", "anthropic", "deepseek"}:
        return explicit

    model_lower = str(model or "").lower()
    try:
        hostname = (urlparse(str(base_url or "")).hostname or "").lower()
    except ValueError:
        hostname = ""
    if "deepseek" in hostname or "deepseek" in model_lower or "-r1" in model_lower or "reasoner" in model_lower:
        return "deepseek"
    # OpenAI Chat Completions is the project's default transport, including
    # routers that expose Claude/Gemini/GLM under an OpenAI-compatible endpoint.
    # Native Anthropic Messages must be selected explicitly.
    if "anthropic" in hostname:
        return "anthropic"
    return "openai"


class ReasoningChatOpenAI(ChatOpenAI):
    """Preserve reasoning fields returned by OpenAI-compatible chat gateways."""

    def _create_chat_result(self, response: Any, generation_info: Optional[Dict[str, Any]] = None) -> Any:
        result = super()._create_chat_result(response, generation_info)
        try:
            resp_dict = response if isinstance(response, dict) else response.model_dump()
            for index, choice in enumerate(resp_dict.get("choices", [])):
                raw_message = choice.get("message") or {}
                reasoning = next(
                    (
                        _coerce_reasoning_text(raw_message.get(key))
                        for key in ("reasoning_content", "reasoning", "thinking", "analysis", "summary")
                        if raw_message.get(key) is not None
                    ),
                    "",
                )
                if reasoning and index < len(result.generations):
                    result.generations[index].message.additional_kwargs["reasoning_content"] = reasoning
        except Exception:
            pass
        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: Dict[str, Any],
        default_chunk_class: type,
        base_generation_info: Optional[Dict[str, Any]],
    ) -> Any:
        """Keep non-standard reasoning deltas dropped by ChatOpenAI's converter."""
        generation = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if generation is None:
            return None
        try:
            choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
            delta = (choices[0].get("delta") or {}) if choices else {}
            reasoning = next(
                (
                    _coerce_reasoning_text(delta.get(key))
                    for key in ("reasoning_content", "reasoning", "thinking", "analysis", "summary")
                    if delta.get(key) is not None
                ),
                "",
            )
            if reasoning:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        except Exception:
            pass
        return generation


class DeepSeekChatOpenAI(ReasoningChatOpenAI):
    """Preserve and replay DeepSeek reasoning_content across tool-call turns."""

    def _get_request_payload(self, input_: Any, *, stop: Optional[List[str]] = None, **kwargs: Any) -> Dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        try:
            original_messages = self._convert_input(input_).to_messages()
            payload_messages: List[Dict[str, Any]] = payload.get("messages", [])
            for index, msg in enumerate(original_messages):
                if index >= len(payload_messages):
                    break
                if isinstance(msg, AIMessage):
                    reasoning = extract_reasoning_content(msg)
                    if reasoning:
                        payload_messages[index]["reasoning_content"] = reasoning
        except Exception:
            pass
        return payload


def _provider_default_headers(base_url: Optional[str]) -> Optional[Dict[str, str]]:
    """Return narrowly scoped compatibility headers for known provider gateways."""
    try:
        hostname = (urlparse(str(base_url or "")).hostname or "").lower()
    except ValueError:
        return None
    if hostname in _BAI_API_HOSTS:
        # B.AI is fronted by Cloudflare. A stable application user agent avoids
        # false-positive bot filtering of the OpenAI SDK's default fingerprint.
        return dict(_BAI_DEFAULT_HEADERS)
    return None


def _anthropic_base_url(base_url: Optional[str]) -> Optional[str]:
    """ChatAnthropic appends /v1/messages itself, so strip a configured /v1."""
    normalized = str(base_url or "").rstrip("/")
    if normalized.lower().endswith("/v1"):
        normalized = normalized[:-3]
    return normalized or None


def _anthropic_uses_adaptive_thinking(model: str) -> bool:
    """Claude 4.6+ uses adaptive thinking; Claude 4.5 and older use budgets."""
    name = str(model or "").lower().replace(".", "-")
    if any(family in name for family in ("sonnet-5", "opus-5", "fable-5", "mythos-5")):
        return True
    return any(version in name for version in ("4-6", "4-7", "4-8"))


def _anthropic_thinking_options(
    model: str,
    thinking_enabled: Optional[bool],
    reasoning_effort: str,
) -> Dict[str, Any]:
    if thinking_enabled is False or reasoning_effort == "none":
        return {"thinking": {"type": "disabled"}}
    if thinking_enabled is not True and not reasoning_effort:
        return {}

    effort = reasoning_effort or "high"
    if _anthropic_uses_adaptive_thinking(model):
        return {
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": effort},
            "max_tokens": 16384,
        }

    budget_by_effort = {
        "low": 2048,
        "medium": 4096,
        "high": 8192,
        "xhigh": 16384,
        "max": 32768,
    }
    budget = budget_by_effort.get(effort, 8192)
    return {
        "thinking": {"type": "enabled", "budget_tokens": budget, "display": "summarized"},
        "max_tokens": budget + 4096,
    }


def build_chat_model(
    *,
    model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    temperature: float = 0.5,
    streaming: bool = False,
    extra_body: Optional[Dict[str, Any]] = None,
    thinking_enabled: Optional[bool] = None,
    reasoning_effort: Optional[str] = None,
    compatibility_mode: Optional[str] = "auto",
) -> BaseChatModel:
    sync_langsmith_environment()

    resolved_mode = resolve_compatibility_mode(
        compatibility_mode,
        model=model,
        base_url=base_url,
    )
    next_extra_body = dict(extra_body or {})
    normalized_effort = str(reasoning_effort or "").strip().lower()
    if normalized_effort not in {"", "none", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError(f"Unsupported reasoning_effort: {reasoning_effort}")
    effective_thinking = thinking_enabled
    model_lower = str(model or "").strip().lower()
    if resolved_mode == "openai" and model_lower.startswith("gemini-3.8"):
        # B.AI's Gemini 3.8 Chat Completions route accepts low/medium/high only.
        # It still performs hidden reasoning and reports its token count, but it
        # does not expose a displayable reasoning summary.
        if normalized_effort in {"xhigh", "max"}:
            normalized_effort = "high"
        elif normalized_effort == "none" or (thinking_enabled is False and not normalized_effort):
            normalized_effort = "low"
    if resolved_mode == "deepseek" and normalized_effort in {"medium", "xhigh"}:
        normalized_effort = "high"
    if resolved_mode == "deepseek" and normalized_effort == "none":
        effective_thinking = False
    if resolved_mode == "deepseek" and effective_thinking is not None:
        next_extra_body["thinking"] = {"type": "enabled" if effective_thinking else "disabled"}

    default_headers = _provider_default_headers(base_url)
    if resolved_mode == "anthropic":
        thinking_options = _anthropic_thinking_options(model, thinking_enabled, normalized_effort)
        anthropic_kwargs: Dict[str, Any] = {
            "model": model,
            "api_key": api_key,
            "base_url": _anthropic_base_url(base_url),
            "streaming": streaming,
            "timeout": get_llm_timeout_seconds(),
            "max_retries": 0,
            "output_version": "v1",
            **thinking_options,
        }
        if not thinking_options or thinking_options.get("thinking", {}).get("type") == "disabled":
            anthropic_kwargs["temperature"] = temperature
        if default_headers:
            anthropic_kwargs["default_headers"] = default_headers
        # Anthropic has no extra_body escape hatch. Forward supported native
        # options as model kwargs so provider settings are not silently lost.
        if next_extra_body:
            anthropic_kwargs["model_kwargs"] = next_extra_body
        return ChatAnthropic(**anthropic_kwargs)

    cls = DeepSeekChatOpenAI if resolved_mode == "deepseek" else ReasoningChatOpenAI
    request_options: Dict[str, Any] = {}
    if next_extra_body:
        request_options["extra_body"] = next_extra_body
    if normalized_effort and not (resolved_mode == "deepseek" and effective_thinking is False):
        request_options["reasoning_effort"] = normalized_effort
    elif thinking_enabled is False and resolved_mode == "openai" and str(compatibility_mode or "auto").lower() == "openai":
        # OpenAI reasoning models use effort=none as the explicit off switch.
        request_options["reasoning_effort"] = "none"

    client_kwargs: Dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "temperature": temperature,
        "streaming": streaming,
        "timeout": get_llm_timeout_seconds(),
        # Retries are handled in invoke_with_retry so SSE status events can reflect retry progress.
        "max_retries": 0,
        **request_options,
    }
    if default_headers:
        client_kwargs["default_headers"] = default_headers

    return cls(
        **client_kwargs,
    )


def classify_llm_error(exc: BaseException) -> str:
    if isinstance(exc, LLMInvocationError):
        return exc.error_type
    if isinstance(exc, (APITimeoutError, TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, AuthenticationError):
        return "auth_error"
    if isinstance(exc, PermissionDeniedError):
        return "permission_error"
    if isinstance(exc, BadRequestError):
        return "bad_request"
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.NetworkError)):
        return "api_error"
    if isinstance(exc, APIError):
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            return "rate_limit"
        if status_code in (400, 404, 413, 422):
            return "bad_request"
        if status_code == 401:
            return "auth_error"
        if status_code == 403:
            return "permission_error"
        return "api_error"
    return "unknown_error"


def is_retryable_llm_error(error_type: str, exc: BaseException) -> bool:
    if isinstance(exc, LLMInvocationError):
        return exc.retryable
    if error_type in {"timeout", "rate_limit"}:
        return True
    if error_type == "api_error":
        status_code = getattr(exc, "status_code", None)
        return status_code is None or int(status_code) >= 500
    return False


def format_llm_error_message(error_type: str) -> str:
    if error_type == "stream_protocol_error":
        return "模型响应流在完成前意外中断，请重试。"
    if error_type == "message_assembly_error":
        return "模型响应已返回，但消息组装失败，请重试。"
    if error_type == "persistence_error":
        return "回答已生成，但暂时无法保存到会话历史。"
    if error_type == "timeout":
        return "模型请求超时，未能在规定时间内返回结果。"
    if error_type == "rate_limit":
        return "模型请求触发了上游限流，请稍后再试。"
    if error_type == "auth_error":
        return "模型鉴权失败，请检查 API Key 或接口地址配置。"
    if error_type == "permission_error":
        return "模型上游拒绝了请求（HTTP 403），请检查账户额度、模型权限或网关/WAF 策略。"
    if error_type == "bad_request":
        return "模型拒绝了本次请求，请检查模型、提示词或请求内容。"
    if error_type == "api_error":
        return "模型上游服务暂时异常，请稍后重试。"
    return "模型请求失败，发生了未预期错误。"


def invoke_with_retry(
    operation: Callable[[], Any],
    *,
    logger,
    context: str,
    on_retry: Optional[Callable[[int, int, str, BaseException], None]] = None,
) -> Any:
    retries = max(get_llm_max_retries(), 0)
    total_attempts = retries + 1
    started_at = time.time()

    for attempt in range(1, total_attempts + 1):
        try:
            result = operation()
            logger.info(
                f"[LLM] {context} succeeded in {time.time() - started_at:.2f}s after {attempt} attempt(s)"
            )
            return result
        except Exception as exc:
            error_type = classify_llm_error(exc)
            retryable = is_retryable_llm_error(error_type, exc)
            is_last_attempt = attempt >= total_attempts
            log_fn = logger.error if is_last_attempt or not retryable else logger.warning
            log_fn(
                f"[LLM] {context} failed on attempt {attempt}/{total_attempts} "
                f"type={error_type} exc={type(exc).__name__}: {exc!r}"
            )

            if not retryable or is_last_attempt:
                raise LLMInvocationError(
                    format_llm_error_message(error_type),
                    error_type=error_type,
                    retryable=retryable,
                    attempts=attempt,
                    original=exc,
                ) from exc

            if on_retry:
                on_retry(attempt + 1, total_attempts, error_type, exc)

            time.sleep(min(2 ** (attempt - 1), 4))
