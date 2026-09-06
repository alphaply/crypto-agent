import concurrent.futures
import json
import os
import sqlite3
import time
import threading
from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv

from backend.agent.agent_graph import (
    generate_short_memory_for_config,
    generate_manual_daily_summary,
    get_short_memory_bucket,
    is_invalid_daily_summary,
    run_agent_for_config,
)
from backend.config import config as global_config
from backend.database import get_short_memory, init_db
from backend.utils.logger import setup_logger
from backend.utils.llm_utils import sync_langsmith_environment
from backend.utils.market_data import MarketTool


load_dotenv()

TZ_CN = pytz.timezone(getattr(global_config, "timezone", "Asia/Shanghai"))
logger = setup_logger("MainScheduler")

_last_run_times = {}
_daily_summary_done_date = None
_daily_summary_last_attempt_at = None
_short_memory_done_buckets = set()
_short_memory_last_attempts = {}
_agent_executor = None
_maintenance_executor = None
_running_agent_futures = {}
_running_maintenance_futures = {}
_daily_summary_future = None
_short_memory_future = None
_scheduler_lock = threading.RLock()
_last_heartbeat_key = None
_protection_thread = None
_protection_futures = {}
_protection_executor = None
_protection_clients = {}

AGENT_JOB_TYPE = "agent"
MAINTENANCE_JOB_TYPE = "maintenance"
DAILY_SUMMARY_DEFAULT_TIME = "00:05"
DAILY_SUMMARY_DEFAULT_RETRY_MINUTES = 15
SHORT_MEMORY_DEFAULT_RETRY_MINUTES = 15


def _scheduler_max_workers() -> int:
    raw_value = str(os.getenv("SCHEDULER_MAX_WORKERS", "")).strip()
    if raw_value:
        try:
            return max(1, int(raw_value))
        except ValueError:
            logger.warning(f"Invalid SCHEDULER_MAX_WORKERS={raw_value!r}, falling back to CPU-based default")

    cpu_count = os.cpu_count() or 1
    return max(1, min(5, cpu_count))


def _maintenance_max_workers() -> int:
    raw_value = str(os.getenv("SCHEDULER_MAINTENANCE_WORKERS", "2")).strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        logger.warning(f"Invalid SCHEDULER_MAINTENANCE_WORKERS={raw_value!r}, falling back to 2")
        return 2


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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    except sqlite3.IntegrityError:
        return False


