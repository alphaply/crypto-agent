"""Current decision facts. Loading is local; projection and rendering are pure.

This view describes missing dependencies; it never grants or denies a trade.
The execution layer still refreshes and assesses the chosen action.
"""
from __future__ import annotations

from dataclasses import dataclass
import json

from backend.utils.execution_ledger import number
from backend.utils.exit_orders import ORDER_TYPES, canonical_order_type, exit_group, order_reference, observed_order_status
from backend.utils.trade_operations import _same_symbol
from backend.utils.trade_order_evidence import classify_order, UNRESOLVED, TERMINAL
from backend.utils.trade_receipts import unresolved_orders
from backend.utils.independent_admission import assess_admission, cycle_quiescent
from backend.utils.trade_context_format import render_positions, render_orders


@dataclass(frozen=True)
class DecisionTradeState:
    scope: dict
    positions: list[dict]
    orders: list[dict]
    coverage: dict
    issues: list[dict]
    evidence_state: dict


def load_trade_plans(config_id: str, account_scope: str, symbol: str) -> list[dict]:
    """Read all cycle associations; a late unresolved order has no page cutoff."""
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        rows = conn.execute('SELECT symbol,payload FROM trade_cycle_evidence WHERE config_id=%s AND account_scope=%s',
                            (config_id, account_scope)).fetchall()
        current = conn.execute('SELECT symbol,payload FROM real_protection_plans WHERE config_id=%s',
                               (config_id,)).fetchall()
    plans = {}
    for row in [*rows, *current]:
        if not _same_symbol(row['symbol'], symbol):
            continue
        plan = json.loads(row['payload'])
        if plan.get('account_scope') != account_scope:
            continue
        if plan.get('execution_mode') == 'independent_exits':
            plans[plan['episode_id']] = plan
    return list(plans.values())


def load_pending_operations(config_id: str, account_scope: str, symbol: str) -> dict:
    """Include in-flight requests even when no cycle could yet be persisted."""
    from backend.database import get_db_conn
    from backend.utils.trade_recovery import operation_evidence
    with get_db_conn() as conn:
        rows = conn.execute("SELECT operation_id,symbol FROM trade_action_runs WHERE config_id=%s "
                            "AND status IN ('running','unknown','pending')", (config_id,)).fetchall()
    return operation_evidence(config_id, symbol, [r['operation_id'] for r in rows if _same_symbol(r['symbol'], symbol)],
                              account_scope=account_scope)


