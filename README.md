# Crypto Agent v1.0

基于 LLM + LangGraph 的自动化加密交易系统，支持实盘、策略模拟、现货定投三种模式，并提供完整的 Web 控制台与历史统计能力。


## 核心能力

- 多配置隔离：基于 `config_id` 隔离同币种多策略运行。
- 三种交易模式：
    - `REAL`：实盘合约交易。
    - `STRATEGY`：模拟挂单与回溯验证。
    - `SPOT_DCA`：现货定投（按日/按周）。
- 可视化控制台：Dashboard / History / Stats / Admin。
- 成本追踪：记录 Token 使用量并按模型定价估算成本。
- 每日总结：按配置生成日级策略汇总，增强长期上下文。

## v1.0 关键变化

- 移除 screener 双模型分流链路，统一主决策执行链。
- History 页面修复多配置曲线渲染、移动端溢出与空 symbol 参数回退。
- 后台 Prompt 模板编辑体验升级：支持高级编辑器（行号、搜索、快捷保存）。
- 模型定价支持删除，并同步回写 `pricing.json`。
- 导航与历史页面无效入口清理（移除“清理”快捷入口、移除历史手动触发每日总结按钮）。

## 快速开始

使用uv快速准备环境

```bash
uv sync
uv run dashboard.py
```

复制.env.template，并且重命名成.env文件。

唯一在这个文件修改的只有后台的密码。其他配置均可以在web界面中进行配置！

浏览器访问：`http://localhost:7860`

## 文档导航

图文说明文档：https://my.feishu.cn/wiki/FfYkwTigTiFzIZkvgE4cW2oLnvd?from=from_copylink

- 安装与运行：`docs/INSTALL.md`
- 配置指南：`docs/CONFIG_GUIDE.md`
- 成本估算：`docs/COST_ESTIMATION.md`
- 常见问题：`docs/FAQ.md`
- 发布说明：`docs/RELEASE_NOTES_v1.0.md`

## 风险提示

本项目仅供学习与研究。数字资产交易风险极高，请务必小仓位、低杠杆、先模拟后实盘。
