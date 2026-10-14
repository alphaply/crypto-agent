# Verification — add-provider-model-discovery

验证日期：2026-10-13。实现与验证均在本地进行；供应商响应、浏览器 API、数据库和凭据均使用离线夹具。没有读取运行密钥，没有调用真实供应商生成 / 评分 / 交易接口。

## 实现结果

- `POST /api/config/provider-models/discover` 使用管理鉴权、独立连接 schema 和 `no-store`，不要求模型名。schema 校验错误也不回显凭据。
- 草稿 Key、同一已保存连接的 Key、显式清除分别处理。后端验证连接身份，前端区分实际编辑的 Key 与管理配置中的预填 Key；复制渠道不借用凭据。
- 统一有界读取 OpenAI 兼容、DeepSeek、Anthropic 分页及 Jev 目录。精确 ID 去重；不跟随重定向、不自动重试；部分目录明确标记。异常正文和可疑凭据回显不进入结果。
- 旧 Jev 查询共用目录实现并保持返回结构；评分继续校验实际 Jev 模型。旧接口不能表达部分目录时返回脱敏错误。
- 抽屉内只有一个可编辑模型选择器。连接编辑完成后去抖 300ms 获取，支持名称 / ID 搜索、手动输入、刷新及局部错误；连接变化与关闭取消旧请求，响应、错误和 finally 都核对版本。
- 无数据库迁移，保持配置保存、价格和生成协议契约。README 与 WORKBENCH 已更新。

## 执行记录

| 验证 | 实际结果 |
| --- | --- |
| `uv run pytest tests/test_provider_model_discovery.py tests/test_news_processing.py tests/test_config_store.py tests/test_config_write_atomicity.py tests/test_llm_utils.py tests/test_provider_pricing_usage.py -q` | **110 passed, 7 subtests passed** |
| `node --test frontend/tests/providerModelDiscovery.test.js` | **9 passed**，受控 Promise / 计时器覆盖去重、取消、迟到成功 / 失败 / finally、关闭后停止更新 |
| `npm run test:chat --prefix frontend` | **24 passed** |
| `npm run test:dashboard --prefix frontend` | **13 passed** |
| `uv run --with playwright python tests/browser_provider_model_discovery.py` | **3 组通过**：1440px 中文、390px 中文、1440px 英文；每组 9 次发现、发现期间 0 次配置 PUT、显式保存后恰好 1 次 PUT |
| `uv run --with playwright python tests/browser_provider.py` | **6 组通过**：1440 / 1366 / 390px × 普通 / 减弱动效；旧模型抽屉选择器、键盘操作及默认 / none 推理选项有效 |
| 在 `frontend/` 执行 `npx eslint src/lib/providerModelDiscovery.js src/components/ProviderModelSelect.jsx src/components/ScorerProviderFields.jsx src/pages/AdminPage.jsx tests/providerModelDiscovery.test.js tests/provider-fixture.jsx` | 通过；仅对本次受影响的前端文件执行 lint |
| `npm run build --prefix frontend` | 通过；Vite 提示已有体积较大 chunk（`api` 约 578 kB），没有构建失败 |
| `openspec validate add-provider-model-discovery --strict` | 通过 |
| `git diff --check` | 通过；只有仓库 CRLF 转换提示，无空白错误 |

Playwright 通过 `uv --with playwright` 临时提供，没有加入运行依赖。浏览器只连接本地 Vite fixture（端口 18971），所有 `/api/**` 都被拦截，未启动交易调度。

## 验收矩阵与证据