def _mark_scheduler_run(config_id: str, job_type: str, scheduled_at: str, status: str, error: str | None = None) -> None:
    from backend.database import get_db_conn

    now_str = _timestamp()
    started_at_sql = ", started_at = COALESCE(started_at, ?)" if status == "RUNNING" else ""
    finished_at_sql = ", finished_at = ?" if status in {"FINISHED", "FAILED", "SKIPPED_RUNNING"} else ""
    params = [status]
    if status == "RUNNING":
        params.append(now_str)
    if status in {"FINISHED", "FAILED", "SKIPPED_RUNNING"}:
        params.append(now_str)
    params.extend([error, now_str, str(config_id), str(job_type), str(scheduled_at)])

    with get_db_conn() as conn:
        conn.execute(
            f"""
            UPDATE scheduler_runs
            SET status = ?{started_at_sql}{finished_at_sql},
                error = ?,
                updated_at = ?
            WHERE config_id = ? AND job_type = ? AND scheduled_at = ?
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
    assignments = ["phase = ?", "progress_message = ?", "updated_at = ?"]
    params = [phase, message, _timestamp()]
    if reasoning is not None:
        assignments.append("reasoning_content = ?")
        params.append(str(reasoning or ""))
    if reasoning_tokens is not None:
        assignments.append("reasoning_tokens = ?")
        params.append(max(int(reasoning_tokens or 0), 0))
    if tool_calls is not None:
        assignments.append("tool_calls_json = ?")
        params.append(json.dumps(tool_calls, ensure_ascii=False, default=str))
    params.extend([str(config_id), AGENT_JOB_TYPE, str(scheduled_at)])

    with get_db_conn() as conn:
        conn.execute(
            f"""
            UPDATE scheduler_runs
            SET {', '.join(assignments)}
            WHERE config_id = ? AND job_type = ? AND scheduled_at = ?
            """,
            tuple(params),
        )
        conn.commit()


def normalize_dca_freq(raw_freq):
    freq = str(raw_freq or "1d").strip().lower()
    if freq in {"1w", "weekly", "week", "w"}:
        return "1w"
    return "1d"


def parse_dca_time(raw_time):
    text = str(raw_time or "08:00").strip()
    if not text:
        return 8, 0

    try:
        parts = text.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return 8, 0

    hour = min(max(hour, 0), 23)
    minute = min(max(minute, 0), 59)
    return hour, minute


def parse_daily_summary_time(raw_time: str | None = None) -> tuple[int, int]:
    text = str(raw_time or os.getenv("DAILY_SUMMARY_TIME", DAILY_SUMMARY_DEFAULT_TIME)).strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        return hour, minute
    except (TypeError, ValueError):
        logger.warning(
            f"Invalid DAILY_SUMMARY_TIME={text!r}; using {DAILY_SUMMARY_DEFAULT_TIME}"
        )
        return 0, 5


def _daily_summary_retry_minutes() -> int:
    raw_value = str(
        os.getenv("DAILY_SUMMARY_RETRY_MINUTES", DAILY_SUMMARY_DEFAULT_RETRY_MINUTES)
    ).strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        logger.warning(
            "Invalid DAILY_SUMMARY_RETRY_MINUTES="
            f"{raw_value!r}; using {DAILY_SUMMARY_DEFAULT_RETRY_MINUTES}"
        )
        return DAILY_SUMMARY_DEFAULT_RETRY_MINUTES


def _short_memory_retry_minutes() -> int:
    raw_value = str(
        os.getenv("SHORT_MEMORY_RETRY_MINUTES", SHORT_MEMORY_DEFAULT_RETRY_MINUTES)
    ).strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        logger.warning(
            "Invalid SHORT_MEMORY_RETRY_MINUTES="
            f"{raw_value!r}; using {SHORT_MEMORY_DEFAULT_RETRY_MINUTES}"
        )
        return SHORT_MEMORY_DEFAULT_RETRY_MINUTES


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
                WHERE config_id = ?
                  AND trade_mode = 'SPOT_DCA'
                  AND timestamp >= ?
                """,
                (config_id, query_time),
            )
            row = c.fetchone()
            return row[0] > 0
    except Exception as e:
        logger.error(f"Failed to check DCA execution state: {e}")
        return False


def is_time_to_run(config, now):
    mode = config.get("mode", "STRATEGY").upper()
    config_id = config.get("config_id")

    if mode == "SPOT_DCA":
        freq = normalize_dca_freq(config.get("dca_freq", "1d"))
        target_hour, target_minute = parse_dca_time(config.get("dca_time", "08:00"))

        if now.hour < target_hour or (
            now.hour == target_hour and now.minute < target_minute
        ):
            return False

        if freq == "1w":
            target_weekday = int(config.get("dca_weekday", 0))
            if now.weekday() != target_weekday:
                return False

        last_run = _last_run_times.get(config_id)
        if last_run:
            if freq == "1d" and last_run.date() == now.date():
                return False
            if (
                freq == "1w"
                and last_run.year == now.year
                and last_run.isocalendar()[1] == now.isocalendar()[1]
            ):
                return False

        if check_dca_executed(config_id, now, freq):
            return False

        return True

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


def run_config_maintenance(config):
    config_id = config.get("config_id", "unknown")
    symbol = config.get("symbol")
    mode = config.get("mode", "STRATEGY").upper()

    if not symbol:
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
                cached = (fingerprint, time.monotonic(), PositionProtection(MarketTool(config_id=config_id)))
                _protection_clients[config_id] = cached
            results = cached[2].reconcile_all()
            for result in results:
                if result.get("error"):
                    logger.error(f"[PositionProtection] {config_id}: {result}")
        except Exception as exc:
            logger.error(f"[PositionProtection] {config_id} reconciliation failed: {exc}")

    elif mode == "SPOT_DCA":
        try:
            from backend.app.services.dashboard_service import calculate_dca_stats

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
        run_agent_for_config(
            config,
            progress_callback=lambda event: _mark_scheduler_progress(config_id, scheduled_at, event),
        )
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
        future = _get_maintenance_executor().submit(run_config_maintenance, config)
        _running_maintenance_futures[config_id] = future
    logger.debug(f"[{config_id}] maintenance queued")
    return True


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
        future = _get_agent_executor().submit(run_config_agent, config, scheduled_at)
    except Exception as exc:
        _mark_scheduler_run(config_id, AGENT_JOB_TYPE, scheduled_at, "FAILED", error=str(exc))
        logger.error(f"[{config_id}] failed to submit scheduled agent job scheduled_at={scheduled_at}: {exc}")
        return False

    with _scheduler_lock:
        _running_agent_futures[config_id] = future
    return True


