import os
import time
from typing import Any, Callable, Dict, List, Optional

import httpx
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from openai import APIError, APITimeoutError, AuthenticationError, BadRequestError, RateLimitError


def get_llm_timeout_seconds() -> float:
    return float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))


def get_llm_max_retries() -> int:
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


class DeepSeekChatOpenAI(ChatOpenAI):
    """ChatOpenAI subclass that injects DeepSeek reasoning_content into the API payload.

    DeepSeek Thinking mode requires that any assistant message which originally contained
    a `reasoning_content` field (especially tool-call messages) must have that field passed
    back verbatim in subsequent turns. LangChain's _convert_message_to_dict does not
    serialise reasoning_content, so we inject it here after the parent builds the payload.
    """

    def _get_request_payload(self, input_: Any, *, stop: Optional[List[str]] = None, **kwargs: Any) -> Dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        try:
            original_messages = self._convert_input(input_).to_messages()
            payload_messages: List[Dict[str, Any]] = payload.get("messages", [])
            for i, msg in enumerate(original_messages):
                if i >= len(payload_messages):
                    break
                if isinstance(msg, AIMessage):
                    rc = (
                        msg.additional_kwargs.get("reasoning_content")
                        or msg.response_metadata.get("reasoning_content")
                    )
                    if rc:
                        payload_messages[i]["reasoning_content"] = rc
        except Exception:
            pass  # never break inference due to injection failure
        return payload


def build_chat_openai(
    *,
    model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    temperature: float = 0.5,
    streaming: bool = False,
    extra_body: Optional[Dict[str, Any]] = None,
) -> ChatOpenAI:
    model_kwargs: Dict[str, Any] = {}
    if extra_body:
        model_kwargs["extra_body"] = extra_body

    # DeepSeek Thinking models require reasoning_content to be echoed back in multi-turn
    # conversations. Use a custom subclass that injects it into the request payload.
    model_lower = (model or "").lower()
    cls = DeepSeekChatOpenAI if ("deepseek" in model_lower or "-r1" in model_lower) else ChatOpenAI

    return cls(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        streaming=streaming,
        timeout=get_llm_timeout_seconds(),
        # Retries are handled in invoke_with_retry so SSE status events can reflect retry progress.
        max_retries=0,
        model_kwargs=model_kwargs,
    )


def classify_llm_error(exc: BaseException) -> str:
    if isinstance(exc, (APITimeoutError, TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, AuthenticationError):
        return "auth_error"
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
        if status_code in (401, 403):
            return "auth_error"
        return "api_error"
    return "unknown_error"


def is_retryable_llm_error(error_type: str, exc: BaseException) -> bool:
    if error_type in {"timeout", "rate_limit"}:
        return True
    if error_type == "api_error":
        status_code = getattr(exc, "status_code", None)
        return status_code is None or int(status_code) >= 500
    return False


def format_llm_error_message(error_type: str) -> str:
    if error_type == "timeout":
        return "模型请求超时，未能在规定时间内返回结果。"
    if error_type == "rate_limit":
        return "模型请求触发了上游限流，请稍后再试。"
    if error_type == "auth_error":
        return "模型鉴权失败，请检查 API Key 或接口地址配置。"
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
                f"type={error_type}: {exc}"
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