| 场景 | 证据 |
| --- | --- |
| 新增普通 / Jev 模型字段为空 | 浏览器先填连接自动 POST，模型值仍为空；Jev 只有一个选择器 |
| 保存 Key / 草稿替换 / 清除 / 删除 / 更换地址 / 复制 | API 与前端测试分别断言凭据模式；独立 SQLite 测试比较发现前后完整数据库 dump 相同 |
| 不保存、不生成、不计费 | 后端测试禁止保存、热重载、构建生成模型、启动调用记录 / 用量及新闻刷新；浏览器核对请求和保存前后字段 |
| 路径与鉴权 | HTTP 夹具断言根地址、`/v1`、自定义大小写路径、本地地址、Bearer / Anthropic 头和 BAI User-Agent |
| 多家族、原始 ID 与重复项 | 保留不同家族与大小写 ID，精确去重；浏览器按显示名称搜索后键盘回填 `Exact/Model` |
| 分页与限制 | Anthropic 正常后续页、重复 / 缺失 / 敏感游标、后续失败 / 超时、页数 / 条数上限、未知分页标记均有断言 |
| 失败可恢复 | 空目录与坏格式分开；401 / 403、404 / 405、429、重定向、坏 JSON、大小超限、总超时均离线验证，失败无重试 |
| 异步隔离 | 受控 Promise 验证 A→B 迟到成功、错误、finally；浏览器修改 Key 取消慢请求并忽略旧结果，关闭 pending 请求后无页面异常 |
| 草稿与旧别名保留 | 浏览器刷新失败仍保留手写别名及带时间的过期结果；保存后价格、协议、温度、extra_body、thinking 和 effort 均与发现前一致 |

## 截图

均为合成数据，图中的 Key 为固定测试字符串；已检查桌面 / 手机候选和 Jev 页面，未出现横向页面溢出。

- [桌面搜索候选](../../../../docs/screenshots/provider-discovery-options-1440.png)
- [手机搜索候选](../../../../docs/screenshots/provider-discovery-options-390.png)
- [桌面中文草稿](../../../../docs/screenshots/provider-discovery-1440-zh.png)
- [手机中文草稿](../../../../docs/screenshots/provider-discovery-390-zh.png)
- [桌面英文草稿](../../../../docs/screenshots/provider-discovery-1440-en.png)
- [手机首次添加 Jev](../../../../docs/screenshots/provider-discovery-jev-390-zh.png)
- [桌面中文 Jev](../../../../docs/screenshots/provider-discovery-jev-1440-zh.png)
- [桌面英文 Jev](../../../../docs/screenshots/provider-discovery-jev-1440-en.png)

## 验证边界与后续

未使用真实供应商验证账号权限、网关定制行为或实时可用模型；目录成功不代表实际生成能力。未执行连接 smoke test，以免触发真实模型请求；未运行全仓库测试 / lint，仅执行上述明确列出的回归。

本次为 apply，尚未同步主规范、归档或发布。后续用户请求 archive 时，按项目主规范执行规范同步、归档、Git 发版与 Docker 重建发布。

## 2026-10-14 归档前收尾复核

独立复核发现并修复一个凭据回显边界：合法 HTTP(S) 连接路径如果包含当前 API Key，原发现成功结果的 `source.api_base` / `source.url` 会原样带出该值。共享目录适配器现在在任何供应商请求前拒绝明文或单次 URL 解码后路径中的当前 Key，返回稳定的 `invalid_connection` / HTTP 400；不在成功结果、错误正文或日志中暴露凭据。

离线反例覆盖草稿和已保存同连接凭据、明文及百分号编码路径，断言供应商请求为零、响应 `no-store`、保存配置不变。另验证旧 Jev 目录调用同样经过此检查；正常地址与准确模型 ID 的既有回归保持通过。

- 再次执行本变更原六文件验收命令：**115 passed, 7 subtests passed in 15.09s**。
- 此前同日只读复核的前端逻辑：`node --test frontend/tests/providerModelDiscovery.test.js`，**9 passed**；本次窄修没有修改前端。
- `openspec validate add-provider-model-discovery --strict` 通过。实际供应商生成、评分和交易接口均未调用。

本记录仅补充归档前验收，不执行规范同步、归档或 Git/Docker 发布；发布阶段由主流程另行记录。
