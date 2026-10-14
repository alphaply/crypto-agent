# Tasks

## 1. 草稿发现接口、凭据与供应商读取

- [x] 1.1 新增 `backend/app/schemas/provider_models.py` 和 `POST /api/config/provider-models/discover` 的薄路由，定义无模型名前置条件的连接请求、显式凭据模式和结构化结果；在 `tests/test_provider_model_discovery.py` 验证未登录拒绝、字段校验、无占位模型请求及验证错误不回显 Key。
- [x] 1.2 在 `backend/app/services/provider_model_service.py` 实现草稿 Key 和已保存同连接凭据的解析；用临时配置与 mock 验证替换 Key、清除 Key、删除渠道、地址/协议变更、复制渠道无 Key，以及所有分支不调用保存/热重载/生成链路。
- [x] 1.3 实现 OpenAI 兼容、DeepSeek 和 Anthropic 的只读列表适配，复用既有协议解析和 BAI 应用请求头语义；用 HTTP 夹具验证根地址、`/v1`、自定义前缀、鉴权头、原始 ID、重复项、不同模型家族及 Anthropic 多页结果。
- [x] 1.4 实现总时间、响应大小、页数/条数限制与统一脱敏错误；用夹具验证空目录、坏 JSON、无效条目、401/403、404/405、429、超时、重定向、重复游标和后续页失败，断言部分结果明确、无自动重试、Key 不出现在响应和日志中。
- [x] 1.5 在 `docs/WORKBENCH.md` 记录发现接口、凭据选择、部分结果和错误状态；运行本组测试，核对文档示例与实际 schema/响应一致。

## 2. Jev 的首次添加与旧接口兼容

- [x] 2.1 将 `backend/utils/jev.py` 的连接校验与评分模型校验分开，接入统一发现的 Jev 适配；扩展 `tests/test_news_processing.py` 与发现测试，验证空模型名的草稿可读取 `models`/`data` 并过滤 Jev，实际评分仍拒绝无效模型。
- [x] 2.2 保留 `/api/news/scorer-models` 已保存渠道查询的鉴权、模型条目结构和脱敏错误，与新发现入口共享相同目录语义；回归现有 Jev 查询测试及跨入口相同候选测试，确认无评分请求或生成用量写入。
- [x] 2.3 更新 `docs/WORKBENCH.md` 的 Jev 获取说明与兼容接口说明；运行本组新闻处理和发现测试，确认现有供应商选择和评分规则保持有效。

## 3. 自动获取、可编辑选择器和抽屉接入

- [x] 3.1 新增共享 `ProviderModelSelect.jsx`，支持模型 ID/名称搜索、准确 ID 回填、手动输入及局部加载/空/部分/错误状态；新增 `frontend/tests/providerModelDiscovery.test.js` 验证规范化、搜索、未列出别名保留和结构化错误映射，确认结果加载不会自动选中第一项。
- [x] 3.2 实现连接编辑完成后的自动请求、去抖、手动刷新、凭据编辑版本和取消/响应版本校验；用可控 Promise/计时器测试验证逐键输入不发送、非连接字段不触发、同版本去重、A→B 迟到成功/失败/finally 全部被忽略、关闭后无状态更新。
- [x] 3.3 接入 `AdminPage.jsx` 的新增/编辑/复制/预设流程并调整连接字段顺序；合并 `ScorerProviderFields.jsx` 的重复列表控件，保留 Jev 供应商说明；通过浏览器夹具验证未保存的普通/Jev 连接自动获取、同一抽屉只有一个模型选择器、预填旧 Key 不随更换端点隐式发出。
- [x] 3.4 新增或扩展 `tests/browser_provider_model_discovery.py` 与现有 provider fixture，使用 mock API 验证桌面/手机、键盘搜索、手动填写、刷新失败和慢响应；统计发现期间配置 PUT 为零，点击保存后仍按既有流程保存，价格/协议/推理字段不被发现结果改写。
- [x] 3.5 更新 README 和 `docs/WORKBENCH.md` 中的添加模型步骤及中英文界面提示，说明候选来源和手动回退；运行前端逻辑测试、模型抽屉浏览器回归及 `npm run build --prefix frontend`，核对截图和文档步骤一致。

## 4. 跨链路验收

- [x] 4.1 运行发现测试及 `tests/test_news_processing.py`、`tests/test_config_store.py`、`tests/test_config_write_atomicity.py`、`tests/test_llm_utils.py`、`tests/test_provider_pricing_usage.py` 的受影响回归，结合前端测试核对 design 验收矩阵；断言只读发现不改变保存契约、定价、协议、推理配置或真实生成调用次数。
- [x] 4.2 运行 `openspec validate add-provider-model-discovery --strict` 和 `git diff --check`，将离线测试、浏览器截图/行为和前端构建结果记入本 change 的 `verification.md`；全部未验证项须明确记录，不使用真实供应商或交易调用代替夹具验收。

## Workflow follow-up

- 用户后续请求归档时，按 `AGENTS.md` 和 `archive-release-workflow` 同步主规范、归档、Git 发版与 Docker 重新构建发布；这些步骤不计入本次 apply 任务完成数。
