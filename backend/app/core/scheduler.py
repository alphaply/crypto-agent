import concurrent.futures
import json
import os
from backend import storage
import time
import threading
import math
from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv

from backend.agent.agent_graph import run_agent_for_config
from backend.config import config as global_config
from backend.database import init_db
from backend.utils.logger import setup_logger
from backend.utils.llm_utils import sync_langsmith_environment
from backend.utils.market_data import MarketTool
from backend.utils.run_schedule import normalize_dca_freq, parse_dca_time, latest_dca_slot, dca_cycle_id
from backend.utils.runtime_capacity import (CapacityBusy, get_capacity_manager,
                                           positive_setting, submit_capacity, reserve_bytes)


load_dotenv()

TZ_CN = pytz.timezone(getattr(global_config, "timezone", "Asia/Shanghai"))
logger = setup_logger("MainScheduler")

_last_run_times = {}
_agent_executor = None
_maintenance_executor = None
_running_agent_futures = {}
_running_maintenance_futures = {}
_scheduler_lock = threading.RLock()
_last_heartbeat_key = None
_protection_thread = None
_protection_futures = {}
_protection_executor = None
_protection_clients = {}
_disabled_mock_maintenance_at = {}
_mcp_spot_maintenance_at = {}
_execution_sync_futures = {}
_execution_sync_executor = None
_execution_sync_last = {}
_exclusive_maintenance_retry = {}
_maintenance_run_locks = {}
_plan_scan_cursor = 0
_recent_plan_scan_cursor = (0, 0)
_protection_scan_progress = {}
_background_receipt_future = None
_background_receipt_cursor = None
_dispatch_cursor = 0

AGENT_JOB_TYPE = "agent"
MAINTENANCE_JOB_TYPE = "maintenance"


def _scheduler_max_workers() -> int:
    configured = positive_setting('SCHEDULER_MAX_WORKERS', get_capacity_manager().role_limits['agent'])
    return configured


def _maintenance_max_workers() -> int:
    return positive_setting('SCHEDULER_MAINTENANCE_WORKERS', get_capacity_manager().role_limits['maintenance'])


def _get_agent_executor():
    global _agent_executor
    with _scheduler_lock:
        if _agent_executor is None:
            _agent_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=_scheduler_max_workers(),
                thread_name_prefix="agent-job",
            )
        return _agent_executor


def _get_maintenance_executor():
    global _maintenance_executor
    with _scheduler_lock:
        if _maintenance_executor is None:
            _maintenance_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=_maintenance_max_workers(),
                thread_name_prefix="scheduler-maintenance",
            )
        return _maintenance_executor


def _timestamp() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


def _scheduled_at(now: datetime) -> str:
    return now.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:00")


def _insert_scheduler_run(config_id: str, job_type: str, scheduled_at: str, status: str = "QUEUED", error: str | None = None) -> bool:
    from backend.database import get_db_conn

    now_str = _timestamp()
    try:
        with get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO scheduler_runs (
                    config_id, job_type, scheduled_at, status, started_at,
                    finished_at, error, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(config_id),
                    str(job_type),
                    str(scheduled_at),
                    str(status),
                    now_str if status == "RUNNING" else None,
                    now_str if status.startswith("SKIPPED") else None,
                    error,
                    now_str,
                    now_str,
                ),
            )
            conn.commit()
        return True
    except storage.IntegrityError:
        return False


def _mark_scheduler_run(config_id: str, job_type: str, scheduled_at: str, status: str, error: str | None = None) -> None:
    from backend.database import get_db_conn

    now_str = _timestamp()
    started_at_sql = ", started_at = COALESCE(started_at, %s)" if status == "RUNNING" else ""
    finished_at_sql = ", finished_at = %s" if status in {"FINISHED", "FAILED", "SKIPPED_RUNNING", "SKIPPED_CAPACITY"} else ""
    params = [status]
    if status == "RUNNING":
        params.append(now_str)
    if status in {"FINISHED", "FAILED", "SKIPPED_RUNNING", "SKIPPED_CAPACITY"}:
        params.append(now_str)
    params.extend([error, now_str, str(config_id), str(job_type), str(scheduled_at)])

    with get_db_conn() as conn:
        conn.execute(
            f"""
            UPDATE scheduler_runs
            SET status = %s{started_at_sql}{finished_at_sql},
                error = %s,
                updated_at = %s
            WHERE config_id = %s AND job_type = %s AND scheduled_at = %s
            """,
            tuple(params),
        )
        conn.commit()


