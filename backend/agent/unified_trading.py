"""Versioned capability contracts and bounded, durable ordered execution.

Only trusted callers choose the execution engine. The schema contains trading
intent, never arbitrary handler names, credentials, or account overrides.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import os
import time
import uuid
from typing import Callable

from backend.agent.trading_contracts import UnifiedTradeActionsSchema
from backend.utils.trade_operations import current_operation_id, run_once, tool_result_status, _decode_receipt, _same_symbol


current_batch_approvals: ContextVar[dict[int, bool] | None] = ContextVar('batch_approvals', default=None)
_active_batches: set[tuple[str, str]] = set()


def capabilities_for_config(config: dict | None, *, engine: str = 'live') -> dict:
    cfg = config or {}
    if engine not in {'live', 'paper'}:
        raise ValueError('Unsupported trusted execution engine')
    mode = str(cfg.get('mode') or 'STRATEGY').upper()
    paper = engine == 'paper'
    market = 'paper' if paper else ('spot' if mode == 'SPOT_DCA' or cfg.get('market_type') == 'spot' else 'swap')
    if not paper and mode not in {'REAL', 'STRATEGY', 'SPOT_DCA'}:
        raise ValueError('Unsupported trading mode')
    from backend.utils.exit_policy import effective_exit_mode
    exit_mode = 'paper' if paper else effective_exit_mode(cfg)
    independent = market == 'swap' and exit_mode == 'independent_exits'
    close_types = (['market', 'limit', 'stop_loss', 'take_profit', 'stop_market', 'take_profit_market'] if paper else
                   ['market', 'limit', 'stop_loss', 'take_profit', 'stop_market', 'take_profit_market', 'oco'] if market == 'spot' else
                   ['market', 'limit', 'stop_market', 'take_profit_market', 'stop_limit', 'take_profit_limit'] if independent else
                   ['market'] if mode == 'STRATEGY' else ['market', 'limit', 'stop_market'])
    return {'contract_version': 3, 'engine': engine, 'mode': mode, 'market_type': market,
            'markets': list(cfg.get('markets') or ['spot', 'swap']) if paper else [market],
            'exit_mode': exit_mode, 'actions': ['open', 'close', 'edit', 'cancel'] + ([] if independent else ['protect']),
            'open_order_types': ['market', 'limit'] if paper else ['limit'], 'close_order_types': close_types,
            'sizing': ['amount', 'quote_amount', 'margin_amount'] if paper else ['amount'],
            'execution_policies': ['stop_on_error', 'independent'], 'max_actions': 20,
            'query_required_before_action': False, 'quantity_units': 'base', 'verification': 'automatic_background'}


def _action_fields(action: str, caps: dict) -> set[str]:
    fields = {'action', 'symbol', 'reason', 'depends_on'}
    paper = caps['engine'] == 'paper'
    spot = caps['market_type'] == 'spot'
    if paper:
        fields |= {'market_type', 'exchange'}
    if action == 'open':
        fields |= {'side', 'order_type', 'amount', 'price'}
        if paper:
            fields |= {'quote_amount', 'margin_amount', 'leverage', 'stop_loss', 'take_profit'}
        elif not spot and caps['exit_mode'] != 'independent_exits':
            fields |= {'stop_loss', 'take_profit'}
        if not paper and caps['mode'] == 'STRATEGY':
            fields.add('valid_duration_hours')
    elif action == 'close':
        fields |= {'side', 'order_type', 'amount', 'close_all', 'price', 'trigger_price'}
        if spot:
            fields.discard('close_all')
        if spot or paper:
            fields |= {'stop_loss', 'take_profit'}
        if spot:
            fields |= {'stop_limit_price', 'take_profit_limit_price'}
    elif action == 'edit':
        fields |= {'order_id', 'remaining_amount', 'price'}
        if paper or caps['market_type'] == 'swap' and caps['exit_mode'] == 'independent_exits':
            fields.add('trigger_price')
        if not paper and caps['mode'] == 'STRATEGY' and caps['exit_mode'] != 'independent_exits':
            fields |= {'stop_loss', 'take_profit'}
    elif action == 'cancel':
        fields.add('order_id')
    elif action == 'protect':
        fields |= {'side', 'stop_loss', 'take_profit'}
        if paper or spot:
            fields |= {'amount', 'close_all'}
        if spot:
            fields.discard('close_all')
            fields |= {'stop_limit_price', 'take_profit_limit_price'}
        if not paper and caps['mode'] == 'STRATEGY':
            fields.add('order_id')
    return fields


def validate_for_config(payload, config: dict | None, *, engine: str = 'live',
                        default_symbol: str | None = None, allowed_symbols=None) -> UnifiedTradeActionsSchema:
    parsed = UnifiedTradeActionsSchema.model_validate(payload)
    cfg = config or {}
    caps = capabilities_for_config(cfg, engine=engine)
    configured = list(allowed_symbols if allowed_symbols is not None else cfg.get('symbols') or
                      ([cfg['symbol']] if cfg.get('symbol') else []))
    unrestricted = cfg.get('mcp_symbol_scope', cfg.get('symbol_scope')) == 'all' or engine == 'paper'
    if not configured and default_symbol and not unrestricted:
        configured = [default_symbol]
    def normalize(value):
        text = str(value).upper()
        if caps['market_type'] == 'swap' and '/' in text and ':' not in text:
            text += ':' + text.split('/')[-1]
        return text
    normalized = {normalize(value) for value in configured}
    for index, action in enumerate(parsed.actions):
        def reject(message):
            raise ValueError(f'actions[{index}]: {message}')
        if action.action not in caps['actions']:
            reject('unsupported_combination: action is unavailable in this mode')
        extra = action.model_fields_set - _action_fields(action.action, caps)
        if extra:
            reject('unsupported fields for this mode: ' + ', '.join(sorted(extra)))
        selected = action.symbol
        if selected is None:
            if len(configured) == 1:
                selected = configured[0]
            elif len(configured) > 1:
                reject('symbol is required for a multi-symbol task/profile')
            else:
                selected = default_symbol
        if not selected and (engine == 'live' or action.action in {'open', 'close', 'protect'}):
            reject('symbol is required when no unique trusted default exists')
        if selected and ('/' not in selected or not unrestricted and normalize(selected) not in normalized):
            reject('symbol is outside the trusted trading scope')
        if selected:
            action.symbol = selected
        market = action.market_type or (caps['markets'][0] if len(caps['markets']) == 1 else None) if engine == 'paper' else caps['market_type']
        if engine == 'paper' and action.action in {'open', 'close', 'protect'} and market not in caps['markets']:
            reject('market_type is required and must be supported by the paper account')
        if market == 'spot' and getattr(action, 'side', 'LONG') != 'LONG':
            reject('spot uses side=LONG: open buys and close sells; SHORT is unsupported')
        if action.action == 'open':
            if action.order_type not in caps['open_order_types']:
                reject('unsupported_combination: live entry supports limit only')
            if market == 'spot' and (action.margin_amount is not None or action.leverage is not None):
                reject('spot does not accept leverage or margin_amount')
            if engine == 'paper' and action.leverage is not None and (not action.leverage.is_integer() or not 1 <= action.leverage <= min(125, int(cfg.get('max_leverage') or 125))):
                reject('paper leverage must be an integer within the account maximum and 1..125')
            if engine == 'live' and caps['exit_mode'] == 'attached_required' and market == 'swap':
                if action.stop_loss is None or action.take_profit is None:
                    reject('attached_required requires stop_loss and take_profit')
        if action.action == 'close':
            if action.order_type not in caps['close_order_types']:
                reject('unsupported_combination: order_type is unavailable in this mode')
            if engine == 'live' and market == 'spot' and action.close_all:
                reject('unsupported_combination: live spot exits require fixed amount')
            if engine == 'live' and caps['exit_mode'] == 'independent_exits' and action.close_all and action.order_type not in {'market', 'stop_market', 'take_profit_market'}:
                reject('unsupported_combination: independent close_all requires market or conditional market')
        if action.action == 'protect':
            if engine == 'live' and market == 'spot' and action.amount is None:
                reject('spot protection requires fixed amount')
            if engine == 'live' and caps['mode'] == 'STRATEGY' and not action.order_id:
                reject('strategy attached protection requires the existing order_id')
    return parsed


def unified_schema_for_config(config: dict | None, *, engine: str = 'live') -> dict:
    caps = capabilities_for_config(config, engine=engine)
    schema = deepcopy(UnifiedTradeActionsSchema.model_json_schema())
    items = schema['properties']['actions']['items']
    mapping = items['discriminator']['mapping']
    for name in list(mapping):
        if name not in caps['actions']:
            mapping.pop(name)
    items['oneOf'] = [{'$ref': ref} for ref in mapping.values()]
    keep_defs = set(ref.rsplit('/', 1)[-1] for ref in mapping.values())
    for name in list(schema.get('$defs', {})):
        if name not in keep_defs:
            schema['$defs'].pop(name)
    for action_name, ref in mapping.items():
        definition = schema['$defs'][ref.rsplit('/', 1)[-1]]
        fields = _action_fields(action_name, caps)
        definition['properties'] = {key: value for key, value in definition['properties'].items() if key in fields}
        definition['required'] = [key for key in definition.get('required', []) if key in fields]
        if caps['engine'] == 'live':
            if 'side' in fields and caps['market_type'] == 'spot':
                definition['properties']['side'] = {'type': 'string', 'const': 'LONG'}
            if action_name == 'open':
                definition['properties']['amount'] = {'type': 'number', 'exclusiveMinimum': 0, 'description': '基础币数量'}
                definition['properties']['price'] = {'type': 'number', 'exclusiveMinimum': 0}
                definition['properties']['order_type'] = {'type': 'string', 'const': 'limit', 'default': 'limit'}
                definition['required'] += [key for key in ('amount', 'price') if key not in definition['required']]
                if caps['market_type'] == 'swap' and caps['exit_mode'] == 'attached_required':
                    for key in ('stop_loss', 'take_profit'):
                        definition['properties'][key] = {'type': 'number', 'exclusiveMinimum': 0}
                        definition['required'].append(key)
            elif action_name == 'close':
                definition['properties']['order_type'] = {'type': 'string', 'enum': caps['close_order_types']}
            elif action_name == 'protect' and caps['mode'] == 'STRATEGY':
                definition['required'].append('order_id')
            if caps['market_type'] == 'spot' and action_name in {'close', 'protect'}:
                definition['properties']['amount'] = {'type': 'number', 'exclusiveMinimum': 0}
                definition['required'].append('amount')
        else:
            if action_name in {'open', 'close', 'protect'}:
                definition['required'].append('symbol')
                if len(caps['markets']) != 1:
                    definition['required'].append('market_type')
            if action_name == 'open':
                definition['properties']['leverage'] = {'anyOf': [
                    {'type': 'integer', 'minimum': 1, 'maximum': min(125, int((config or {}).get('max_leverage') or 125))}, {'type': 'null'}]}
        properties = definition['properties']
        positive = {'type': 'number', 'exclusiveMinimum': 0}
        if action_name == 'open' and caps['engine'] == 'paper':
            sizing = [key for key in ('amount', 'quote_amount', 'margin_amount') if key in properties]
            definition['oneOf'] = [{'required': [key], 'properties': {
                name: positive if name == key else {'type': 'null'} for name in sizing}} for key in sizing]
            definition['allOf'] = [{'if': {'required': ['order_type'], 'properties': {'order_type': {'const': 'market'}}},
                                     'then': {'properties': {'price': {'type': 'null'}}},
                                     'else': {'required': ['price'], 'properties': {'price': positive}}}]
        elif action_name == 'close':
            if 'close_all' in properties:
                definition['oneOf'] = [
                    {'required': ['amount'], 'properties': {'amount': positive, 'close_all': {'type': 'null'}}},
                    {'required': ['close_all'], 'properties': {'close_all': {'const': True}, 'amount': {'type': 'null'}}}]
            rules = []
            for kind in caps['close_order_types']:
                required = {'limit': ['price'], 'stop_market': ['trigger_price'], 'take_profit_market': ['trigger_price'],
                            'stop_limit': ['trigger_price', 'price'], 'take_profit_limit': ['trigger_price', 'price'],
                            'oco': ['stop_loss', 'take_profit']}.get(kind, [])
                allowed = set(required)
                if kind in {'stop_loss', 'take_profit'}:
                    allowed |= {'trigger_price', kind, 'stop_limit_price' if kind == 'stop_loss' else 'take_profit_limit_price'}
                if kind == 'oco':
                    allowed |= {'stop_limit_price', 'take_profit_limit_price'}
                condition = {'required': required, 'properties': {field: positive if field in required else {'type': 'null'}
                    for field in ('price', 'trigger_price', 'stop_loss', 'take_profit', 'stop_limit_price', 'take_profit_limit_price')
                    if field in properties and (field in required or field not in allowed)}}
                if kind in {'stop_loss', 'take_profit'}:
                    condition['oneOf'] = [
                        {'required': ['trigger_price'], 'properties': {'trigger_price': positive, kind: {'type': 'null'}}},
                        {'required': [kind], 'properties': {kind: positive, 'trigger_price': {'type': 'null'}}}]
                if caps['exit_mode'] == 'independent_exits' and kind not in {'market', 'stop_market', 'take_profit_market'} and 'close_all' in properties:
                    condition['properties']['close_all'] = {'type': 'null'}
                rules.append({'if': {'properties': {'order_type': {'const': kind}}}, 'then': condition})
            definition['allOf'] = rules
        elif action_name in {'edit', 'protect'}:
            changes = ['remaining_amount', 'price', 'trigger_price', 'stop_loss', 'take_profit'] if action_name == 'edit' else ['stop_loss', 'take_profit']
            definition['anyOf'] = [{'required': [key], 'properties': {key: positive}} for key in changes if key in properties]
        definition['required'] = list(dict.fromkeys(definition.get('required', [])))
    return schema


def compact_trade_receipt(payload):
    """Project bounded facts after preserving the full durable receipt."""
    from backend.utils.trade_summary import compact_trade_receipt as bounded_summary
    return bounded_summary(payload)


def stable_child_operation_id(owner_id: str, parent_id: str, index: int) -> str:
    candidate = f'{parent_id}:{index}'
    return candidate if len(candidate) <= 160 else 'v3:' + hashlib.sha256(
        f'{owner_id}\0{parent_id}\0{index}'.encode()).hexdigest()


def _initialize_batches(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS unified_trade_batches (
        config_id TEXT NOT NULL,parent_operation_id TEXT NOT NULL,worker_pid INTEGER NOT NULL,
        state TEXT NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(config_id,parent_operation_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS unified_trade_children (
        config_id TEXT NOT NULL,parent_operation_id TEXT NOT NULL,action_index INTEGER NOT NULL,
        child_operation_id TEXT NOT NULL,symbol TEXT NOT NULL,
        PRIMARY KEY(config_id,parent_operation_id,action_index),UNIQUE(config_id,child_operation_id))''')


def _prepare_children(config_id, parent_id, parsed, default_symbol):
    from backend import database
    with database.get_db_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _initialize_batches(conn)
        conn.execute('INSERT INTO unified_trade_batches VALUES(?,?,?,?,?)',
                     (config_id, parent_id, os.getpid(), 'running', time.time()))
        for index, action in enumerate(parsed.actions):
            conn.execute('INSERT INTO unified_trade_children VALUES(?,?,?,?,?)',
                         (config_id, parent_id, index, stable_child_operation_id(config_id, parent_id, index), action.symbol or default_symbol))
        conn.commit()


def _failure(payload, parent_id, exc):
    return {'contract_version': 3, 'receipt_version': 1, 'operation_id': parent_id, 'status': 'failed',
            'phase': 'preflight', 'submitted': False, 'error': str(exc), 'error_code': 'invalid_request',
            'results': [{'index': index, 'action': item.get('action'), 'status': 'not_executed'}
                        for index, item in enumerate(payload.get('actions', []))]}


def _configuration_unavailable(payload, parent_id):
    return {**_failure(payload, parent_id, ValueError('Trading configuration is missing or has been deleted')),
            'error_code': 'configuration_unavailable', 'submission_state': 'not_submitted'}


def _replay_without_configuration(payload, config_id, symbol, parent_id, engine):
    """A retired task can query its original intent, but cannot infer a new engine."""
    from backend import database
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        saved = conn.execute('SELECT request_json FROM trade_operation_requests WHERE config_id=? AND operation_id=?',
                             (config_id, parent_id)).fetchone() if 'trade_operation_requests' in tables else None
    if not saved:
        return _configuration_unavailable(payload, parent_id)
    intent = json.loads(saved[0])
    context = intent.get('trusted_context', {})
    def serialize(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    if (intent.get('contract_version') != 3 or intent.get('tool') != 'execute_trade_actions' or
            context.get('config_id') != config_id or context.get('symbol') != symbol or
            context.get('engine') != engine or serialize(intent.get('args')) != serialize(payload)):
        raise ValueError('操作 ID 已用于不同请求；禁止重用')
    return query_unified_operation(config_id, symbol, parent_id, read_exchange=False)


def run_ordered_actions(parsed, parent_id, owner_id, dispatch: Callable, *, approval=None) -> dict:
    """Shared policy loop; the injected dispatcher owns each engine's commit."""
    results, blocked = [], None
    approvals = current_batch_approvals.get()
    for index, action in enumerate(parsed.actions):
        child_id = stable_child_operation_id(owner_id, parent_id, index)
        row = {'index': index, 'action': action.action, 'operation_id': child_id, 'symbol': action.symbol}
        row.update({key: value for key, value in action.model_dump(exclude_none=True).items()
                    if key in {'side', 'amount', 'price', 'trigger_price', 'order_id', 'stop_loss', 'take_profit'}})
        if getattr(action, 'close_all', False):
            row['quantity_mode'] = 'all'
        if blocked is not None:
            results.append({**row, 'status': 'not_executed', 'blocked_by_index': blocked})
            continue
        unmet = [dependency for dependency in action.depends_on if results[dependency]['status'] != 'completed']
        if unmet:
            results.append({**row, 'status': 'not_executed', 'reason_code': 'dependency_not_completed', 'blocked_by_indices': unmet})
            continue
        accepted = approval(action, index) if approval else approvals.get(index, True) if approvals is not None else True
        if not accepted:
            results.append({**row, 'status': 'not_executed', 'rejected_by_user': True, 'reason_code': 'user_rejected'})
            blocked = index
            continue
        result = _decode_receipt(dispatch(action, index, child_id, action.symbol))
        if not isinstance(result, dict):
            result = {'status': tool_result_status(result), 'message': str(result)}
        result.setdefault('operation_id', child_id)
        status = tool_result_status(result)
        results.append({**row, 'status': status, 'result': result})
        if status in {'unknown', 'pending'} or (parsed.execution_policy == 'stop_on_error' and status in {'failed', 'not_executed'}):
            blocked = index
    status = next((candidate for candidate in ('unknown', 'pending', 'failed') if any(row['status'] == candidate for row in results)),
                  'failed' if any(row['status'] == 'not_executed' for row in results) else
                  'submitted' if any(row['status'] == 'submitted' for row in results) else 'completed')
    return {'contract_version': 3, 'receipt_version': 1, 'operation_id': parent_id, 'status': status,
            'execution_policy': parsed.execution_policy, 'results': results,
            'partial_execution': any(row['status'] in {'completed', 'submitted'} for row in results) and status in {'unknown', 'pending', 'failed'},
            'rejected_by_user': any(row.get('rejected_by_user') for row in results),
            'summary': {candidate: sum(row['status'] == candidate for row in results) for candidate in
                        ('completed', 'submitted', 'failed', 'unknown', 'pending', 'not_executed', 'skipped')}}


