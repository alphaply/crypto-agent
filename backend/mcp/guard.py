"""MCP-only exposure gate, evaluated before entry/protection side effects."""
from __future__ import annotations

import math
import json


def perpetual_symbol(exchange, symbol):
    market = exchange.market(symbol)
    if not market.get('contract') and ':' not in symbol:
        market = exchange.market(f'{symbol}:{symbol.split("/")[-1]}')
    if market.get('contract') is False or market.get('linear') is False:
        raise ValueError('MCP perpetual profile requires a linear contract')
    return market['symbol']


def assert_position_owner(market_tool, symbol, *, plan_override=None, adopted_only=False, side=None):
    """Fail closed on shared or untracked perpetual exposure, using read-only evidence."""
    from backend.utils.management_policy import exclusive
    if exclusive(getattr(market_tool, 'runtime_config', None), symbol, side):
        from backend.utils.independent_exits import IndependentExits
        from backend.utils.position_management import ensure_managed
        service = IndependentExits(market_tool)
        if not (plan_override or {}).get('management'):
            ensure_managed(service, symbol, side)
        elif plan_override['management'].get('sync_status') != 'verified':
            raise ValueError('Exclusive position evidence is incomplete')
        relevant = [plan_override] if plan_override else [service._load(service._market(symbol)['symbol'], s)
                                                        for s in ([side] if side else ['LONG', 'SHORT'])]
        if any(p and p.get('ownership_error') for p in relevant):
            raise ValueError('Exclusive position evidence is incomplete')
        return
    config_id = str(getattr(market_tool, 'config_id', '') or '')
    if not config_id.startswith('mcp:') and not adopted_only:
        return
    from backend import database
    from backend.utils.execution_ledger import account_scope
    from backend.utils.exit_orders import is_conditional, order_reference
    from backend.utils.order_ownership import symbol_aliases
    exchange = market_tool.exchange
    if getattr(market_tool, 'market_type', None) == 'spot':
        return
    scoped_side = side if side and exchange.fetch_position_mode(symbol).get('hedged') else None
    aliases = symbol_aliases(market_tool, symbol)
    scope = account_scope(exchange, config_id)
    plans = []
    links = {}
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()")}
        placeholders = ','.join('%s' for _ in aliases)
        if 'real_protection_plans' in tables:
            for row in conn.execute(f'SELECT config_id,payload FROM real_protection_plans WHERE symbol IN ({placeholders})', aliases):
                plan = json.loads(row['payload'])
                if plan.get('account_scope') == scope and plan.get('state') != 'DONE':
                    if row['config_id'] != config_id:
                        raise ValueError('Another strategy owns this account position; MCP cannot modify shared exposure')
                    if plan_override and plan.get('side') == plan_override.get('side'):
                        plan = plan_override
                    plans.append(plan)
        if 'execution_order_links' in tables:
            for row in conn.execute(f'SELECT order_id,config_id,role,payload FROM execution_order_links WHERE account_scope=%s AND symbol IN ({placeholders})', (scope, *aliases)):
                links[str(row['order_id'])] = {'config_id': row['config_id'], 'role': row['role'], **json.loads(row['payload'])}
    for plan in plans:
        independent = plan.get('execution_mode') == 'independent_exits'
        for role, records in (('entry', plan.get('entries', [])), ('exit', [*plan.get('legs', []), *plan.get('exits', []), *([plan['exit_order']] if plan.get('exit_order') else [])])):
            for record in records:
                # A conditional parent cannot own a regular fill with the same
                # numeric ID. Only its observed execution child can own fills.
                parent_id = order_reference(record, current=False) if independent and role == 'exit' and is_conditional(record) else record.get('id')
                for order_id in (parent_id, record.get('execution_order_id')):
                    if order_id:
                        links.setdefault(str(order_id), {'config_id': config_id, 'role': role, 'side': plan['side']})
    canonical = perpetual_symbol(exchange, symbol)
    positions = [row for row in exchange.fetch_positions([canonical]) if abs(float(row.get('contracts') or 0)) > 0]
    if scoped_side:
        plans = [plan for plan in plans if plan.get('side') == scoped_side]
        positions = [row for row in positions if str(row.get('side', '')).upper() == scoped_side]
    adoptions = [plan['adoption'] for plan in plans if plan.get('adoption')]
    if not positions and not adoptions:
        return
    owned_sides = {str(plan.get('side')).upper() for plan in plans}
    if any(str(row.get('side')).upper() not in owned_sides for row in positions):
        raise ValueError('Untracked exchange position: MCP cannot establish sole ownership')
    starts = [float(entry['created_at']) for plan in plans for entry in plan.get('entries', []) if entry.get('created_at')]
    amounts = {}
    baseline_ids = set()
    for baseline in adoptions:
        side = baseline.get('side')
        contracts = float(baseline.get('contracts') or 0)
        since_ms = baseline.get('since_ms')
        ids = baseline.get('observed_trade_ids')
        if (baseline.get('account_scope') != scope or side not in owned_sides
                or not math.isfinite(contracts) or contracts <= 0
                or isinstance(since_ms, bool) or not isinstance(since_ms, (int, float))
                or not math.isfinite(since_ms) or since_ms <= 0
                or not isinstance(ids, list) or any(not isinstance(value, str) or not value for value in ids)):
            raise ValueError('Adopted position baseline requires reconciliation')
        amounts[side] = amounts.get(side, 0) + contracts
        starts.append(since_ms / 1000)
        baseline_ids.update(ids)
    if not starts:
        raise ValueError('Position ownership has no verifiable entry history')
    # Complete fills since the current position cycle distinguish owned quantity
    # from manual or another strategy's additions to the same exchange position.
    trades = exchange.fetch_my_trades(canonical, since=int(min(starts) * 1000), limit=1000)
    if len(trades) >= 1000:
        raise ValueError('Position history exceeds one verifiable page; reconcile ownership before trading')
    seen = set()
    for trade in trades:
        if trade.get('id') is None:
            raise ValueError('Exchange fill has no stable identity')
        if str(trade['id']) in seen:
            continue
        seen.add(str(trade['id']))
        if str(trade['id']) in baseline_ids:
            continue  # Explicitly observed before adoption; never attribute historical manual fills.
        link = links.get(str(trade.get('order')))
        if scoped_side:
            trade_side = str((link or {}).get('side') or (trade.get('info') or {}).get('positionSide') or '').upper()
            if trade_side in {'LONG', 'SHORT'} and trade_side != scoped_side:
                continue
        if not link or link.get('config_id') != config_id:
            raise ValueError('Untracked or foreign fills share this position; MCP modification denied')
        side = str(link.get('side') or '').upper()
        if side not in {'LONG', 'SHORT'}:
            raise ValueError('Fill ownership direction is unresolved')
        amount = float(trade.get('amount') or 0)
        if not math.isfinite(amount) or amount < 0:
            raise ValueError('Invalid exchange fill quantity')
        amounts[side] = amounts.get(side, 0) + (amount if link['role'] == 'entry' else -amount)
    actual = {str(row['side']).upper(): float(row['contracts']) for row in positions}
    if any(not math.isclose(amounts.get(side, 0), actual.get(side, 0), rel_tol=1e-6, abs_tol=1e-10) for side in set(amounts) | set(actual)):
        raise ValueError('Exchange position does not match solely owned fills; MCP modification denied')