def _mark_scheduler_progress(config_id: str, scheduled_at: str, event: dict) -> None:
    """Persist model/tool progress so the dashboard can follow an active task."""
    from backend.database import get_db_conn

    phase = str(event.get("phase") or "working")
    message = str(event.get("message") or "")
    reasoning = event.get("reasoning_content")
    reasoning_tokens = event.get("reasoning_tokens")
    tool_calls = event.get("tool_calls")
    assignments = ["phase = %s", "progress_message = %s", "updated_at = %s"]
    params = [phase, message, _timestamp()]
    if reasoning is not None:
        assignments.append("reasoning_content = %s")
        params.append(str(reasoning or ""))
    if reasoning_tokens is not None:
        assignments.append("reasoning_tokens = %s")
        params.append(max(int(reasoning_tokens or 0), 0))
    if tool_calls is not None:
        assignments.append("tool_calls_json = %s")
        params.append(json.dumps(tool_calls, ensure_ascii=False, default=str))
    if event.get('reasoning_diagnostics') is not None:
        assignments.append('reasoning_diagnostics = %s')
        params.append(json.dumps(event['reasoning_diagnostics'], ensure_ascii=False))
    if event.get('decision') is not None:
        assignments.append('decision_json = %s')
        params.append(json.dumps(event['decision'], ensure_ascii=False, default=str))
    params.extend([str(config_id), AGENT_JOB_TYPE, str(scheduled_at)])

    with get_db_conn() as conn:
        conn.execute(
            f"""
            UPDATE scheduler_runs
            SET {', '.join(assignments)}
            WHERE config_id = %s AND job_type = %s AND scheduled_at = %s
            """,
            tuple(params),
        )
        conn.commit()


def check_dca_executed(config_id, now, freq="1d"):
    from backend.database import get_db_conn

    try:
        normalized_freq = normalize_dca_freq(freq)
        if normalized_freq == "1w":
            start_of_week = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
            query_time = f"{start_of_week} 00:00:00"
        else:
            query_time = f"{now.strftime('%Y-%m-%d')} 00:00:00"

        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute(
                """
                SELECT count(*) as cnt
                FROM orders
                WHERE config_id = %s
                  AND trade_mode = 'SPOT_DCA'
                  AND timestamp >= %s
                """,
                (config_id, query_time),
            )
            row = c.fetchone()
            return row[0] > 0
    except Exception as e:
        logger.error(f"Failed to check DCA execution state: {e}")
        return False


def dca_was_dispatched(config_id, now, freq=None, config=None):
    cfg = config or global_config.get_config_by_id(config_id) or {'dca_freq': freq or '1d'}
    slot = latest_dca_slot(cfg, now)
    if not slot:
        return False
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        return bool(conn.execute('SELECT 1 FROM scheduler_runs WHERE config_id=%s AND job_type=%s AND scheduled_at>=%s AND scheduled_at<=%s',
                                (config_id, AGENT_JOB_TYPE, _scheduled_at(slot.astimezone(TZ_CN)), _scheduled_at(now.astimezone(TZ_CN)))).fetchone())


def is_time_to_run(config, now):
    mode = config.get("mode", "STRATEGY").upper()
    config_id = config.get("config_id")

    if mode == "SPOT_DCA":
        slot = latest_dca_slot(config, now)
        if slot is None:
            return False
        # Includes old releases that persisted the actual catch-up dispatch minute.
        return not dca_was_dispatched(config_id, now, config=config)
    from backend.utils.run_schedule import schedule_due
    if not schedule_due(config, now):
        return False
    last_run = _last_run_times.get(config_id)
    return not (last_run and last_run.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0))


def _get_current_price(mt, symbol: str) -> float:
    try:
        ticker = mt.exchange.fetch_ticker(symbol)
        return float(ticker.get("last") or 0)
    except Exception as exc:
        logger.warning(f"[Scheduler] fetch_ticker failed for {symbol}: {exc}")
        return 0.0