def execute_v3_actions(payload, *, config_id: str, symbol: str, operation_id: str | None = None,
                       config: dict | None = None, engine: str = 'live', dispatch=None, preflight=None,
                       approval=None, account_summary=None, allowed_symbols=None) -> dict:
    """Durable live orchestration; paper may reuse validation/policy with its own transactions."""
    parent_id = operation_id or current_operation_id.get() or uuid.uuid4().hex
    if config is None:
        from backend.config import config as runtime
        config = runtime.get_config_by_id(config_id)
    if not config:
        return _replay_without_configuration(payload, config_id, symbol, parent_id, engine)
    request = {'tool': 'execute_trade_actions', 'contract_version': 3, 'args': payload,
               'trusted_context': {'config_id': config_id, 'symbol': symbol, 'engine': engine,
                                   'mode': config.get('mode', 'STRATEGY'),
                                   'market_type': config.get('market_type', 'spot' if config.get('mode') == 'SPOT_DCA' else 'swap'),
                                   'exit_mode': config.get('exit_mode')}}
    def execute_parent_unlocked():
        if dispatch is None and engine == 'live':
            from backend.config import config as runtime
            if not runtime.get_config_by_id(config_id):
                return _configuration_unavailable(payload, parent_id)
        try:
            parsed = validate_for_config(payload, config, engine=engine, default_symbol=symbol, allowed_symbols=allowed_symbols)
            if preflight:
                preflight(parsed)
            elif dispatch is None and engine == 'live' and config.get('mode') == 'REAL':
                # Position mode is a static capability. Balances, quantities and
                # leverage admission stay inside the individual action.
                from backend.agent.trade_batch import _validate_batch_direction
                for selected in dict.fromkeys(action.symbol for action in parsed.actions):
                    _validate_batch_direction([action for action in parsed.actions if action.symbol == selected],
                                              config_id, selected, 'REAL')
        except (ValueError, PermissionError) as exc:
            return _failure(payload, parent_id, exc)
        _prepare_children(config_id, parent_id, parsed, symbol)
        _active_batches.add((config_id, parent_id))
        from backend.utils.spot_execution import current_spot_cycle_id, ensure_spot_budget_cycle
        spot_cycle = current_spot_cycle_id.get()
        new_spot_cycle = config.get('mode') == 'SPOT_DCA' and not spot_cycle
        spot_cycle = spot_cycle or parent_id
        spot_token = current_spot_cycle_id.set(spot_cycle) if config.get('mode') == 'SPOT_DCA' else None
        registered_cycle = not new_spot_cycle
        try:
            def commit(action, index, child_id, selected_symbol):
                nonlocal registered_cycle
                values = action.model_dump(exclude_none=True)
                def write():
                    nonlocal registered_cycle
                    if new_spot_cycle and action.action == 'open' and not registered_cycle:
                        try:
                            ensure_spot_budget_cycle(config_id, config, spot_cycle, 'v3_batch')
                            registered_cycle = True
                        except ValueError as exc:
                            return {'contract_version': 3, 'receipt_version': 1, 'status': 'failed',
                                    'phase': 'preflight', 'submitted': False, 'error_code': 'spot_budget_rejected', 'error': str(exc)}
                    if dispatch:
                        return dispatch(action, index, child_id, selected_symbol)
                    from backend.agent.unified_dispatch import dispatch_live_action
                    return dispatch_live_action(action, config_id, selected_symbol, config, child_id)
                return run_once(config_id, selected_symbol or symbol, child_id,
                                {'contract_version': 3, 'action': action.action, 'arguments': values,
                                 'trusted_context': request['trusted_context']}, write)
            receipt = run_ordered_actions(parsed, parent_id, config_id, commit, approval=approval)
            if 'account' in parsed.include:
                try:
                    if account_summary:
                        receipt['account_summary'] = account_summary()
                    else:
                        from backend.agent.unified_dispatch import live_account_summary
                        receipt['account_summary'] = live_account_summary(config_id, parsed, config)
                except Exception as exc:
                    receipt['account_summary'] = {'status': 'failed', 'error': str(exc), 'observed_at': None}
            from backend import database
            with database.get_db_conn() as conn:
                conn.execute('UPDATE unified_trade_batches SET state=?,updated_at=? WHERE config_id=? AND parent_operation_id=?',
                             ('finished', time.time(), config_id, parent_id))
                conn.commit()
            return receipt
        finally:
            if spot_token is not None:
                current_spot_cycle_id.reset(spot_token)
            _active_batches.discard((config_id, parent_id))
    def execute_parent():
        from backend.utils.management_lock import scope_file_lock
        with scope_file_lock('unified-batch:' + config_id, parent_id, 'BOTH'):
            return execute_parent_unlocked()
    result = _decode_receipt(run_once(config_id, symbol, parent_id, request, execute_parent))
    if isinstance(result, dict) and result.get('contract_version') == 3:
        return result
    # A lost parent response cannot cause its financial callback to run again.
    return query_unified_operation(config_id, symbol, parent_id, read_exchange=False) or result