def _submit_daily_summary() -> bool:
    global _daily_summary_future
    with _scheduler_lock:
        if _daily_summary_future:
            if not _daily_summary_future.done():
                logger.debug("[DailySummary] skip_due_to_running")
                return False
            try:
                _daily_summary_future.result()
            except Exception as exc:
                logger.error(f"[DailySummary] worker failed: {exc}")
        _daily_summary_future = _get_maintenance_executor().submit(run_daily_summary_job)
    return True


def _submit_short_memory() -> bool:
    global _short_memory_future
    with _scheduler_lock:
        if _short_memory_future:
            if not _short_memory_future.done():
                logger.debug("[ShortMemory] skip_due_to_running")
                return False
            try:
                _short_memory_future.result()
            except Exception as exc:
                logger.error(f"[ShortMemory] worker failed: {exc}")
        _short_memory_future = _get_maintenance_executor().submit(run_short_memory_job)
    return True


def wait_until_next_minute():
    now = datetime.now().astimezone(TZ_CN)
    sleep_seconds = 60 - now.second - (now.microsecond / 1000000.0)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


def job(now: datetime | None = None):
    global _last_heartbeat_key
    now = now or datetime.now(TZ_CN)
    scheduled_at = _scheduled_at(now)

    global_config.reload_config()
    sync_langsmith_environment()
    configs = global_config.get_all_symbol_configs()
    active_configs = [c for c in configs if c.get("enabled", True)]

    _drop_finished_futures(_running_agent_futures, "agent")
    _drop_finished_futures(_running_maintenance_futures, "maintenance")

    if not active_configs:
        return {"active": 0, "maintenance_queued": 0, "agent_queued": 0, "agent_due": 0}

    maintenance_queued = 0
    agent_due = 0
    agent_queued = 0

    for config in active_configs:
        if _submit_maintenance(config):
            maintenance_queued += 1

        if is_time_to_run(config, now):
            config_id = config.get("config_id", "unknown")
            mode = str(config.get("mode", "STRATEGY")).upper()
            agent_due += 1
            logger.info(
                f"[{config_id}] due detected ({mode}, scheduled_at={scheduled_at}, interval={config.get('run_interval', 'default')})"
            )
            _last_run_times[config_id] = now
            if _submit_agent(config, scheduled_at):
                agent_queued += 1

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


def _ensure_balance_snapshot_for_date(configs: list, date_str: str) -> None:
    """确保指定日期在 balance_history 中有快照记录；若缺失则通过交易所实时拉取一次补录。"""
    from backend.database import get_db_conn, save_balance_snapshot
    from backend.utils.market_data import MarketTool

    # 按 symbol 分组，取第一个 REAL 模式的 config 用于拉取余额
    symbol_config_map: dict[str, dict] = {}
    for cfg in configs:
        symbol = cfg.get("symbol")
        mode = cfg.get("mode", "STRATEGY").upper()
        if symbol and mode in ("REAL",) and symbol not in symbol_config_map:
            symbol_config_map[symbol] = cfg

    for symbol, cfg in symbol_config_map.items():
        try:
            config_id = cfg.get("config_id", "unknown")
            with get_db_conn() as conn:
                c = conn.cursor()
                row = c.execute(
                    "SELECT id FROM balance_history "
                    "WHERE (config_id = ? OR (config_id IS NULL AND symbol = ?)) "
                    "AND date(timestamp) = ? LIMIT 1",
                    (str(config_id), symbol, date_str),
                ).fetchone()
            if row:
                continue  # 已有数据，跳过

            # 当天无快照，拉取当前余额补录
            logger.info(f"[DailySummary] No balance snapshot for {symbol} on {date_str}, fetching now...")
            m_tool = MarketTool(config_id=config_id)
            account_data = m_tool.get_account_status(symbol, is_real=True, config_id=config_id)
            balance = float(account_data.get("balance", 0))
            positions = account_data.get("real_positions", [])
            unrealized = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
            if balance > 0:
                save_balance_snapshot(symbol, balance, unrealized, config_id=config_id)
                logger.info(f"[DailySummary] Saved balance snapshot for {symbol}: balance={balance}, unrealized={unrealized}")
        except Exception as exc:
            logger.warning(f"[DailySummary] Failed to ensure balance snapshot for {symbol}: {exc}")


