# Design

## Context

动机见 `proposal.md`。本设计基于以下已检查的实现：

- `AdminPage.jsx` 的 `openAddProvider` 只预填 API Base，`saveProvider` 要求手填模型名；更新草稿后才加入页面配置并由既有自动保存机制持久化。复制渠道清空 Key。
- `LlmProviderPayload` 保存单个 `model`，没有端点独立凭据或批量模型实体。接口有 `chat` / `decisions`，对话兼容模式有 `auto` / `openai` / `deepseek` / `anthropic`。
- `ScorerProviderFields.jsx` 通过 `savedScorerConnection` 要求连接已保存，再调用 `/api/news/scorer-models`。`jev.list_models` 调用了同时要求已有模型 ID 的执行校验，形成首次添加时的前置依赖。
- `/api/config` 由 `get_current_user` 保护；运行快照可在服务端读取已解密凭据。管理配置目前也可能携带 `secrets.api_key.value`，本变更不重构整个管理端的密钥展示，但新发现响应不返回 Key。
- `llm_utils.resolve_compatibility_mode` 与 `_anthropic_base_url` 已定义协议和地址语义，BAI 有既有应用 User-Agent 规则。`pricing_service.py` 的 models.dev 目录用于计价，与目标供应商的模型可见性无关。
- 主规范 `model-reasoning-observability` 将“在线探测”统一要求为手动触发。本 change 明确生成/推理探测仍须手动，配置页可自动做只读目录请求，不将目录成功当作能力验证。

## Goals / Non-Goals

**Goals:**

- 在模型名为空且未保存时完成发现，沿用现有单模型表单与保存契约。
- 同一连接使用一致的协议、凭据和候选语义，并隔离草稿变更与异步响应。
- 通过离线 HTTP 夹具和浏览器夹具验证实际 UI/API 行为，无需真实供应商 Key。

**Non-Goals:**

- 不批量导入所有模型，不新增共享端点/共享凭据数据模型，也不改变复制渠道时清空 Key 的行为。
- 不自动探测模型生成、推理、上下文长度或工具能力，不同步价格，不调用 LLM。
- 不新增 Gemini 原生、Azure 专用部署发现、Bedrock 等当前配置未表达的鉴权协议；提供 OpenAI 兼容端点的网关按已有模式支持。
- 不做后台定时模型同步、共享目录缓存或数据库迁移。

## Decisions

### 1. 增加面向连接草稿的只读发现 API

在 `backend/app/api/config.py` 增加 `POST /api/config/provider-models/discover`，使用现有管理鉴权。请求 schema 与 `LlmProviderPayload` 分开，不要求 `model`、渠道显示名、温度或价格。路由仅处理鉴权与状态映射，服务放在 `backend/app/services/provider_model_service.py`，schema 放在 `backend/app/schemas/provider_models.py`。

请求包含：

- `api_base`、`api_protocol`、`compatibility_mode`，以及 Decisions 渠道的 `decisions_api`。
- `credential` 为显式联合类型：`draft` 携带本次 `api_key`，`saved` 携带 `provider_id`，`none` 表示没有可用凭据。Key 用敏感字段类型承载，未知字段拒绝。
- 不接收任意请求头、任意列表 URL 或前端提供的下一页 URL。

成功返回 `success`、`models: [{id, name, description?}]`、`fetched_at`、不含凭据的 `source`（实际列表地址与协议）、`complete` 和可选的 `warning`。空数组是成功空目录；部分页可返回已有候选并设置 `complete=false`。返回 `Cache-Control: no-store`。

不完整连接为 400，schema 错误为 422，引用已删除渠道为 404；上游凭据失败、无列表接口、限流、连接或格式错误返回 502 和脱敏 `detail.code/message/retryable`，总时间超限为 504。上游 401 不映射成本地会话失效。客户端读取结构化 code 并显示中文/英文提示，而不是直接把供应商正文显示给用户。

**选择理由：** 浏览器直接请求供应商会引入跨域和凭据路径分歧；强制先保存会继续要求用户猜模型名。POST 允许草稿 Key 仅在请求体传递，不进入 URL、列表响应或查询日志。

### 2. 凭据显式选择，发现不改变保存状态

前端根据当前抽屉和 `persistedProviders` 选择凭据模式：新输入 Key 使用 `draft`；编辑同一已保存连接且未改 Key 时使用 `saved`；清除 Key 使用 `none`。判断不能只依赖 `configured`、掩码或页面中尚未持久化的记录。

服务端 `saved` 模式从运行配置按 ID 读取凭据，并核对规范化地址、`api_protocol`、`compatibility_mode` 和有效 `decisions_api`。地址规范化仅处理首尾空白、scheme/host 规范形式和末尾斜杠，保留端口及路径大小写，不沿用 UI 分组把整个 URL 小写的方式。地址或协议已经变化时不隐式复用已存 Key，需要本次显式填写的草稿 Key；仅修改模型名、价格或温度不影响发现。