def query_unified_operation(config_id: str, symbol: str, operation_id: str, *, read_exchange=True,
                            market_tool=None, recovery_scope=None):
    from backend import database
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'trade_operation_requests' not in tables:
            return None
        request = conn.execute('SELECT request_json FROM trade_operation_requests WHERE config_id=? AND operation_id=?',
                               (config_id, operation_id)).fetchone()
        intent = json.loads(request[0]) if request else {}
        if intent.get('contract_version') != 3:
            return None
        parent = conn.execute('SELECT * FROM trade_action_runs WHERE config_id=? AND operation_id=?',
                              (config_id, operation_id)).fetchone()
        if not parent:
            return {'status': 'failed', 'error_code': 'operation_not_found', 'operation_id': operation_id}
        if intent.get('tool') != 'execute_trade_actions':
            matches = parent['symbol'].split('/')[0] == symbol.split('/')[0] if (recovery_scope or {}).get('market_type') == 'spot' else _same_symbol(parent['symbol'], symbol)
            if not matches:
                return {'status': 'failed', 'error_code': 'operation_not_found', 'operation_id': operation_id}
            if (recovery_scope or {}).get('side') in {'LONG', 'SHORT'} and _original_action_side(
                    config_id, parent['symbol'], operation_id, intent.get('arguments', {}), _decode_receipt(parent['result'])) != recovery_scope['side']:
                return {'status': 'unknown', 'operation_id': operation_id, 'error_code': 'accepted_scope_evidence_unavailable',
                        'query': {'status': 'completed', 'source': 'local_scope_lookup'}}
            return _refresh_child(config_id, parent['symbol'], operation_id, _decode_receipt(parent['result']), read_exchange,
                                  market_tool=market_tool)
        if not recovery_scope and not _same_symbol(parent['symbol'], symbol):
            return {'status': 'failed', 'error_code': 'operation_not_found', 'operation_id': operation_id}
        previous = _decode_receipt(parent['result'])
        mappings = conn.execute('SELECT * FROM unified_trade_children WHERE config_id=? AND parent_operation_id=? ORDER BY action_index',
                                (config_id, operation_id)).fetchall() if 'unified_trade_children' in tables else []
        child_rows = {row['operation_id']: dict(row) for row in conn.execute('SELECT * FROM trade_action_runs WHERE config_id=?', (config_id,))}
    if not mappings:
        return previous or {'status': 'pending', 'operation_id': operation_id, 'contract_version': 3, 'receipt_version': 1}
    active = (config_id, operation_id) in _active_batches
    if not active:
        from backend.utils.management_lock import scope_file_lock, ManagementLockBusy
        try:
            with scope_file_lock('unified-batch:' + config_id, operation_id, 'BOTH'):
                pass
        except ManagementLockBusy:
            active = True
    output = dict(previous) if isinstance(previous, dict) else {
        'contract_version': 3, 'receipt_version': 1, 'operation_id': operation_id, 'results': []}
    output.update(contract_version=3, receipt_version=1, operation_id=operation_id)
    stored = {item['index']: item for item in output.get('results', [])}
    actions = json.loads(request[0])['args']['actions']
    rows = []
    for mapping in mappings:
        index, child_id = mapping['action_index'], mapping['child_operation_id']
        row = stored.get(index) or {'index': index, 'action': actions[index]['action'], 'operation_id': child_id, 'symbol': mapping['symbol']}
        if recovery_scope:
            target = recovery_scope.get('symbol', symbol)
            scoped_symbol = mapping['symbol']
            if recovery_scope.get('market_type') == 'spot':
                allowed = scoped_symbol.split('/')[0] == target.split('/')[0]
            else:
                allowed = _same_symbol(scoped_symbol, target)
            side = _original_action_side(config_id, scoped_symbol, child_id, actions[index], row.get('result'))
            if not allowed or recovery_scope.get('side') in {'LONG', 'SHORT'} and side != recovery_scope['side']:
                continue
        child = child_rows.get(child_id)
        if row.get('status') == 'not_executed':
            rows.append(row)
            continue
        if child:
            result = _refresh_child(config_id, mapping['symbol'], child_id,
                                    _decode_receipt(child['result']), read_exchange, market_tool=market_tool)
            if isinstance(result, dict):
                row = {**row, 'status': tool_result_status(result), 'result': result}
            else:
                row = {**row, 'status': 'pending' if active else 'unknown',
                       'recovery': {'state': 'background', 'target_operation_id': operation_id}}
        else:
            row = {**row, 'status': 'pending' if active else 'not_executed'}
        rows.append(row)
    output['results'] = rows
    if recovery_scope:
        output = {key: value for key, value in output.items() if key in {
            'contract_version', 'receipt_version', 'operation_id', 'results', 'execution_policy'}}
    output['status'] = next((value for value in ('unknown', 'pending', 'failed') if any(row['status'] == value for row in rows)),
                            'failed' if any(row['status'] == 'not_executed' for row in rows) else
                            'submitted' if any(row['status'] == 'submitted' for row in rows) else 'completed')
    if recovery_scope and not rows:
        output.update(status='unknown', error_code='accepted_scope_evidence_unavailable')
    query_errors = [{'index': row['index'], 'errors': row['result']['query'].get('errors', [])}
                    for row in rows if isinstance(row.get('result'), dict) and row['result'].get('query', {}).get('status') == 'failed']
    output['query'] = {'status': 'partial' if query_errors and len(query_errors) < len(rows) else 'failed' if query_errors else 'completed',
                       'queried_at': time.time(), 'source': 'operation_evidence',
                       **({'errors': query_errors} if query_errors else {})}
    output['summary'] = {value: sum(row['status'] == value for row in rows) for value in
                         ('completed', 'submitted', 'failed', 'unknown', 'pending', 'not_executed', 'skipped')}
    output['partial_execution'] = any(row['status'] in {'completed', 'submitted'} for row in rows) and output['status'] in {'unknown', 'pending', 'failed'}
    if not active and not recovery_scope:
        _save_projection(config_id, operation_id, output)
    return output