def run_daily_summary_job(now: datetime | None = None) -> dict:
    global _daily_summary_done_date, _daily_summary_last_attempt_at
    from backend.database import get_daily_summaries, get_pending_daily_summary_data

    now = now or datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")

    if _daily_summary_done_date == today_str:
        return {"status": "already_completed", "date": today_str}

    target_hour, target_minute = parse_daily_summary_time()
    target_time = now.replace(
        hour=target_hour,
        minute=target_minute,
        second=0,
        microsecond=0,
    )
    if now < target_time:
        return {
            "status": "not_due",
            "date": today_str,
            "scheduled_time": f"{target_hour:02d}:{target_minute:02d}",
        }

    retry_after = timedelta(minutes=_daily_summary_retry_minutes())
    if (
        _daily_summary_last_attempt_at is not None
        and _daily_summary_last_attempt_at.date() == now.date()
        and now - _daily_summary_last_attempt_at < retry_after
    ):
        return {"status": "retry_wait", "date": today_str}
    _daily_summary_last_attempt_at = now

    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    logger.info(
        f"[DailySummary] start summarizing {yesterday} "
        f"(scheduled_time={target_hour:02d}:{target_minute:02d}, now={now.strftime('%H:%M')})"
    )

    configs = global_config.get_all_symbol_configs()
    active_configs = [c for c in configs if c.get("enabled", True)]

    # 先补录昨日缺失的余额快照，保证 Equity 对比图表有数据
    _ensure_balance_snapshot_for_date(active_configs, yesterday)

    result = {
        "status": "completed",
        "date": yesterday,
        "generated": 0,
        "existing": 0,
        "no_source": 0,
        "failed": [],
    }
    for config in active_configs:
        config_id = str(config.get("config_id") or "")
        if not config_id:
            continue
        try:
            existing = next(
                (
                    row
                    for row in get_daily_summaries(config_id, days=7)
                    if row.get("date") == yesterday
                ),
                None,
            )
            if existing and not is_invalid_daily_summary(existing.get("summary")):
                result["existing"] += 1
                continue

            source_rows = get_pending_daily_summary_data(config_id, yesterday)
            if not any(str(row.get("strategy_logic") or "").strip() for row in source_rows):
                from backend.utils.trade_review import daily_execution_evidence
                if config.get('mode', '').upper() not in {'REAL', 'SPOT_DCA'} and not daily_execution_evidence(config_id, yesterday):
                    result["no_source"] += 1
                    logger.info(f"[DailySummary] no source data for {config_id} on {yesterday}; skipped")
                    continue

            if generate_manual_daily_summary(config_id, yesterday):
                result["generated"] += 1
            else:
                result["failed"].append(config_id)
        except Exception as exc:
            result["failed"].append(config_id)
            logger.error(f"[DailySummary] failed for {config_id} on {yesterday}: {exc}")

    if result["failed"]:
        result["status"] = "retry_pending"
        logger.warning(
            f"[DailySummary] incomplete for {yesterday}; retry in "
            f"{_daily_summary_retry_minutes()} minutes, failed={result['failed']}"
        )
    else:
        _daily_summary_done_date = today_str
        logger.info(
            f"[DailySummary] completed summarizing {yesterday}: "
            f"generated={result['generated']} existing={result['existing']} "
            f"no_source={result['no_source']}"
        )
    return result