def _snapshot_strategy_equity(config, mt):
    """给 STRATEGY 模式配置写一条权益快照。

    - 钱包余额来自 mock_accounts
    - 未实现盈亏 = sum((current_price - entry) * amount * dir) 针对已入场仓位
    - total_equity = balance + unrealized_pnl
    未成交挂单不计未实现盈亏。
    """
    from backend.database import (
        get_filled_mock_positions,
        get_mock_account,
        save_mock_equity_snapshot,
    )

    config_id = config.get("config_id")
    symbol = config.get("symbol")
    if not config_id or not symbol:
        return

    try:
        acc = get_mock_account(config_id, symbol)
        balance = float(acc.get("balance") or 0)

        filled = get_filled_mock_positions(config_id, symbol)
        unrealized = 0.0
        current_price = 0.0
        if filled:
            current_price = _get_current_price(mt, symbol)
            if current_price > 0:
                for pos in filled:
                    side = str(pos.get("side") or "").upper()
                    entry = float(pos.get("price") or 0)
                    amount = float(pos.get("amount") or 0)
                    direction = 1 if "BUY" in side else -1
                    unrealized += (current_price - entry) * amount * direction

        save_mock_equity_snapshot(config_id, symbol, balance, unrealized)
        logger.debug(
            f"[EquitySnapshot] STRATEGY {config_id} {symbol}: "
            f"balance={balance:.4f} unrealized={unrealized:.4f} "
            f"positions={len(filled)}"
        )
    except Exception as exc:
        logger.warning(f"[EquitySnapshot] STRATEGY snapshot failed {config_id}: {exc}")


def _snapshot_real_equity(config, mt):
    """给 REAL 模式配置写一条实盘权益快照（带 config_id）。"""
    from backend.database import save_balance_snapshot

    config_id = config.get("config_id")
    symbol = config.get("symbol")
    if not config_id or not symbol:
        return

    try:
        status = mt.get_account_status(symbol, is_real=True, config_id=config_id)
        balance = float(status.get("balance") or 0)
        positions = status.get("real_positions") or []
        unrealized = sum(float(p.get("unrealized_pnl") or 0) for p in positions)
        if balance > 0:
            save_balance_snapshot(symbol, balance, unrealized, config_id=config_id)
            logger.debug(
                f"[EquitySnapshot] REAL {config_id} {symbol}: "
                f"balance={balance:.4f} unrealized={unrealized:.4f}"
            )
    except Exception as exc:
        logger.warning(f"[EquitySnapshot] REAL snapshot failed {config_id}: {exc}")


def _run_exclusive_maintenance(manager, config):
    """A failed book/receipt must not starve another symbol or recovery phase."""
    from backend.utils.position_management import ensure_managed, cleanup_previous_orders, recover_reservations
    if config.get('position_management_policy') == 'visible_first_claim':
        from backend.utils.position_management import discover_management_symbols, ensure_visible_management
        symbols = discover_management_symbols(manager.mt, config)
    else:
        symbols = list(dict.fromkeys(scope['symbol'] for scope in config.get('management_scopes') or []))
    for symbol in symbols:
        for phase, callback in (('recovery', recover_reservations), ('discovery', ensure_managed),
                                ('cleanup', cleanup_previous_orders)):
            key = (config['config_id'], symbol, phase)
            prior = _exclusive_maintenance_retry.get(key, {})
            if time.monotonic() < prior.get('next_at', 0):
                continue
            try:
                if phase == 'discovery' and config.get('position_management_policy') == 'visible_first_claim':
                    ensure_visible_management(manager.mt, symbol, discover=True)
                elif phase == 'recovery' and config.get('position_management_policy') == 'visible_first_claim':
                    from backend.utils.position_management import reconcile_visible_actions
                    reconcile_visible_actions(manager.config_id)
                    callback(manager, symbol)
                else:
                    callback(manager, symbol)
            except Exception as exc:
                failures = min(6, prior.get('failures', 0) + 1)
                _exclusive_maintenance_retry[key] = {'failures': failures,
                                                   'next_at': time.monotonic() + min(60, 30 * 2 ** (failures - 1))}
                # Signed URLs or exchange headers can occur in exception text.
                logger.warning('[PositionManagement] owner=%s symbol=%s phase=%s result=%s',
                               config['config_id'], symbol, phase, type(exc).__name__)
            else:
                _exclusive_maintenance_retry[key] = {'failures': 0, 'next_at': time.monotonic() + 30}