def _original_action_side(config_id, symbol, operation_id, args, receipt):
    receipt = _decode_receipt(receipt)
    side = args.get('side') or (receipt.get('side') if isinstance(receipt, dict) else None)
    if side in {'LONG', 'SHORT'}:
        return side
    from backend import database
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'trade_operation_evidence' in tables:
            evidence = conn.execute("SELECT json_extract(c.payload,'$.side') FROM trade_cycle_evidence c "
                                   'JOIN trade_operation_evidence e USING(config_id,account_scope,symbol,episode_id) '
                                   'WHERE e.config_id=? AND e.operation_id=? LIMIT 1',
                                    (config_id, operation_id)).fetchone()
            if evidence:
                return evidence[0]
        plans = conn.execute("SELECT payload FROM real_protection_plans WHERE config_id=? OR json_extract(payload,'$.original_owner')=?",
                             (config_id, config_id)).fetchall() if 'real_protection_plans' in tables else []
    for saved in plans:
        plan = json.loads(saved[0])
        if not _same_symbol(plan['symbol'], symbol):
            continue
        for record in plan.get('entries', []) + plan.get('exits', []) + plan.get('legs', []):
            identities = [record.get(key) for key in ('operation_id', 'last_operation_id', 'cancel_operation_id')]
            identities += [(record.get(key) or {}).get('operation_id') for key in ('amendment', 'replacement')]
            if operation_id in identities:
                return plan.get('side')
    return None