def run_short_memory_job(now: datetime | None = None) -> dict:
    """Generate the latest fully completed four-hour memory bucket.

    The scheduler checks this every minute so a restart shortly after a
    four-hour boundary still catches up instead of waiting for the next one.
    """
    now = now or datetime.now(TZ_CN)
    current_bucket_start, _ = get_short_memory_bucket(now)
    target_time = current_bucket_start - timedelta(seconds=1)
    bucket_start, bucket_end = get_short_memory_bucket(target_time)
    bucket_key = bucket_start.strftime("%Y-%m-%d %H:%M:%S")
    result = {
        "status": "completed",
        "bucket_start": bucket_key,
        "bucket_end": bucket_end.strftime("%Y-%m-%d %H:%M:%S"),
        "generated": 0,
        "existing": 0,
        "retry_wait": 0,
        "failed": [],
    }

    configs = [cfg for cfg in global_config.get_all_symbol_configs() if cfg.get("enabled", True)]
    retry_after = timedelta(minutes=_short_memory_retry_minutes())
    for config in configs:
        config_id = str(config.get("config_id") or "")
        if not config_id:
            continue
        done_key = (bucket_key, config_id)
        if done_key in _short_memory_done_buckets:
            result["existing"] += 1
            continue

        last_attempt = _short_memory_last_attempts.get(done_key)
        if last_attempt is not None and now - last_attempt < retry_after:
            result["retry_wait"] += 1
            continue

        _short_memory_last_attempts[done_key] = now
        try:
            generated = generate_short_memory_for_config(config_id, now_cn=target_time)
            existing = get_short_memory(config_id, bucket_key)
            if generated:
                result["generated"] += 1
                _short_memory_done_buckets.add(done_key)
            elif existing and int(existing.get("source_count") or 0) > 0:
                result["existing"] += 1
                _short_memory_done_buckets.add(done_key)
            else:
                result["failed"].append(config_id)
        except Exception as exc:
            result["failed"].append(config_id)
            logger.error(f"[ShortMemory] failed for {config_id}: {exc}")

    cutoff_key = (bucket_start - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    _short_memory_done_buckets.intersection_update(
        {key for key in _short_memory_done_buckets if key[0] >= cutoff_key}
    )
    for key in list(_short_memory_last_attempts):
        if key[0] < cutoff_key or key in _short_memory_done_buckets:
            _short_memory_last_attempts.pop(key, None)

    if result["failed"]:
        result["status"] = "retry_pending"
        logger.warning(
            f"[ShortMemory] incomplete for {bucket_key}; retry in "
            f"{_short_memory_retry_minutes()} minutes, failed={result['failed']}"
        )
    elif result["retry_wait"]:
        result["status"] = "retry_wait"
    elif result["generated"]:
        logger.info(
            f"[ShortMemory] completed {bucket_key} - {result['bucket_end']}: "
            f"generated={result['generated']} existing={result['existing']}"
        )
    return result


def run_scheduler_forever():
    logger.info(f"[System] scheduler loop started (max_workers={_scheduler_max_workers()})")

    try:
        init_db()
        logger.info("Database initialized for scheduler")
    except Exception as e:
        logger.error(f"Database initialization failed in scheduler: {e}")

    global_config.reload_config()
    _start_protection_monitor()

    while True:
        try:
            wait_until_next_minute()

            if not global_config.enable_scheduler:
                if datetime.now().minute % 10 == 0:
                    logger.info("Scheduler disabled globally, waiting...")
                continue

            _submit_daily_summary()
            _submit_short_memory()
            job()

            if datetime.now().minute % 5 == 0:
                global_config.reload_config()

        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
            time.sleep(10)


def scheduler_should_run() -> bool:
    return bool(getattr(global_config, "enable_scheduler", True))


def protection_tick():
    """Only persisted live plans need exchange polling; disabled agents still get protection."""
    global _protection_executor
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        rows = conn.execute('SELECT config_id,payload FROM real_protection_plans').fetchall()
    ids = {row['config_id'] for row in rows if json.loads(row['payload'])['state'] != 'DONE'}
    configs = {cfg['config_id']: cfg for cfg in global_config.get_all_symbol_configs()}
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
        _protection_futures[config_id] = _protection_executor.submit(run_config_maintenance, cfg)


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
    run_scheduler_forever()