def run_config_maintenance(config):
    # The minute scheduler and protection monitor have different future maps.
    # Merge their work before accessing a cached mutable CCXT client.
    with _scheduler_lock:
        lock = _maintenance_run_locks.setdefault(config.get('config_id', 'unknown'), threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        return _run_config_maintenance(config)
    finally:
        lock.release()


def _run_config_maintenance(config):
    config_id = config.get("config_id", "unknown")
    symbol = config.get("symbol")
    mode = config.get("mode", "STRATEGY").upper()

    if not symbol and config.get('position_management_policy') != 'visible_first_claim':
        return

    if mode == "STRATEGY":
        mt = None
        try:
            mt = MarketTool(config_id=config_id)
            detect = mt.run_silent_sl_tp() or {}
            if detect.get("error"):
                logger.warning(
                    f"[SilentMonitor] {config_id} 检测异常: {detect.get('error')}"
                )
            elif detect.get("success") and detect.get("checked", 0) == 0:
                logger.debug(
                    f"[SilentMonitor] {config_id} {symbol}: 本次无可检测挂单"
                )
        except Exception as exc:
            logger.warning(f"[SilentMonitor] {config_id} 初始化/检测失败: {exc}")

        # 每分钟记录一条策略权益快照
        if mt is not None:
            _snapshot_strategy_equity(config, mt)

    elif mode == "REAL":
        from backend.utils.position_protection import PositionProtection
        try:
            import hashlib
            fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()
            cached = _protection_clients.get(config_id)
            if not cached or cached[0] != fingerprint or time.monotonic() - cached[1] > 300:
                if cached:
                    cached[2].mt.close()
                cached = (fingerprint, time.monotonic(), PositionProtection(MarketTool(config_id=config_id)))
                _protection_clients[config_id] = cached
            if config.get('position_management_policy') in {'exclusive_scope', 'visible_first_claim'}:
                from backend.utils.independent_exits import IndependentExits
                manager = IndependentExits(cached[2].mt)
                _run_exclusive_maintenance(manager, config)
            page = cached[2].reconcile_plan_page(
                limit=positive_setting('RUNTIME_SCAN_BATCH_SIZE', 32),
                after_cursor=_protection_scan_progress.get(config_id, 0), respect_backoff=True)
            _protection_scan_progress[config_id] = page['next_cursor']
            results = page['results']
            from backend.utils.trade_operations import reconcile_pending_trade_operations
            reconcile_pending_trade_operations(config_id)
            for result in results:
                if result.get("error"):
                    logger.error(f"[PositionProtection] {config_id}: {result}")
        except Exception as exc:
            logger.error(f"[PositionProtection] {config_id} reconciliation failed: {exc}")

    elif mode == "SPOT_DCA":
        try:
            from backend.utils.position_management import management_status
            if config.get('position_management_policy') == 'visible_first_claim' and (config.get('enabled', True) or management_status(config_id)['scopes']):
                from backend.utils.position_management import discover_management_symbols, ensure_visible_management
                stock_tool = MarketTool(config_id=config_id)
                for stock_symbol in discover_management_symbols(stock_tool, config):
                    ensure_visible_management(stock_tool, stock_symbol, discover=True)
            from backend.app.services.dashboard_service import calculate_dca_stats

            if config.get('mcp_profile'):
                from backend.mcp.spot_orders import reconcile
                reconcile(config_id)

            if config.get('mcp_symbol_scope') == 'all':
                from backend.mcp.settings import runtime_symbols
                from backend.utils.spot_portfolio import MAX_SPOT_SYMBOLS
                by_quote = {}
                for owned_symbol in config.get('mcp_owned_symbols') or []:
                    by_quote.setdefault(owned_symbol.split('/')[1], []).append(owned_symbol)
                for symbols in by_quote.values():
                    for start in range(0, len(symbols), MAX_SPOT_SYMBOLS):
                        with runtime_symbols(config_id, symbols[start:start + MAX_SPOT_SYMBOLS]):
                            calculate_dca_stats(config_id)
            else:
                calculate_dca_stats(config_id)
        except Exception as exc:
            logger.warning(f"[DCA Sync] {config_id} order/stat sync failed: {exc}")


def run_config_agent(config, scheduled_at: str):
    config_id = config.get("config_id", "unknown")
    mode = config.get("mode", "STRATEGY").upper()
    logger.info(
        f"[{config_id}] started scheduled agent job ({mode}, scheduled_at={scheduled_at}, interval={config.get('run_interval', 'default')})"
    )
    _mark_scheduler_run(config_id, AGENT_JOB_TYPE, scheduled_at, "RUNNING")
    _mark_scheduler_progress(
        config_id,
        scheduled_at,
        {"phase": "preparing", "message": "正在准备市场、账户与历史上下文"},
    )
    try:
        from backend.app.services.news_triggers import trigger_message
        run_agent_for_config(
            config,
            **({'human_message': trigger_message(config)} if config.get('_news_event') else {}),
            progress_callback=lambda event: _mark_scheduler_progress(config_id, scheduled_at, event),
            run_id=dca_cycle_id(config_id, TZ_CN.localize(datetime.fromisoformat(scheduled_at))),
        )
        from backend.agent.memory_updates import tick_memory_updates
        tick_memory_updates()
        _mark_scheduler_progress(
            config_id,
            scheduled_at,
            {"phase": "completed", "message": "任务执行完成"},
        )
        _mark_scheduler_run(config_id, AGENT_JOB_TYPE, scheduled_at, "FINISHED")
        logger.info(f"[{config_id}] finished scheduled agent job (scheduled_at={scheduled_at})")
    except Exception as exc:
        _mark_scheduler_progress(
            config_id,
            scheduled_at,
            {"phase": "failed", "message": str(exc)},
        )
        _mark_scheduler_run(config_id, AGENT_JOB_TYPE, scheduled_at, "FAILED", error=str(exc))
        logger.error(f"Error executing agent [{config_id}] scheduled_at={scheduled_at}: {exc}")


def _drop_finished_futures(running_map: dict[str, concurrent.futures.Future], label: str) -> None:
    with _scheduler_lock:
        finished_keys = [key for key, future in running_map.items() if future.done()]
        for key in finished_keys:
            future = running_map.pop(key)
            try:
                future.result()
            except Exception as exc:
                logger.error(f"[{key}] {label} worker failed: {exc}")


def _submit_maintenance(config) -> bool:
    config_id = str(config.get("config_id") or "unknown")
    with _scheduler_lock:
        existing = _running_maintenance_futures.get(config_id)
        if existing and not existing.done():
            logger.debug(f"[{config_id}] maintenance skip_due_to_running")
            return False
        try:
            future = submit_capacity(_get_maintenance_executor(), 'maintenance', run_config_maintenance,
                                     config, critical=True)
        except CapacityBusy:
            return False
        _running_maintenance_futures[config_id] = future
    logger.debug(f"[{config_id}] maintenance queued")
    return True


def _submit_execution_sync(config):
    """Separate worker pool: history catch-up must never delay protective exits."""
    global _execution_sync_executor
    if str(config.get('mode') or '').upper() != 'REAL':
        return
    cid = config['config_id']
    with _scheduler_lock:
        current = _execution_sync_futures.get(cid)
        if current and not current.done():
            return
        if time.monotonic() - _execution_sync_last.get(cid, 0) < 30:
            return
        _execution_sync_last[cid] = time.monotonic()
        if _execution_sync_executor is None:
            _execution_sync_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='execution-sync')
        try:
            _execution_sync_futures[cid] = submit_capacity(
                _execution_sync_executor, 'recovery', _sync_execution, dict(config), critical=True)
        except CapacityBusy:
            return