def project_trade_state(account: dict, *, config_id: str, account_scope: str,
                        symbol: str, plans: list[dict] | None,
                        contract_size: float | None = None, referenced_evidence: dict | None = None) -> DecisionTradeState:
    mock = account.get('independent_exits')
    is_mock = mock is not None
    source = 'mock_snapshot' if is_mock else 'exchange_snapshot'
    evidence = dict(account.get('evidence_state') or {})
    hedged = account.get('hedged')
    scope = dict(config_id=config_id, account_scope=account_scope, symbol=symbol, hedged=hedged)
    if account.get('management'):
        scope['management'] = account['management']
    cycles = [p for p in plans or [] if _same_symbol(p.get('symbol', symbol), symbol)
              and p.get('account_scope') == account_scope and p.get('execution_mode') == 'independent_exits']
    active = {p.get('side'): p for p in cycles if p.get('state') != 'DONE'}
    issues = {}

    def issue(reason, side=None, *, plan=None, order_id=None, operation_id=None, **extra):
        plan = plan or {}
        key = '|'.join(str(v or '') for v in (account_scope, symbol, plan.get('episode_id'),
                                             side, order_id or operation_id, reason))
        if key not in issues:
            issues[key] = dict(identity=key, reason_code=reason, side=side, order_id=order_id,
                               operation_id=operation_id, episode_id=plan.get('episode_id'),
                               scope='same_side' if hedged is True else 'shared_net_position' if hedged is False else 'position_mode_unverified',
                               observed_at=plan.get('verified_at'), **extra)

    if account.get('error'):
        issue('account_snapshot_unavailable', missing_evidence=str(account['error']))
    if not is_mock:
        for component, field in (('positions', 'real_positions'), ('orders', 'real_open_orders')):
            if field not in account:
                issue(component + '_unavailable', missing_evidence=field)
    if not is_mock and plans is None:
        issue('cycle_evidence_unavailable', missing_evidence='current_task_ownership_and_orders')
    for component, detail in evidence.items():
        if isinstance(detail, dict) and detail.get('error'):
            issue(component + '_unavailable', missing_evidence=detail['error'])

    positions = []
    position_observation = evidence.get('positions') or {}
    position_failed = bool(position_observation.get('error') or
                           (account.get('error') and not position_observation.get('observed_at')) or
                           (not is_mock and 'real_positions' not in account))
    for p in (mock.get('positions', []) if is_mock else account.get('real_positions', [])):
        if p.get('symbol') and not _same_symbol(p['symbol'], symbol):
            continue
        side = str(p.get('pos_side') or p.get('side') or '').upper()
        if side not in {'LONG', 'SHORT'}:
            side = 'LONG' if 'BUY' in side else 'SHORT' if 'SELL' in side else side
        plan = active.get(side, {})
        observation = plan.get('position_observation') or {}
        amount = number(p.get('amount'))
        management = p.get('management') or {}
        owned = is_mock or management.get('state') in {'owned', 'shared'} or (observation.get('status') == 'verified' and observation.get('amount') == amount
                            and not plan.get('ownership_error') and not plan.get('position_error'))
        positions.append(dict(side=side, amount=None if position_failed else amount,
                              last_known_amount=amount if position_failed else None,
                              entry_price=p.get('entry_price', p.get('price')),
                              unrealized_pnl=p.get('unrealized_pnl'), ownership='task' if owned else 'unverified',
                              adoption=plan.get('adoption'), management=management or plan.get('management'),
                              management_state=management.get('state'), visible=management.get('visible', True),
                              eligible=management.get('eligible'), can_claim=management.get('can_claim'), owner=management.get('owner'),
                              source=p.get('source') or (plan.get('management') or {}).get('source', source),
                              managed=owned, supported_actions=p.get('supported_actions', ['close'] if owned else []),
                              observed_at=(evidence.get('positions') or {}).get('observed_at', p.get('observed_at'))))
        if not owned and not management:
            issue('ownership_unverified', side, plan=plan, missing_evidence='current_task_position_ownership')

    local_orders = []
    for plan in cycles:
        side = plan.get('side')
        blockers = unresolved_orders(plan)
        relevant = plan.get('state') != 'DONE' or blockers or any(
            classify_order(r)['state'] == 'live_verified' for r in plan.get('entries', []) + plan.get('exits', []))
        if not relevant and not (plan.get('cleanup_fill_unseen') or plan.get('unobserved_entry_fills')):
            continue
        opposite = active.get('SHORT' if side == 'LONG' else 'LONG')
        assessments = [assess_admission(plan, action, hedged=hedged is not False, opposite=opposite)
                       for action in ('open', 'close')]
        for blocker in blockers:
            effects = sorted({a['intent']['effect'] for a in assessments
                              if any(b.get('order_id') == blocker.get('order_id') for b in a['blockers'])})
            issue(blocker['reason_code'], side, plan=plan, order_id=blocker.get('order_id'),
                  operation_id=blocker.get('operation_id'), affected_effects=effects,
                  missing_evidence='verified_execution_outcome', recovery='execution_layer',
                  last_error=blocker.get('last_error'))
        if plan.get('cleanup_fill_unseen') or plan.get('unobserved_entry_fills') or (
                plan.get('state') == 'EXITING' and not cycle_quiescent(plan) and not blockers):
            issue('cycle_cleanup_unverified', side, plan=plan, missing_evidence='position_and_cleanup_fills')
        if plan.get('residual_verified') and (plan.get('position_observation') or {}).get('amount'):
            issue('residual_position_requires_management', side, plan=plan,
                  amount=plan['position_observation']['amount'], recovery='explicit_position_management')
        for field in ('position_error', 'ownership_error'):
            if plan.get(field):
                issue(field, side, plan=plan, missing_evidence=plan[field])
        for role in ('entries', 'exits'):
            for record in plan.get(role, []):
                classification = classify_order(record)['state']
                if classification in {'not_accepted', 'terminal_verified'}:
                    continue
                # A terminal order with missing fills is an issue, not a live order.
                if record.get('status') in TERMINAL:
                    continue
                size = number(plan.get('contract_size', contract_size))
                amount, filled = number(record.get('amount')), number(record.get('filled'))
                remaining = (max(amount - filled, 0) * size if size and amount is not None and filled is not None
                             and classification == 'live_verified' and record.get('quantity_mode') != 'all' else None)
                local_orders.append(dict(order_id=order_reference(record),
                    parent_order_id=order_reference(record, current=False) if record.get('execution_order_id') else None,
                    client_id=record.get('client_id'), side=side, role='exit' if role == 'exits' else 'entry',
                    order_type=canonical_order_type(record), status=observed_order_status(record),
                    price=record.get('price'), trigger_price=record.get('trigger_price'),
                    quantity_mode=record.get('quantity_mode', 'fixed'), remaining=remaining,
                    resolved_amount=record.get('resolved_amount'), ownership='task',
                    source=record.get('source', 'agent'), managed=True,
                    supported_actions=['cancel', 'edit'] if canonical_order_type(record) in ORDER_TYPES - {'market'} else [],
                    observed_at=record.get('fill_observed_at', plan.get('verified_at')),
                    verified=classification == 'live_verified'))

    if is_mock:
        for role in ('entries', 'exits'):
            for r in mock.get(role, []):
                local_orders.append(dict(order_id=order_reference(r), parent_order_id=None,
                    side=str(r.get('pos_side') or r.get('side') or '').upper(), role='exit' if role == 'exits' else 'entry',
                    order_type=canonical_order_type(r), status=observed_order_status(r), price=r.get('price'),
                    trigger_price=r.get('trigger_price'), quantity_mode=r.get('quantity_mode', 'fixed'),
                    remaining=None if r.get('quantity_mode') == 'all' else number(r.get('remaining', r.get('amount'))),
                    resolved_amount=r.get('resolved_amount'), ownership='task', source=source, managed=True,
                    supported_actions=['cancel', 'edit'] if canonical_order_type(r) in ORDER_TYPES - {'market'} else [],
                    observed_at=r.get('observed_at'), verified=not r.get('pending', False)))
        for b in mock.get('unresolved_orders', []):
            issue(b.get('reason_code', 'order_unresolved'), b.get('side'),
                  order_id=b.get('order_id'), operation_id=b.get('operation_id'))

    orders = {r['order_id'] or ('client:' + str(r.get('client_id'))): r for r in local_orders}
    aliases = {r['parent_order_id']: key for key, r in orders.items() if r.get('parent_order_id')}
    seen = set()
    for raw in account.get('real_open_orders', []) if not is_mock else []:
        if raw.get('symbol') and not _same_symbol(raw['symbol'], symbol):
            continue
        ref = order_reference(raw)
        ref = aliases.get(ref, ref)
        seen.add(ref)
        if ref in orders:
            if raw.get('source'):
                orders[ref]['source'] = raw['source']
            orders[ref]['supported_actions'] = raw.get('supported_actions', orders[ref]['supported_actions'])
            orders[ref]['snapshot_observed_at'] = (evidence.get('orders') or {}).get('observed_at')
            orders[ref]['visible_in_snapshot'] = True
            snapshot_remaining = number(raw.get('remaining'))
            orders[ref]['snapshot_remaining'] = snapshot_remaining
            orders[ref]['snapshot_status'] = raw.get('status')
            orders[ref]['snapshot_price'] = raw.get('price')
            orders[ref]['snapshot_trigger_price'] = raw.get('trigger_price')
            remaining = orders[ref].get('remaining')
            if snapshot_remaining is not None and remaining is not None and abs(snapshot_remaining - remaining) > 1e-12:
                orders[ref]['verified'] = False
                issue('order_quantity_conflict', orders[ref]['side'], order_id=ref,
                      missing_evidence='reconcile_snapshot_remaining_with_local_fill')
        else:
            orders[ref] = dict(order_id=ref, side=raw.get('pos_side'), role='unverified',
                order_type=raw.get('order_type', raw.get('type')), status=raw.get('status'),
                price=raw.get('price'), trigger_price=raw.get('trigger_price'),
                quantity_mode=raw.get('quantity_mode', 'fixed'), remaining=number(raw.get('remaining')),
                ownership='task' if raw.get('managed') else 'unverified', source=raw.get('source') or source,
                management=raw.get('management'), management_state=raw.get('management_state'),
                visible=raw.get('visible', True), eligible=raw.get('eligible'), can_claim=raw.get('can_claim'), owner=raw.get('owner'),
                managed=raw.get('managed', False), supported_actions=raw.get('supported_actions') or [],
                observed_at=raw.get('observed_at') or (evidence.get('orders') or {}).get('observed_at'), verified=False)
    if not is_mock and 'real_open_orders' in account and not (evidence.get('orders') or {}).get('error') and not account.get('error'):
        for ref, order in orders.items():
            if ref not in seen and order.get('verified'):
                order['verified'] = False
                issue('order_snapshot_conflict', order['side'], order_id=ref, missing_evidence='live_order_in_current_snapshot')

    # Referenced history supplies only missing facts, never old prose/permissions.
    for identity, resolved in (referenced_evidence or {}).items():
        if not identity or str(identity).lower() in {'null', 'none'}:
            continue
        if str(identity).startswith('toolu_') and resolved.get('resolution') != 'matched':
            continue
        if resolved.get('resolution') != 'matched':
            if resolved.get('affects_current_action') is True:
                issue('referenced_evidence_' + resolved.get('resolution', 'unavailable'), operation_id=identity,
                      missing_evidence='original_operation_in_current_account')
            continue
        receipt = resolved.get('latest') or {}
        parts = [i.get('result', i) for i in receipt.get('results', [])] or [receipt]
        for part in parts:
            if (part.get('cleanup') or {}).get('status') == 'pending' and not (part.get('cleanup') or {}).get('orders'):
                if not any(i['reason_code'] == 'cycle_cleanup_unverified' and i.get('side') == part.get('side') for i in issues.values()):
                    issue('cycle_cleanup_unverified', part.get('side'), operation_id=part.get('operation_id', identity),
                          missing_evidence='position_and_cleanup_fills')
            for blocker in (part.get('cleanup') or {}).get('orders', []):
                if not any(i.get('order_id') == blocker.get('order_id') and i.get('side') == blocker.get('side') for i in issues.values()):
                    issue(blocker.get('reason_code', 'order_unresolved'), blocker.get('side'),
                          order_id=blocker.get('order_id'), operation_id=blocker.get('operation_id'))
            if part.get('status') in {'unknown', 'pending'} and not any(i.get('operation_id') == part.get('operation_id', identity) for i in issues.values()):
                issue('execution_unverified', part.get('side'), operation_id=part.get('operation_id', identity),
                      order_id=part.get('order_id'), missing_evidence='verified_execution_outcome')

    amounts = {side: sum(p['amount'] for p in positions if p['side'] == side and p['amount'] is not None) for side in ('LONG', 'SHORT')}
    uncovered, conflicts = {}, []
    for side in amounts:
        exits = [o for o in orders.values() if o['side'] == side and o['role'] == 'exit']
        stops = [o for o in exits if exit_group(o) == 'stop_loss' and o.get('verified')]
        unknown = position_failed or (not is_mock and plans is None) or any(p['amount'] is None for p in positions if p['side'] == side)
        unknown |= any((evidence.get(component) or {}).get('error') for component in ('orders', 'trigger_orders'))
        unknown |= any(o['remaining'] is None and o['quantity_mode'] != 'all' for o in stops)
        protected = amounts[side] if any(o['quantity_mode'] == 'all' for o in stops) else sum(o['remaining'] or 0 for o in stops)
        uncovered[side] = None if unknown else max(amounts[side] - protected, 0)
        for group in {exit_group(o) for o in exits}:
            qty = sum(o['remaining'] or 0 for o in exits if exit_group(o) == group and o['quantity_mode'] == 'fixed')
            if not unknown and qty > amounts[side] + 1e-12:
                conflicts.append(dict(side=side, group=group, fixed_remaining=qty, position_amount=amounts[side]))
    return DecisionTradeState(scope, positions, sorted(orders.values(), key=lambda o: str(o['order_id'])),
                              dict(uncovered=uncovered, quantity_conflict=conflicts),
                              sorted(issues.values(), key=lambda i: i['identity']),
                              dict(sources=evidence, local_cycles='unavailable' if plans is None and not is_mock else 'read',
                                   positions='unavailable' if position_failed else 'read',
                                   cycle_versions={p['episode_id']: p.get('record_version') for p in cycles}))