def assert_entry_allowed(market_tool, symbol, *, side=None, only_capability=False):
    config_id = str(getattr(market_tool, 'config_id', '') or '')
    if not config_id.startswith('mcp:'):
        return  # Internal agents deliberately retain their existing leverage policy.
    from .settings import get_runtime_profile, settings
    config = get_runtime_profile(config_id)
    if not config or not config.get('enabled') or not settings()['enabled']:
        raise ValueError('MCP trading profile is disabled or missing')
    if config['market_type'] == 'spot':
        return
    maximum = int(config['mcp_max_leverage'])
    if int(config.get('leverage', 1)) > maximum:
        raise ValueError('Configured leverage exceeds MCP maximum')
    exchange = market_tool.exchange
    try:
        canonical = perpetual_symbol(exchange, symbol)
        leverage = exchange.fetch_leverage(canonical)
        values = [leverage.get(key) for key in ('longLeverage', 'shortLeverage', 'leverage')]
        values = [float(value) for value in values if value is not None]
        # Also inspect open positions: OKX isolated positions may differ from the cross setting.
        positions = exchange.fetch_positions([canonical])
        for position in positions:
            if abs(float(position.get('contracts') or 0)):
                if position.get('leverage') is None:
                    raise ValueError('Existing position leverage is unavailable')
                values.append(float(position['leverage']))
        if not values or any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError('Exchange leverage is unavailable')
    except Exception as exc:
        raise ValueError('Cannot verify actual exchange leverage; MCP exposure increase denied') from exc
    if max(values) > maximum:
        raise ValueError(f'Actual exchange leverage {max(values):g}x exceeds MCP limit {maximum}x; close/cancel remains available')
    if not only_capability:
        assert_position_owner(market_tool, symbol, **({'side': side} if side else {}))


