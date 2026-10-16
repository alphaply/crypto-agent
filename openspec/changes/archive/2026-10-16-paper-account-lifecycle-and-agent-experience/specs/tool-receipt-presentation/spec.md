# Spec Delta

## Purpose

为 Agent 运行过程和聊天中的工具调用提供一致、简短且可核实的结果展示，减少重复字段和嵌套回执占用，同时让需要排查问题的用户仍可主动展开查看完整执行事实。

## ADDED Requirements

### Requirement: Default tool results use concise cards
前端 SHALL 以简短卡片展示工具名称、真实状态与简短结果。交易结果 SHALL 保留必要的标的、方向、数量和成交情况；失败或待确认原因 SHALL 在摘要中可见。默认视图 SHALL 不重复展开同一回执，不铺开内部版本、身份及多层字段树。

#### Scenario: Trading receipt contains repeated nested evidence
- **WHEN** 工具结果包含父子回执、账户摘要和重复核验字段
- **THEN** 默认只显示一份简短结果，区分已提交、已成交、部分成交、失败和待确认，不把提交当成交

#### Scenario: Generic tool returns a long text or records
- **WHEN** 查询工具返回长文本或多条记录
- **THEN** 卡片展示简短摘要，长文本与完整记录在展开后读取，不原样占满运行过程

### Requirement: Complete results remain available on demand
工具结果卡片 SHALL 提供按需展开的完整结果及已有授权审计入口，保持错误和未知事实，不改变模型接收的结果、持久化证据或现有查询权限。

#### Scenario: User expands a failed result
- **WHEN** 用户展开失败或待确认的卡片
- **THEN** 可查看完整可授权结果及具体原因，收起后恢复简短卡片，不产生新的金融调用

#### Scenario: Legacy result is plain text
- **WHEN** 结果是旧版纯文本而非 JSON
- **THEN** 前端仍可摘要和展开，不因解析失败丢失内容；中英文与窄屏界面均可阅读
