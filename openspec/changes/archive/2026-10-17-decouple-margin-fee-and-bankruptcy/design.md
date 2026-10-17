# Design: 借款交易、合约手续费与破产机制解耦

## Context

详见 `proposal.md`。
当前系统中，`backend/mcp/paper_orders.py` 在计算永续合约数量时，直接将输入的 `margin_amount` 乘以杠杆除以价格；而在 `backend/mcp/paper.py` 的 `_fill` 中另外计算 `fee = notional * fee_rate` 并从现金中扣除 `margin + fee`。
同时，`backend/mcp/paper_lifecycle.py` 的 `refresh_status` 在每次写入后执行 `if view['net_equity'] <= 0: account['account_status'] = 'bankrupt'`。一旦进入 `bankrupt`，`paper.py` 的 `_mark` 跳过强平，`paper_orders.py` 的 `match` 跳过挂单撮合，`assert_active` 拒绝任何金融操作，导致正常持仓和止盈单直接卡死。

## Goals / Non-Goals

**Goals:**
- 永续开仓输入 `margin_amount` 时，按总成本预算倒推实际保证金与手续费，保证扣款总额严格等于输入金额。
- 逐仓持仓与破产判定解耦：只要账户持有未爆仓的有效持仓，不进入破产状态，挂单（止盈止损）正常撮合，强平按维持保证金率正常监控。
- 破产判定时机精准化：仅在每日扣息透支或所有持仓全部结束且无资金还债时判定破产。
- 负净资产持仓保护模式（只减仓）：允许已有持仓平仓、修改止盈止损及撮合成交，禁止在负净资产期间新开仓和新增借款。
- 历史误判破产账户自动恢复：包含当前卡死的账户在内，只要有未平仓位自动解除破产状态。

**Non-Goals:**
- 不支持全仓杠杆（Cross Margin），保持逐仓（Isolated Margin）架构不变。
- 不修改 Binance 公开行情的接入方式和缓存时间。
- 不取消真实的下海（reset）流程：无持仓且彻底资不抵债后，依然遵循下海重置逻辑。

## Decisions

### 1. 合约开仓采用总成本预算模式倒推保证金与手续费
- **实现方案**：
  在 `paper_orders.quantity` 中，对于 `kind == 'swap'` 且传入 `margin_amount` 的情况：
  令有效保证金 `effective_margin = margin_amount / (1 + lev * fee_rate)`；
  计算数量 `qty = effective_margin * lev / price`，按最小单位向下取整（`ROUND_DOWN`）。
  在 `_fill` 中扣减 `margin = notional / lev` 与 `fee = notional * fee_rate`，二者合计 `<= margin_amount`。
- **替代方案对比**：
  - *替代方案 A（纯外扣）*：用户必须自己算出 2,910 手续费并手动扣减。小白体验极差，极易算错导致现金不足拒绝或开单即破产。
  - *替代方案 B（直接从保证金里扣除手续费而不调整数量）*：持仓名义价值不变但保证金减少，会导致实际杠杆被动放大，强平价格恶化。

### 2. 破产触发时机调整为银行违约与清仓资不抵债
- **实现方案**：
  在 `backend/mcp/paper_lifecycle.py` 的 `refresh_status` 中：
  ```python
  has_positions = bool(account.get('positions')) or bool(account.get('spot'))
  if has_positions:
      return  # 持仓运行中，绝不判定破产
  view = paper.snapshot(account, conn=conn)
  if view['net_equity'] is not None and view['net_equity'] <= 0 and paper.signed_cash(account['cash']) <= 0:
      account['account_status'] = 'bankrupt'
      paper._event(conn, account, 'bankruptcy', {'reason': 'insolvent_after_liquidation'})
  ```
  在 `settle` 每日 00:00 扣息逻辑中保留违约破产：当利息大于可用现金且导致现金为负时，判定破产。
- **替代方案对比**：
  - *替代方案 A（浮亏随时判破产但强平全部仓位）*：只要一有浮亏就直接全部强平，会抹杀所有正常回调和盈利机会（例如本案中 95 止盈即带来巨幅盈利）。

### 3. 负净资产账户启用“只减仓（Reduce-Only）”保护
- **实现方案**：
  在 `backend/mcp/paper_orders.py` 的开仓准入（`prepare_order` 和 `execute_market`）中：
  当 `action in {'buy', 'open_long', 'open_short'}` 且 `paper.snapshot(account)['net_equity'] <= 0` 时，拒绝新增开仓，提示“当前账户净资产不足以覆盖借款，禁止新开仓；请先平仓盈利或偿还借款”。
  对于平仓类动作（`sell`, `close_long`, `close_short`）及止盈止损单设置和触发，完全不受限制。

### 4. 历史受影响账户的自动解冻机制
- **实现方案**：
  在 `backend/mcp/paper.py` 的 `upgrade_account` 或账户加载路径中：
  若检测到 `account['account_status'] == 'bankrupt'` 且 `bool(account.get('positions'))` 为真，自动将 `account['account_status']` 重设为 `'active'`，并在订单表中将该账户此前因破产而未匹配的保护订单恢复正常监控。

## Risks / Trade-offs

- **[Risk]** 倒推数量精度四舍五入导致保证金加手续费微量溢出可用余额。
  → **Mitigation**: 使用 `ROUND_DOWN` 向下截断，保证 `margin + fee <= margin_amount`，在任何边界情况下都不会超支。
- **[Risk]** 负净资产账户在持仓平仓获利后未能及时恢复开仓权限。
  → **Mitigation**: 每次平仓成交后会实时更新 `cash` 和 `equity`，`net_equity` 随之实时转正，下一次开单校验自动通过，无需任何手动介入。
- **[Risk]** 强平逻辑此前包含 `and not frozen` 限制。
  → **Mitigation**: 持仓与破产解耦后，正常持仓账户状态为 `active`，强平监控正常运行；当逐仓浮亏达到维持保证金线时，正常触发逐仓市价强平。
