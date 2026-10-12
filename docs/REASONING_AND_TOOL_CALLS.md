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

The Web configuration page provides OpenAI/Codex, DeepSeek, BAI Claude, and BAI Gemini
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

## Cost behavior

Each agent graph node makes exactly one model request per tool-decision turn.
Missing displayable reasoning never triggers a second request. A later agent
request is expected only after a real tool result needs another decision, or
when the configured retry policy retries a failed request. Finalization makes
one strategy-summary request; the scheduler generates rolling short memory on
its separate four-hour cadence.

## DeepSeek requirement

When `tools` are present, DeepSeek requires the complete assistant message,
including `reasoning_content` and `tool_calls`, to be sent back with tool
results. `DeepSeekChatOpenAI` preserves this field in both non-streaming and
assembled streaming messages and replays it in subsequent requests.

Never place API keys in this document, source files, test fixtures, or exported
logs.
# 本轮新增：请求与推理证据诊断

`reasoning_diagnostics` 记录实际请求边界的协议、模型、服务商域名、原配置 effort、最终发送的
effort/thinking/output_config 参数及响应证据。extra_body 的有效覆盖和温度兼容回退以最终
请求为准。HTTP 200 只表示请求被接受，不能证明服务商严格执行了指定档位。

展示状态区分：文本可见、仅返回用量、已请求但未返回证据、显式关闭。零文本且零推理用量
不会再让已配置的 effort 无说明消失。推理诊断不保存 API key、headers、提示词、原始
extra_body 或 thinking signature。原生协议的 signature 仍留在协议消息中供 tool_result
续接，不拿来展示为推理文本。诊断随单轮消息、运行详情及总结持久化，最终 SSE usage
只计一次；回退后显示实际返回响应的模型。

B AI 三组凭据的已观察形状用合成离线样本复现：Chat Completions 空文本/零用量，原生
Messages 空 thinking 但有正数 thinking tokens，以及最终 SSE usage。它们说明“没有
可展示文本”不能推导为“effort 没发送”，也不能推导为“服务商已经严格按 high 执行”。
现有渠道保持原协议。Sonnet 5.5 的 `none` 或显式关闭在本项目中返回不支持提示；网关
未知能力保留 unverified，采样参数支持未经确认时明确提示。

可选受限在线探测（正常测试不运行）：在当前进程设置 `BAI_API_KEY` 后，显式执行
`uv run python -m backend.utils.probe_reasoning --run-online --limit 4`。
最多允许 6 请求，每请求输出上限 2048 tokens，timeout 45 秒，不自动重试、不保存凭据
或完整响应。`--base-url` 默认 `https://api.bankofai.io/v1`；只有在确实需要付费探测时运行。
