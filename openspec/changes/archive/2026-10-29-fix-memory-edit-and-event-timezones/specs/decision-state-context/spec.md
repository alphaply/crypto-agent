## ADDED Requirements

### Requirement: Authenticated memory edits remain consistent across tabs and readers
前台和后台记忆编辑 SHALL 使用同步的登录状态及最新凭证。保存成功后页面及后续 Agent 输入 SHALL 读取相同的已保存正文。失败 SHALL 显示原因并保留输入，登录失效明确提示；未认证请求仍被拒绝，正文修改不改变其他记录或成交快照。

#### Scenario: Login in another tab
- **WHEN** 用户先打开前台，再在另一标签页登录
- **THEN** 前台无需整页刷新即可使用新凭证保存记忆；另一标签页退出时编辑权限同步关闭

#### Scenario: Save fails
- **WHEN** 保存遭遇 401、服务器错误或网络错误
- **THEN** 编辑框显示错误并保留输入，不提示成功；重试成功后更新指定记忆并可由模型读取

#### Scenario: Market refresh is delayed
- **WHEN** 记忆保存成功但工作区行情刷新尚未完成
- **THEN** 页面立即显示已保存的正文，刷新页面和后台管理仍读取一致内容
