# Reasoning and tool-call compatibility

Crypto Agent keeps reasoning output separate from the final answer and supports
tool calls made while a model is in reasoning mode.

## Provider settings

- `compatibility_mode`: `auto`, `openai`, `deepseek`, or `anthropic`. The
  default transport is OpenAI Chat Completions, including for multi-model
  gateways. Use explicit
  `deepseek` for aliases and proxy gateways so every assistant
  `reasoning_content` value is replayed after tool calls. Select `anthropic`
  only when the endpoint exposes the native `/v1/messages` API.
- `thinking_enabled`: `null` follows the model default, `true` forces thinking
  on where the provider exposes a switch, and `false` forces it off.
- `reasoning_effort`: `none`, `low`, `medium`, `high`, `xhigh`, or `max`.
  Unsupported levels are ultimately provider/model dependent. DeepSeek maps
  `medium` and `xhigh` to `high`; `none` is translated to thinking disabled.
- `extra_body`: remains available for gateway-specific extensions.

The Web configuration page provides OpenAI/Codex, DeepSeek, and BAI Claude
presets. Presets never populate or copy API keys.

## Display and persistence

Chat SSE emits reasoning tokens independently from answer tokens. Task runs
persist phase, progress text, tool status, and accumulated reasoning in
`scheduler_runs`. Completed task reasoning is stored in
`summaries.reasoning_content`, allowing the dashboard to show both live and
historical reasoning without mixing it into the answer.

LangChain v1 `content_blocks` are used as the provider-neutral read format and
streamed chunks are combined by addition. This preserves native reasoning
blocks and signatures across tool turns. When an upstream response reports
reasoning-token usage but does not expose reasoning text, the UI shows that
fact and the token count instead of inventing or hiding a reasoning summary.

## Observed OpenAI-compatible gateway behavior

Provider behavior differs even when every model uses `/v1/chat/completions`:

- Some models expose displayable text as `reasoning`.
- DeepSeek and GLM-compatible models may expose it as `reasoning_content`.
- Some Claude and Gemini routes report reasoning-token usage but omit any
  displayable reasoning field. The application can show the usage status, but
  cannot reconstruct text that the upstream API did not return.

## DeepSeek requirement

When `tools` are present, DeepSeek requires the complete assistant message,
including `reasoning_content` and `tool_calls`, to be sent back with tool
results. `DeepSeekChatOpenAI` preserves this field in both non-streaming and
assembled streaming messages and replays it in subsequent requests.

Never place API keys in this document, source files, test fixtures, or exported
logs.
