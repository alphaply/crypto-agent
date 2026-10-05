# 交易 Prompt 设计

本次调整（2026-10-06）把交易执行与记忆复盘分开。交易 Agent 读取当前事实并执行决策；短期记忆 Agent 批量整理历史、复盘和维护长期规则。交易 Prompt 不再无条件追加整段“决策与复盘要求”，每轮结束也不再调用两次额外模型完成策略摘要和短期记忆更新。工具参数、权限和硬性风控仍由工具描述及执行代码定义。

Prompt 长度减少不代表总调用费用或交易亏损会按相同比例下降；实际费用还取决于模型、调用频率、工具返回内容及推理和输出长度。

## 精简范围

- 压缩内置合约和现货模板，保留决策所需的账户、持仓、挂单、市场和记忆上下文。
- 默认最终输出聚焦决策、关键依据、必要参数、工具回执和下次行动条件，减少固定长篇结构。
- 现货运行时去除重复的持仓和操作方法说明；现货仍以该模式实际可用工具为准。
- 退出上下文只标明当前 `exit_mode`，不重复教授工具参数。
- 管理页新建自定义 Prompt 的示例尾部同步缩短，去掉每轮规则复盘指令。
- 默认决策只带最新市场与账户、短期记忆、最近三轮短摘录和有效长期规则，不再读取和拼接七天日报。
- `finalize` 直接保存有界的最终决策摘录与工具执行状态，保留完整输出供查看；这一步不再调用摘要模型。

## 记忆与规则

交易 Agent 与绑定任务聊天只读长期规则，工具注册及执行入口均禁止 `manage_trading_rules`。在旧自定义 Prompt 中要求交易模型修改规则也不会得到该权限。交易工具继续负责订单归属、数量、价格、方向、保护状态及执行结果校验。

短期记忆 Agent 是规则工具的唯一模型调用方，四小时后台任务、手动整理和滚动整理使用同一职责边界。输入包括旧记忆、窗口内新决策及执行状态、实际历史证据和现有规则；输出压缩后的状态、教训和规则复盘。只有可复用且有证据的修正才写入规则，不因单次盈亏制造新规则，也不把短期计划或未经核实的结果变成长期事实。

每次整理最多三次模型调用、一次 `apply` 批量修改，只允许使用 `manage_trading_rules`，不能执行交易。规则写入沿用任务隔离、人工锁定、版本检查、批次原子性和幂等回执；人工锁定规则不能被模型修改或停用。工具成功才表示规则已保存，记忆文字不代替规则变更回执。

模型调用失败、空输出或无效记忆不覆盖旧记忆。规则工具已经成功但最终记忆生成失败时，在 `memory_review_results` 独立保存 `partial` 结果、错误及真实工具回执，旧短期记忆的内容和原记录均保留。调度器报告部分完成，停止同窗口重试，防止为了重新生成记忆而重复写规则；模型调用状态与整轮复盘结果分别核实，不宣称整轮完成。近期决策摘录和实时账户仍独立提供，旧记忆必须按覆盖区间和当前事实重新判断。没有新决策且历史证据没有变化时，直接沿用记忆，不调用复盘模型。

每日复盘保留独立生成、查看和导出功能，不负责交易执行或规则工具调用，也不再默认进入每轮交易输入。

## 查看实际输入与成本

后台 `/console/config` 的「Agent 运行」需要登录，可按记录查看实际发送的消息及角色、工具 schema、输出、状态和已返回的 token 数。这里展示调用时的输入快照，包含模板渲染和运行时追加的内容；只编辑模板不能代表完整调用输入。查看记录不会重新抓行情、重跑模型或执行工具。

记录从功能上线后的调用开始，不补造旧运行的 Prompt；每任务保留最近 100 次调用，最长 30 天。调用开始时保存模型费率快照，结束时依据实际返回的输入、输出 token 数估算费用，服务商返回费用存在时优先采用；页面区分「按配置费率估算」与「服务商返回」。缺失用量或价格时不把未知估算为零，费率后来修改也不回算旧调用。估算不含缓存优惠等差异，不代替供应商账单。减少逐轮摘要调用与重复历史输入可以直接核对，具体节省比例和交易效果需根据后续实际记录评估。

## 模板与升级

内置合约模板和 `backend/agent/prompts/real.txt`、`strategy.txt` 保持一致。任务配置的自定义正文继续保留；升级不得用仓库默认文件覆盖数据卷中的自定义 Prompt。删除的“决策与复盘要求”也不再被运行时追加到自定义正文。

任务编辑器可选择“内置默认（随版本更新）”，以空 `prompt_file` 使用当前交易模式的最新内置模板。已有任务继续使用原选择；若要用这次完整的新正文，需要切换该选项并保存，或自行将新正文合并到原自定义文件。

`{trading_rules_text}`、`{short_memory_text}` 和 `{recent_summaries_text}` 继续提供默认决策上下文，缺失区块按既有兼容机制补充。`{history_text}` 保留兼容，自定义模板显式使用时可加载日报；默认模板不使用它，也不再隐藏追加日报。部署后须重启后端与独立调度器，才能加载新的角色权限和记录功能。

## 历史参考与适用边界

以下是此前设计使用的固定版本及论文链接，仅作历史参考，不代表当前 Prompt 必须执行其中的完整工作流，也不作为收益承诺：

- **TradingAgents**：[Portfolio Manager](https://github.com/TauricResearch/TradingAgents/blob/1394a3f72aa4393e1a98f51b382434c4b4c2d972/tradingagents/agents/managers/portfolio_manager.py)、[Trader](https://github.com/TauricResearch/TradingAgents/blob/1394a3f72aa4393e1a98f51b382434c4b4c2d972/tradingagents/agents/trader/trader.py)、[Reflection](https://github.com/TauricResearch/TradingAgents/blob/1394a3f72aa4393e1a98f51b382434c4b4c2d972/tradingagents/memory/reflection.py)。
- **AI Hedge Fund**：[Risk Limits](https://github.com/virattt/ai-hedge-fund/blob/78b779c1389e2d1452dc29606d2c4126d859b964/hedge_fund/risk/limits.py)、[Portfolio Validation](https://github.com/virattt/ai-hedge-fund/blob/78b779c1389e2d1452dc29606d2c4126d859b964/hedge_fund/portfolio/validation.py)。
- **FinMem**：[Reflection](https://github.com/pipiku915/FinMem-LLM-StockTrading/blob/be814aa47970de9bf2fdd6a1d5a60ae5cf361b46/puppy/reflection.py)、[Prompts](https://github.com/pipiku915/FinMem-LLM-StockTrading/blob/be814aa47970de9bf2fdd6a1d5a60ae5cf361b46/puppy/prompts.py)。
- **CryptoTrade**：[原论文 v2](https://arxiv.org/abs/2407.09546v2)。

提示词检查能够验证上下文是否保留、冗余是否删除、模板是否一致，不能验证收益或胜率提高。费用和交易效果应分别根据实际调用账单及完整成交记录评估。