def _sync_execution(config):
    from backend.utils.execution_ledger import ExecutionLedger
    try:
        mt = MarketTool(config_id=config['config_id'])
        from backend.utils.execution_stream import ensure_stream
        from backend.utils.execution_metrics import update_post_exit
        symbols = config.get('mcp_owned_symbols') if config.get('mcp_symbol_scope') == 'all' else [config['symbol']]
        for symbol in symbols or []:
            try:
                ensure_stream(mt, symbol=symbol)
                ledger = ExecutionLedger(mt.exchange, config['config_id'], symbol)
                ledger.index_saved_plans()
                result = ledger.sync()
                if ledger.index_state.get('complete'):
                    from backend.utils.execution_history_work import drain_scope_history
                    drain_scope_history(ledger.scope, ledger.symbol)
                update_post_exit(mt.exchange, ledger.scope, ledger.symbol)
                if result.get('error'):
                    logger.warning(f"[ExecutionSync] {config['config_id']} {symbol}: {result['error']}")
            except Exception as exc:
                logger.warning(f"[ExecutionSync] {config['config_id']} {symbol}: {exc}")
    except Exception as exc:
        logger.warning(f"[ExecutionSync] {config['config_id']}: {exc}")


def _submit_agent(config, scheduled_at: str) -> bool:
    config_id = str(config.get("config_id") or "unknown")
    mode = str(config.get("mode", "STRATEGY")).upper()

    with _scheduler_lock:
        existing = _running_agent_futures.get(config_id)
        if existing and not existing.done():
            inserted = _insert_scheduler_run(
                config_id,
                AGENT_JOB_TYPE,
                scheduled_at,
                status="SKIPPED_RUNNING",
                error="previous job still running",
            )
            if inserted:
                logger.warning(f"[{config_id}] skip_due_to_running scheduled_at={scheduled_at}")
            else:
                logger.info(f"[{config_id}] duplicate skipped scheduled_at={scheduled_at}")
            return False

    if not _insert_scheduler_run(config_id, AGENT_JOB_TYPE, scheduled_at, status="QUEUED"):
        logger.info(f"[{config_id}] duplicate skipped scheduled_at={scheduled_at}")
        return False

    logger.info(
        f"[{config_id}] queued scheduled agent job ({mode}, scheduled_at={scheduled_at}, interval={config.get('run_interval', 'default')})"
    )
    try:
        future = submit_capacity(_get_agent_executor(), 'agent', run_config_agent, config, scheduled_at)
    except CapacityBusy as exc:
        _mark_scheduler_run(config_id, AGENT_JOB_TYPE, scheduled_at, 'SKIPPED_CAPACITY', error=exc.reason)
        return False
    except Exception as exc:
        _mark_scheduler_run(config_id, AGENT_JOB_TYPE, scheduled_at, "FAILED", error=str(exc))
        logger.error(f"[{config_id}] failed to submit scheduled agent job scheduled_at={scheduled_at}: {exc}")
        return False

    with _scheduler_lock:
        _running_agent_futures[config_id] = future
    return True