管理配置加载的 Key 值不等于本次显式输入。组件跟踪 Key 的实际编辑状态，换端点时不把未修改的预填值自动切成 `draft` 发往新地址。新建/复制渠道保持既有无 Key 行为，不按相同 API Base 搜索其他记录的凭据。

所有分支只在内存中组装连接，不调用配置保存、热重载、任务调度或生成/用量记录链路。传入 Key、验证错误、HTTP 异常和上游原始正文不得写入发现日志或返回客户端；Pydantic 错误处理也要避免回显敏感 input。

**选择理由：** 隐式“同地址借 Key”会改变现有渠道隔离和复制语义；先保存草稿 Key 则会在选择模型前修改真实运行配置。

### 3. 按已有传输语义选择目录适配器

使用现有 `httpx`。协议由明确配置决定；`auto` 复用现有解析规则但传入空模型名，避免候选尚不存在时依赖 model 字符串。不会因列表失败自动切换其他协议或替换供应商域名。

| 配置 | 列表地址与认证 | 返回处理 |
| --- | --- | --- |
| OpenAI 兼容 / DeepSeek | 当前 API Base 去末尾斜杠后追加 `/models`；Bearer Key；保留自定义路径前缀 | `data` 数组，读取 `id`，可读名称缺失时使用 ID |
| Anthropic Messages | 按现有 Anthropic 规则去末尾 `/v1` 后追加 `/v1/models`；`x-api-key`、`anthropic-version: 2023-06-01` | `data`、`display_name`，按 `has_more` / `last_id` 请求 `after_id` |
| Jev Decisions | 沿用 `jev.api_base` 的 `/v1` 规则与 Bearer Key | 兼容 `models` / `data`、`name` / `id`，保留已有 `jev-...` ID 校验与过滤 |

OpenAI 兼容模式不会对任意 base 自动补 `/v1`：例如现有 DeepSeek 预设使用根地址，网关也可能使用自定义前缀。调用哪个 base 就从哪个 base 下发现。复用 BAI 已知应用请求头的语义；如果需要提取共享连接帮助函数，不改变生成路径的现有行为。

规范化去除非法条目、按精确非空字符串 ID 去重、稳定排序；ID 不改大小写、不按硬编码模型家族白名单筛选。可显示的描述作为纯文本并限制长度。原始合法空数组是空目录；非空且全部为格式无效条目是格式错误。Jev 合法混合目录中没有匹配项可正常返回空列表。

目录不一定保证每个返回 ID 都适合当前生成用途。除既有 Jev 专用过滤外，本次不猜测可调用性，不通过试调用验证，也不将选择结果用于修改协议、价格或推理参数。

**选择理由：** 使用单一 OpenAI 路径覆盖所有模式会发错原生鉴权和分页；新建供应商品牌枚举会重复已有配置体系；直接复用要求模型名的 Jev 执行校验会保留循环依赖。

### 4. 有界读取及明确的失败与部分结果

HTTP 连接超时 5 秒、整个发现请求总预算 15 秒；采用有大小限制的流式读取，每页最多 2 MiB，最多 10 页或 5000 个唯一模型，达到任一限制立即停止。分页仅对当前适配器明确支持的游标格式进行，禁止跟随远端返回的任意 URL。重复/缺失游标、后续页错误和可识别但不支持的续页标记均返回已有部分结果与提示，不宣称完整。

不自动重试，不跟随 HTTP 重定向，不轮询，也不回退硬编码或价格目录。检查地址为 HTTP(S)、有合法主机，拒绝内嵌凭据、query、fragment 和控制字符；保留管理员显式配置的本地/私有兼容端点，不另建与现有调用不一致的公网白名单。

本次不加跨用户或跨连接缓存。抽屉内保留同一连接的最近结果用于展示和防重复请求；手动刷新失败时可保留该连接的旧候选，但明确显示刷新失败与上次获取时间。连接一旦改变，旧候选立即清除。

**选择理由：** 一次返回有界列表使 UI 搜索简单；共享缓存会增加凭据隔离和失效复杂度。大目录或供应商独有分页以明确部分状态降级，手动模型 ID 始终可用。

### 5. 统一的可编辑模型选择器和连接请求状态

新增 `ProviderModelSelect.jsx` 及小型请求状态 hook/辅助模块，嵌入 `AdminPage.jsx`。用可输入的自动完成控件承载当前 model，候选显示名称和准确 ID，按二者搜索。结果加载不调用 `onChange`；用户选择或输入时才更新模型字段。手工值未出现在目录中也可以保存。

自动读取触发为：打开已有有效连接，或用户完成连接字段编辑（地址/Key 失焦，协议/预设选择完成），在连接完整后去抖 300ms。只把完成的连接编辑提交到发现状态；持续输入时立即使旧请求失效，但不逐键发送。表单现有预设可填模型，但该预设值不冒充远程候选。