def is_exposure_increasing(tool_name, args, config_id, symbol):
    """Classify edits from an owned target, never from caller-supplied role hints."""
    if tool_name in {'open', 'open_position_spot_dca', 'open_position_real', 'update_entry_order_real'}:
        return True
    if tool_name == 'edit':
        from backend.agent.independent_tools import describe_order
        target = describe_order(config_id, symbol, args['order_id'])
        if target.get('kind') not in {'entry', 'exit'}:
            raise ValueError('Cannot verify edit target role')
        return target['kind'] == 'entry'
    if tool_name == 'execute_trade_actions':
        # Inspect every edit before the first action can write, including edits
        # after an entry; those targets must already exist and belong to us.
        increasing = False
        for action in args.get('actions', []):
            if action.get('action') == 'edit':
                increasing = is_exposure_increasing('edit', action, config_id, symbol) or increasing
            elif action.get('action') in {'open', 'amend_entry'}:
                increasing = True
        return increasing
    return False


def preflight_tool(tool_name, args, config_id, symbol):
    if not str(config_id).startswith('mcp:'):
        return
    from .settings import get_runtime_profile
    profile = get_runtime_profile(config_id)
    if not profile:
        raise ValueError('Unknown MCP profile')
    if profile['market_type'] == 'spot':
        return
    if tool_name == 'adopt_position_real':
        # The explicit adoption service verifies current ownership and quantity.
        # Requiring an existing cycle here would make adoption unreachable.
        return
    side = args.get('side')
    if tool_name == 'edit':
        from backend.agent.independent_tools import describe_order
        target = describe_order(config_id, symbol, args['order_id'])
        side = target.get('side') or target.get('pos_side')
    risky = is_exposure_increasing(tool_name, args, config_id, symbol)
    if (tool_name == 'execute_trade_actions' and args.get('contract_version') == 2
            and args.get('execution_policy') == 'independent'):
        # Gateway permissions and target roles are checked for the whole batch;
        # live leverage/position dependencies belong to each executing action.
        return
    if risky:
        from backend.utils.market_data import MarketTool
        assert_entry_allowed(MarketTool(config_id=config_id), symbol, **({'side': side} if side else {}))
    elif tool_name not in {'cancel', 'cancel_orders_real', 'cancel_orders_spot'}:
        if tool_name == 'execute_trade_actions' and all(action.get('action') == 'cancel' for action in args.get('actions', [])):
            return
        from backend.utils.market_data import MarketTool
        assert_position_owner(MarketTool(config_id=config_id), symbol, **({'side': side} if side else {}))