def wait_until_next_minute():
    now = datetime.now().astimezone(TZ_CN)
    sleep_seconds = 60 - now.second - (now.microsecond / 1000000.0)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


def job(now: datetime | None = None):
    global _last_heartbeat_key, _dispatch_cursor
    now = now or datetime.now(TZ_CN)
    scheduled_at = _scheduled_at(now)

    global_config.reload_config()
    sync_langsmith_environment()
    configs = global_config.get_all_symbol_configs()
    active_configs = [c for c in configs if c.get("enabled", True)]
    if active_configs:
        index = _dispatch_cursor % len(active_configs)
        active_configs = active_configs[index:] + active_configs[:index]
        _dispatch_cursor += 1

    _drop_finished_futures(_running_agent_futures, "agent")
    _drop_finished_futures(_running_maintenance_futures, "maintenance")
    _drop_finished_futures(_execution_sync_futures, 'execution-sync')

    if not active_configs:
        return {"active": 0, "maintenance_queued": 0, "agent_queued": 0, "agent_due": 0}

    maintenance_queued = 0
    agent_due = 0
    agent_queued = 0
    regularly_due = set()

    for config in active_configs:
        _submit_execution_sync(config)
        if _submit_maintenance(config):
            maintenance_queued += 1

        if is_time_to_run(config, now):
            regularly_due.add(config['config_id'])
            config_id = config.get("config_id", "unknown")
            mode = str(config.get("mode", "STRATEGY")).upper()
            agent_due += 1
            logger.info(
                f"[{config_id}] due detected ({mode}, scheduled_at={scheduled_at}, interval={config.get('run_interval', 'default')})"
            )
            _last_run_times[config_id] = now
            slot = latest_dca_slot(config, now) if mode == 'SPOT_DCA' else None
            dispatch_at = _scheduled_at(slot.astimezone(TZ_CN)) if slot else scheduled_at
            if _submit_agent(config, dispatch_at):
                agent_queued += 1

    try:
        from backend.app.services.news_triggers import dispatch_news
        agent_queued += dispatch_news(active_configs, lambda config: _submit_agent(config, scheduled_at), now=now, regularly_due=regularly_due)
    except Exception as exc:
        logger.warning('News dispatch unavailable: %s', type(exc).__name__)

    heartbeat_key = now.strftime("%Y-%m-%d %H:%M")
    if now.minute % 10 == 0 and heartbeat_key != _last_heartbeat_key:
        _last_heartbeat_key = heartbeat_key
        with _scheduler_lock:
            active_agent_jobs = sum(1 for future in _running_agent_futures.values() if not future.done())
            active_maintenance_jobs = sum(1 for future in _running_maintenance_futures.values() if not future.done())
        logger.info(
            "[SchedulerHeartbeat] alive "
            f"active_configs={len(active_configs)} active_agent_jobs={active_agent_jobs} "
            f"active_maintenance_jobs={active_maintenance_jobs} due={agent_due} queued={agent_queued}"
        )

    return {
        "active": len(active_configs),
        "maintenance_queued": maintenance_queued,
        "agent_due": agent_due,
        "agent_queued": agent_queued,
    }