状态区分 `idle/incomplete/loading/ready/empty/partial/error`。每个抽屉会话生成递增 generation；连接版本包含规范化地址、协议与凭据编辑版本/保存引用，不将明文 Key 用作日志、storage 或长期缓存键。关闭、切换编辑对象、改变连接即取消计时器和 `AbortController`，并递增版本；响应、错误和 finally 都核对版本，防止旧响应或旧 loading 状态污染当前界面。同一版本最多自动请求一次，手动刷新可再次请求。

读取使用现有 `api`、`silent: true` 和局部 loading，避免触发页面级保存状态或覆盖草稿。API Base 和协议置于模型候选之前，使填写顺序清楚；Key 字段与连接区就近展示，支持桌面、手机、键盘选择和中英文提示。

`ScorerProviderFields` 保留 Jev 供应商选择及用途说明，移除单独的已保存后才能获取列表按钮，交由同一模型选择器呈现候选。旧 `GET /api/news/scorer-models` 保持返回形状；将 `jev` 的连接校验与评分模型校验拆开并复用发现实现，评分入口仍严格验证模型。旧接口与新接口的错误码结构可分别适配，不破坏既有调用方。

**选择理由：** 仅添加手动获取按钮不能实现自动获取；监听所有表单变化会产生重复请求。保留独立 Jev 模型选择器会出现两个控件编辑同一字段和两套状态。

## Risks / Trade-offs

- [供应商支持生成但不支持目录] → 显示不支持并保留手动填写，不把发现失败升级为保存失败。
- [返回列表包含用途不匹配或未获实际调用权限的模型] → 仅表述目录结果，运行调用继续沿用既有校验与错误处理，不自动做收费试调用。
- [草稿 Key 和已保存 Key 混用] → 显式凭据模式、服务端同连接校验和 Key 编辑版本；测试清除、更换地址及复制渠道。
- [上游较慢、目录过大或分页异常] → 总预算、容量上限和部分结果；失败无自动重试。
- [现有 Jev 路径或自动保存回归] → 保留旧路由契约，用已有新闻测试与浏览器请求计数验证发现不产生配置 PUT。

## Migration Plan

1. 添加发现服务/schema/路由及离线协议与凭据测试，保持现有保存和生成逻辑。
2. 接入模型抽屉，统一 Jev 选择控件，保留旧新闻查询接口。
3. 执行 API、协议、Jev、配置与定价回归及前端请求状态/浏览器测试，构建前端；在本 change 记录结果。
4. 更新 README 和 `docs/WORKBENCH.md` 的操作流程。无数据迁移；回滚时恢复旧前后端即可，已保存模型结构不变。
5. 用户后续请求归档时按项目既有 archive 规则同步主规范、Git 发版并重建发布 Docker；本 propose 阶段只产出计划。

## Validation Matrix

| 场景 | 主要验收 |
| --- | --- |
| 新渠道模型空白 | 完成连接编辑后自动 POST；无配置 PUT、无生成调用 |
| 已保存 Key / 新 Key / 清除 Key | 请求使用正确凭据；清除不回退；数据库 Key 不变 |
| 根地址、`/v1`、自定义前缀 | 各适配器路径和鉴权正确，不拼接重复版本路径 |
| Anthropic 分页 / 重复游标 / 第二页失败 | 收集正常后续页；异常有界停止且标记部分结果 |
| OpenAI 网关不同模型家族 | 返回原 ID、搜索与去重正常，无名称驱动的协议切换 |
| 新建 Jev / 旧 Jev API | 无需占位模型；过滤与旧响应形状兼容；评分校验未放宽 |
| A→B、改 Key、关闭重开 | A 的响应、错误和 finally 均不能改变 B 的候选与状态 |
| 空、401/403、404/405、429、超时、坏 JSON | 状态区别明确、错误无密钥、手动填写和保存可用 |
| 已填别名、旧目录刷新失败 | 模型值和其他配置不变，旧结果若保留则标明时间与刷新失败 |
| 桌面/手机/键盘 | 搜索选择、手工值、刷新和提示可使用，抽屉不出现重复模型控件 |

## References

核对日期：2026-10-13。列表协议依据官方接口文档；没有读取本地运行密钥或发起真实供应商请求。

- [OpenAI List models](https://developers.openai.com/api/reference/resources/models/methods/list)：`GET /v1/models`、Bearer 鉴权及 `data[].id`。
- [DeepSeek Lists Models](https://api-docs.deepseek.com/api/list-models/)：现有模型列表接口与 OpenAI 兼容响应。
- [Anthropic List Models](https://platform.claude.com/docs/en/api/models/list)：`GET /v1/models`、版本/Key 头及游标分页。
- Jev 兼容契约来自仓库 `backend/utils/jev.py` 和 `tests/test_news_processing.py` 的已保存查询测试；本次规划没有改变其评分请求协议。