def _save_projection(config_id, operation_id, receipt):
    from backend import database
    from backend.utils.trade_evidence import append_receipt
    with database.get_db_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        append_receipt(conn, config_id, operation_id, receipt)
        conn.execute('UPDATE trade_action_runs SET status=?,result=?,updated_at=? WHERE config_id=? AND operation_id=?',
                     (tool_result_status(receipt), json.dumps(receipt, ensure_ascii=False), time.time(), config_id, operation_id))
        conn.commit()


def _refresh_child(config_id, symbol, operation_id, previous, read_exchange, *, market_tool=None):
    from backend import database
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        simulated = conn.execute('SELECT result FROM mock_trade_operations WHERE config_id=? AND symbol=? AND operation_id=?',
                                 (config_id, symbol, operation_id)).fetchone() if 'mock_trade_operations' in tables else None
    saved_simulation = json.loads(simulated[0]) if simulated else None
    simulated = simulated if isinstance(saved_simulation, dict) and saved_simulation.get('status') else None
    result = saved_simulation if simulated else previous
    if isinstance(result, dict):
        result = {**result, 'operation_id': operation_id, 'contract_version': 3, 'receipt_version': 1}
        if result.get('execution_operation_id') and not simulated:
            with database.get_db_conn() as conn:
                native = conn.execute('SELECT result FROM trade_action_runs WHERE config_id=? AND symbol=? AND operation_id=?',
                                      (config_id, symbol, result['execution_operation_id'])).fetchone()
            observed = _decode_receipt(native[0]) if native and native[0] else None
            if isinstance(observed, dict) and len(observed.get('results', [])) == 1:
                observed = observed['results'][0].get('result', observed['results'][0])
            if isinstance(observed, dict) and observed.get('status') not in {None, 'unknown', 'pending', 'running'}:
                result.update({key: value for key, value in observed.items() if key in {
                    'status', 'order_id', 'order_status', 'amount', 'filled', 'filled_amount', 'remaining', 'remaining_amount', 'price'}})
        if simulated:
            identity = result.get('order_id') or result.get('id')
            if identity:
                identity = str(identity).removeprefix('regular:').removeprefix('algo:')
                with database.get_db_conn() as conn:
                    order = conn.execute('SELECT * FROM mock_orders WHERE config_id=? AND symbol=? AND order_id=?',
                                         (config_id, symbol, identity)).fetchone()
                    if order:
                        order_state = 'filled' if order['is_filled'] else str(order['status']).lower()
                        result['order_status'] = {'cancelled': 'canceled'}.get(order_state, order_state)
                        result['filled_amount'] = order['amount'] if order['is_filled'] else order['filled_amount'] or 0
                        if result.get('action') == 'open' or result.get('status') in {'open', 'submitted'}:
                            result['status'] = 'submitted' if order_state == 'open' else 'completed'
    cleanup_pending = isinstance(result, dict) and isinstance(result.get('cleanup'), dict) and result['cleanup'].get('status') in {'pending', 'unknown'}
    if not simulated and (not isinstance(result, dict) or result.get('status') in {'unknown', 'pending', 'running', 'submitted'} or cleanup_pending):
        if not read_exchange:
            from backend.utils.trade_operations import has_scoped_evidence, refresh_operation_receipt
            with database.get_db_conn() as conn:
                scoped = has_scoped_evidence(conn, config_id, operation_id)
            if scoped:
                result = refresh_operation_receipt(config_id, symbol, operation_id, result, read_exchange=False)
        else:
            from backend.agent.unified_dispatch import refresh_native_operation
            result = refresh_native_operation(config_id, symbol, operation_id, result, market_tool=market_tool)
    if isinstance(result, dict) and result != previous:
        _save_projection(config_id, operation_id, result)
    return result