def tick_shared_services():
    """Each service owns its nonblocking cadence and durable retry state."""
    for module_name, function_name in (
        ('backend.app.services.news_service', 'tick_news_pipeline'),
        ('backend.app.services.pricing_service', 'tick_price_sync'),
        ('backend.agent.memory_updates', 'tick_memory_updates'),
    ):
        try:
            import importlib
            getattr(importlib.import_module(module_name), function_name)()
        except Exception as exc:
            logger.warning('Background service %s failed: %s', function_name, exc)


def run_scheduler_forever():
    from backend.utils.deployment_mode import maintenance_enabled
    if maintenance_enabled():
        raise RuntimeError('Scheduler is disabled during deployment maintenance')
    from backend.utils.resource_diagnostics import start_resource_collector
    start_resource_collector('scheduler')
    logger.info(f"[System] scheduler loop started (max_workers={_scheduler_max_workers()})")

    try:
        init_db()
        logger.info("Database initialized for scheduler")
    except Exception as e:
        logger.error("Database initialization failed in scheduler")
        raise

    global_config.reload_config()
    _start_protection_monitor()

    while True:
        try:
            wait_until_next_minute()

            # Another process can re-enable this scheduler while it is paused.
            if datetime.now().minute % 5 == 0:
                global_config.reload_config()

            tick_shared_services()

            if not global_config.enable_scheduler:
                if datetime.now().minute % 10 == 0:
                    logger.info("Scheduler disabled globally, waiting...")
                continue

            job()

        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
            time.sleep(10)


def scheduler_should_run() -> bool:
    return bool(getattr(global_config, "enable_scheduler", True))


def protection_tick():
    """Only persisted live plans need exchange polling; disabled agents still get protection."""
    global _protection_executor, _plan_scan_cursor, _recent_plan_scan_cursor
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        page_size = positive_setting('RUNTIME_SCAN_BATCH_SIZE', 32)
        from backend.utils.protection_scan import active_plan_page, recent_completed_page
        references = active_plan_page(conn, after_cursor=_plan_scan_cursor, limit=page_size)
        conn.commit()
        _plan_scan_cursor = references[-1]['rowid'] if len(references) == page_size else 0
        rows = references
        completed = recent_completed_page(conn, since=time.time() - 86400,
                                          after_cursor=_recent_plan_scan_cursor, limit=page_size)
        _recent_plan_scan_cursor = (completed[-1]['updated_at'], completed[-1]['plan_rowid']) if len(completed) == page_size else (0, 0)
        mock_ids = {r[0] for r in conn.execute("SELECT DISTINCT config_id FROM mock_orders WHERE status='OPEN'")}
        tables = storage.table_names(conn)
        if 'mock_positions' in tables:
            mock_ids.update(r[0] for r in conn.execute('SELECT DISTINCT config_id FROM mock_positions WHERE quantity>0'))
        if 'mock_exit_orders' in tables:
            mock_ids.update(r[0] for r in conn.execute(
                "SELECT DISTINCT config_id FROM mock_exit_orders "
                "WHERE status IN ('OPEN','WAITING_TRIGGER','PENDING','UNKNOWN')"))
    ids = {row['config_id'] for row in rows if row['state'] != 'DONE'}
    from backend.mcp.settings import maintenance_profiles
    try:
        external_configs = maintenance_profiles()
    except Exception as exc:
        logger.error('Cannot load external maintenance profiles: %s', exc)
        external_configs = []
    # MCP profiles never enter the decision scheduler, but their owned spot
    # orders still need reconciliation after submission or while paused.
    for cfg in external_configs:
        config_id = cfg['config_id']
        if (cfg.get('mode') == 'SPOT_DCA'
                and time.monotonic() - _mcp_spot_maintenance_at.get(config_id, -60) >= 60
                and _submit_maintenance(cfg)):
            _mcp_spot_maintenance_at[config_id] = time.monotonic()
    configs = {cfg['config_id']: cfg for cfg in [*global_config.get_all_symbol_configs(), *external_configs] if cfg}
    from backend.utils.position_management import management_status
    ids.update(cid for cid, cfg in configs.items()
               if cfg.get('mode', '').upper() == 'REAL'
               and (cfg.get('position_management_policy') == 'exclusive_scope'
                    or cfg.get('position_management_policy') == 'visible_first_claim'
                    and (cfg.get('enabled', True) or management_status(cid)['scopes'])))
    # Data accounting continues for recently active plans even when decision making
    # is disabled, including fills that arrive after protective cleanup.
    recent = {row['config_id'] for row in completed}
    recent.update(row['config_id'] for row in rows if (row['updated_at'] or 0) > time.time() - 86400)
    for cfg in configs.values():
        if cfg.get('mode', '').upper() == 'REAL' and (cfg.get('enabled', True) or cfg['config_id'] in ids | recent):
            _submit_execution_sync(cfg)
    from backend.utils.execution_stream import stop_unused_streams
    stop_unused_streams()
    if _protection_executor is None:
        _protection_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='position-protection')
    _drop_finished_futures(_protection_futures, 'protection')
    for config_id in ids:
        if config_id in _protection_futures:
            continue
        cfg = configs.get(config_id)
        if not cfg or cfg.get('mode', '').upper() != 'REAL':
            logger.error(f'[PositionProtection] {config_id}: live plan has no REAL exchange configuration; manual attention required')
            continue
        try:
            _protection_futures[config_id] = submit_capacity(
                _protection_executor, 'maintenance', run_config_maintenance, cfg, critical=True)
        except CapacityBusy:
            continue
    # Decision scheduling may be disabled while a simulated position still needs
    # entry/exit reconciliation. Reuse the same in-flight guard as live plans.
    for config_id in mock_ids:
        if config_id in _protection_futures:
            continue
        cfg = configs.get(config_id)
        if (cfg and cfg.get('mode', '').upper() == 'STRATEGY' and not cfg.get('enabled', True)
                and time.monotonic() - _disabled_mock_maintenance_at.get(config_id, -60) >= 60):
            _disabled_mock_maintenance_at[config_id] = time.monotonic()
            try:
                _protection_futures[config_id] = submit_capacity(
                    _protection_executor, 'maintenance', run_config_maintenance, cfg, critical=True)
            except CapacityBusy:
                continue
    _retire_scheduler_clients(configs)
    _tick_background_receipts()


