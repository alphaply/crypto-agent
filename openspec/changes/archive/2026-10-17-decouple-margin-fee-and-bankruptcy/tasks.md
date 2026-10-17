# Tasks

## 1. 合约开仓总成本倒推计算（margin_amount 模式）

- [x] 1.1 在 `backend/mcp/paper_orders.py` 中更新 `quantity` 函数：当 `kind == 'swap'` 且传入 `margin_amount` 时，根据杠杆与手续费率倒推计算实际保证金 `effective_margin = margin_amount / (1 + lev * fee_rate)` 及合约数量 `qty`；在测试用例中验证保证金与手续费合计严格小于等于输入预算。
- [x] 1.2 在 `backend/mcp/paper_orders.py` 的 `prepare_order` 限价单预留计算中，同步支持按总成本倒推预留现金 `reserve`，避免限价单由于额外手续费预留过大而误拒绝；通过单元测试验证限价单预留与市价单扣款一致。

## 2. 持仓与破产解耦及银行违约判定重构

- [x] 2.1 在 `backend/mcp/paper_lifecycle.py` 中更新 `refresh_status`：只要账户持有未平仓有效持仓（`positions` 或 `spot`），绝不判定为破产；仅在无任何持仓且净资产 `<= 0` 且现金 `<= 0` 时触发破产判定；保留每日 00:00 `settle` 扣息不足时的违约破产。
- [x] 2.2 在 `backend/mcp/paper.py` 的 `_mark` 评估函数中，确保未破产的正常持仓在市场波动达到维持保证金率时能够正常触发逐仓强平，解除原先误绑定的冻结限制。
- [x] 2.3 编写测试用例验证“借款开仓扣手续费导致净资产转负”场景：验证账户状态保持 `active`，持仓与止盈止损订单正常运行，平仓后净资产正确结算。

## 3. 负净资产账户只减仓准入与挂单撮合保障

- [x] 3.1 在 `backend/mcp/paper_orders.py` 的 `execute_market` 与 `prepare_order` 中增加风控准入：对于仍有持仓但 `net_equity <= 0` 的账户，允许平仓（`close_long`、`close_short`、`sell`）与保护单设置，但拒绝新开仓（`open_long`、`open_short`、`buy`）及新增借款；编写准入拦截测试。
- [x] 3.2 验证 `backend/mcp/paper_orders.py` 中的 `match` 撮合引擎：确保负净资产持仓账户已挂出的止盈单（如本案中的 95 止盈）能够在行情到达时正常触发并完成平仓结算，编写订单撮合成交测试。

## 4. 历史误判破产账户自动恢复与回归验证

- [x] 4.1 在 `backend/mcp/paper.py` 的账户读取/升级逻辑（`upgrade_account`）中增加自愈检查：若账户此前被标记为 `bankrupt` 但仍持有未平持仓，自动将其状态恢复为 `active` 并持久化。
- [x] 4.2 运行全套相关测试（如 `pytest tests/test_mcp_paper*.py`），确保开仓手续费倒推、持仓与破产解耦、订单撮合与历史账户恢复全部通过。

## Workflow follow-up

- 实施完成后运行测试验证全流程。
- 遵循 OpenSpec 规范校验与归档流程。