def _retire_scheduler_clients(configs):
    from backend.mcp.exchange_runtime import retire_runtime_caches
    for cid, cached in list(_protection_clients.items()):
        if cid not in configs:
            lock = _maintenance_run_locks.setdefault(cid, threading.Lock())
            if lock.acquire(blocking=False):
                try:
                    if cached[2].mt.close():
                        _protection_clients.pop(cid, None)
                finally:
                    lock.release()
    retire_runtime_caches()
    for mapping in (_maintenance_run_locks, _protection_scan_progress, _execution_sync_last,
                    _disabled_mock_maintenance_at, _mcp_spot_maintenance_at, _last_run_times):
        for cid in set(mapping) - set(configs):
            lock = _maintenance_run_locks.get(cid)
            if cid not in _protection_clients and (not lock or not lock.locked()):
                mapping.pop(cid, None)
    _drop_finished_futures(_execution_sync_futures, 'execution-sync')
    for key in list(_exclusive_maintenance_retry):
        if key[0] not in configs:
            _exclusive_maintenance_retry.pop(key, None)


def _tick_background_receipts():
    global _background_receipt_future, _background_receipt_cursor
    if _background_receipt_future is not None:
        if not _background_receipt_future.done():
            return
        try:
            _background_receipt_cursor = _background_receipt_future.result().get('next_cursor')
        except Exception as exc:
            logger.warning('Background receipt verification failed: %s', type(exc).__name__)
    from backend.utils import trade_operations
    callback = getattr(trade_operations, 'reconcile_background_receipts', None)
    if callback is None:
        return
    try:
        _background_receipt_future = submit_capacity(
            _get_maintenance_executor(), 'recovery', callback, critical=True,
            limit=positive_setting('RUNTIME_SCAN_BATCH_SIZE', 32), after_cursor=_background_receipt_cursor)
    except CapacityBusy:
        _background_receipt_future = None


def _start_protection_monitor():
    global _protection_thread
    if _protection_thread and _protection_thread.is_alive():
        return
    def monitor():
        while True:
            try:
                protection_tick()
            except Exception as exc:
                logger.error(f'[PositionProtection] monitor failed: {exc}')
            time.sleep(5)
    _protection_thread = threading.Thread(target=monitor, name='protection-monitor', daemon=True)
    _protection_thread.start()


if __name__ == "__main__":
    from backend.storage import close_pool
    from backend.storage.checkpoints import close_checkpoints
    try:
        run_scheduler_forever()
    finally:
        close_checkpoints()
        close_pool()
