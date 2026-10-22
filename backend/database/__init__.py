"""Public database API; domain stores live in the sibling modules."""
import os
from backend import storage
import time
import uuid
import pytz
from datetime import datetime, timedelta
import json
from contextlib import contextmanager
from backend.database.balance import BalanceHistoryStore
from backend.database.chat import ChatSessionStore
from backend.database.cleanup import ConfigCleanupStore
from backend.database.dca import DcaSnapshotStore
from backend.database.memory import SummaryMemoryStore
from backend.database.mock import MockTradingStore
from backend.database.orders import OrderPersistenceStore
from backend.database.position import PositionHistoryStore
from backend.database.pricing import PricingStore
from backend.database.summary import SummaryStore
from backend.database.statistics import TradingStatisticsStore
from backend.database.trade import TradeHistoryStore
from backend.storage_paths import DATA_DIR, PROJECT_ROOT
from backend.database.schema import initialize_schema
from backend.utils.logger import setup_logger

# 设置时区
TZ_CN = pytz.timezone('Asia/Shanghai')

BASE_DIR = str(PROJECT_ROOT)
# Inert compatibility value for legacy callers; PostgreSQL never opens or copies it.
DB_NAME = os.path.abspath(os.getenv("TRADING_DB_PATH", str(DATA_DIR / "trading_data.db")))
logger = setup_logger("Database")

@contextmanager
def get_db_conn(timeout=30):
    """Acquire PostgreSQL with the caller's existing bounded wait budget."""
    from backend.mcp.resilience import current_budget, setting
    budget = current_budget.get()
    if budget:
        timeout = min(timeout, setting('DB_WAIT_SECONDS', 0.25))
    with storage.get_connection(timeout=timeout) as conn:
        yield conn


def _initialize_runtime_config() -> None:
    from backend.config_store import ensure_runtime_config_initialized
    ensure_runtime_config_initialized()


def _reload_runtime_config() -> None:
    from backend.config import config as runtime_config
    runtime_config.reload_config()


def _current_timestamp() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


def _current_date() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d")


def _current_epoch() -> float:
    return datetime.now().timestamp()


_chat_session_store = ChatSessionStore(get_db_conn, _current_timestamp)
_balance_history_store = BalanceHistoryStore(get_db_conn, _current_timestamp)
_pricing_store = PricingStore(get_db_conn)
_config_cleanup_store = ConfigCleanupStore(get_db_conn, _current_timestamp)
_dca_snapshot_store = DcaSnapshotStore(get_db_conn, _current_date, _current_timestamp)
_summary_memory_store = SummaryMemoryStore(get_db_conn, lambda: datetime.now(TZ_CN), logger)
_mock_trading_store = MockTradingStore(get_db_conn, lambda: datetime.now(TZ_CN), _current_epoch, lambda **kwargs: upsert_position_history(**kwargs), logger)
_order_persistence_store = OrderPersistenceStore(get_db_conn, _current_timestamp)
_position_history_store = PositionHistoryStore(get_db_conn, lambda: datetime.now(TZ_CN), logger)
_summary_store = SummaryStore(get_db_conn, _current_timestamp, logger)
_trade_history_store = TradeHistoryStore(get_db_conn, logger)
_statistics_store = TradingStatisticsStore(lambda: get_db_conn())

def init_db():
    logger.info("Checking PostgreSQL schema version")
    with get_db_conn() as conn:
        initialize_schema(conn)
        from backend.utils.protection_scan import initialize_schema as initialize_scan_mode
        initialize_scan_mode(conn.cursor())
        conn.commit()

    _initialize_runtime_config()
    _reload_runtime_config()

# --- 模型计价管理 ---

def get_all_pricing():
    """获取所有模型的计价信息"""
    return _pricing_store.get_all()


def update_model_pricing(model, input_price, output_price, currency='USD'):
    """更新或插入模型计价"""
    _pricing_store.upsert(model, input_price, output_price, currency)


def delete_model_pricing(model):
    """删除模型计价。"""
    return _pricing_store.delete(model)

# --- 模拟交易资金池 / 挂单池功能 ---

def get_mock_account(config_id, symbol):
    """获取/初始化模拟账户"""
    return _mock_trading_store.get_account(config_id, symbol)

def update_mock_account_balance(config_id, symbol, realized_pnl):
    """更新模拟账户余额，处理爆仓逻辑，并记录一条流水（以 realized_pnl 触发）。
    注意：这条快照仅作为余额变动点保留，未实现盈亏/总权益字段默认与余额相同；
    调度器会在每分钟调用 save_mock_equity_snapshot 写入包含未实现盈亏的完整快照。
    """
    return _mock_trading_store.update_account_balance(config_id, symbol, realized_pnl)


def save_mock_equity_snapshot(config_id, symbol, balance, unrealized_pnl):
    """写入包含未实现盈亏的策略模式权益快照。

    :param balance: 钱包余额（已实现）
    :param unrealized_pnl: 已成交模拟仓位的未实现盈亏（未成交挂单不计）
    """
    return _mock_trading_store.save_equity_snapshot(config_id, symbol, balance, unrealized_pnl)

def get_mock_equity_history(config_id, days=30, symbol=None, include_fallback=True):
    """获取指定策略模拟账户的资金曲线（按天聚合的最后一条），最多保留 30 天。
    优先使用 total_equity (钱包余额+未实现盈亏)，旧数据回退 balance。
    """
    return _mock_trading_store.get_equity_history(
        config_id,
        days=days,
        symbol=symbol,
        include_fallback=include_fallback,
    )

def save_token_usage(symbol, config_id, model, prompt_tokens, completion_tokens):
    """记录 LLM Token 使用情况"""
    timestamp = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    total_tokens = prompt_tokens + completion_tokens
    
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO token_usage (timestamp, symbol, config_id, model, prompt_tokens, completion_tokens, total_tokens)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (timestamp, symbol, config_id, model, prompt_tokens, completion_tokens, total_tokens))
            conn.commit()
        except storage.Error:
            raise
        except Exception as e:
            logger.error(f"❌ DB Error (save_token_usage): {e}")

def get_mock_orders(symbol=None, agent_name=None, config_id=None):
    """获取活跃模拟挂单 (支持 Agent 隔离)"""
    return _mock_trading_store.get_orders(symbol=symbol, agent_name=agent_name, config_id=config_id)

def create_mock_order(symbol, side, price, amount, stop_loss, take_profit, agent_name, config_id=None, order_id=None, expire_at=None):
    """创建模拟挂单 (必须传入 agent_name 和 config_id)"""
    if not order_id:
        order_id = f"ST-{uuid.uuid4().hex[:6]}"
    _mock_trading_store.create_order(symbol, side, price, amount, stop_loss, take_profit, agent_name, config_id=config_id, order_id=order_id, expire_at=expire_at)

def cancel_mock_order(order_id):
    return _mock_trading_store.cancel_order(order_id)

def update_mock_order_filled(order_id):
    """标记模拟挂单已成交 (入场)"""
    _mock_trading_store.mark_order_filled(order_id)

def close_mock_order(order_id, close_price=0.0, realized_pnl=0.0):
    """平仓模拟挂单"""
    return _mock_trading_store.close_order(order_id, close_price=close_price, realized_pnl=realized_pnl)


def reduce_mock_positions(config_id: str, symbol: str, pos_side: str, amount: float, current_price: float, reason: str = '', operation_id: str | None = None):
    """Reduce filled attached-mode positions atomically at the observed market price."""
    if operation_id is None:
        return _mock_trading_store.reduce_positions(config_id, symbol, pos_side, amount, current_price, reason)
    return _mock_trading_store.reduce_positions(config_id, symbol, pos_side, amount, current_price, reason, operation_id=operation_id)


def save_order_log(
    order_id,
    symbol,
    agent_name,
    side,
    entry,
    tp,
    sl,
    reason,
    trade_mode="STRATEGY",
    config_id=None,
    amount=0,
    status="OPEN",
    event_type="ORDER_CREATED",
    parent_order_id=None,
    is_auto=False,
    realized_pnl=0.0,
):
    order_id_text = str(order_id)
    if order_id_text.endswith("_AUTO"):
        parent_id = order_id_text[:-5]
        order_id = parent_id
        parent_order_id = parent_order_id or parent_id
        is_auto = True
        if event_type == "ORDER_CREATED":
            event_type = "AUTO_CLOSE"
            try:
                close_price = float(entry or 0)
                tp_price = float(tp or 0)
                sl_price = float(sl or 0)
                if sl_price > 0 and abs(close_price - sl_price) <= 1e-12:
                    event_type = "SL_HIT"
                elif tp_price > 0 and abs(close_price - tp_price) <= 1e-12:
                    event_type = "TP_HIT"
            except storage.Error:
                raise
            except Exception:
                event_type = "AUTO_CLOSE"
        if not realized_pnl:
            try:
                with get_db_conn() as conn:
                    row = conn.execute(
                        "SELECT side, price, amount FROM mock_orders WHERE order_id = %s LIMIT 1",
                        (parent_id,),
                    ).fetchone()
                if row:
                    close_price = float(entry or 0)
                    entry_price = float(row["price"] or 0)
                    qty = float(amount or row["amount"] or 0)
                    direction = 1 if "BUY" in str(row["side"]).upper() else -1
                    realized_pnl = (close_price - entry_price) * qty * direction
            except storage.Error:
                raise
            except Exception:
                realized_pnl = 0.0
    _order_persistence_store.save_order_log(
        order_id,
        symbol,
        agent_name,
        side,
        entry,
        tp,
        sl,
        reason,
        trade_mode=trade_mode,
        config_id=config_id,
        amount=amount,
        status=status,
        event_type=event_type,
        parent_order_id=parent_order_id,
        is_auto=is_auto,
        realized_pnl=realized_pnl,
    )


def update_order_fill_status(order_id, status, filled_qty=0.0, filled_cost=0.0, avg_fill_price=0.0, filled_at=None):
    """更新 orders 表的成交状态信息（主要用于 SPOT_DCA）。"""
    _order_persistence_store.update_fill_status(order_id, status, filled_qty=filled_qty, filled_cost=filled_cost, avg_fill_price=avg_fill_price, filled_at=filled_at)


def upsert_spot_order_fill(order_id, config_id, symbol, status, filled_qty=0.0, filled_cost=0.0, avg_fill_price=0.0, filled_at=None, *, connection: storage.Connection | None = None):
    """写入现货成交同步状态；传入连接时由调用方统一提交。"""
    _order_persistence_store.upsert_spot_fill(
        order_id, config_id, symbol, status, filled_qty=filled_qty,
        filled_cost=filled_cost, avg_fill_price=avg_fill_price,
        filled_at=filled_at, connection=connection,
    )


def save_dca_daily_snapshot(config_id, symbol, stats):
    """按天保存 DCA 统计快照（同一天覆盖更新）。"""
    _dca_snapshot_store.save_snapshot(config_id, symbol, stats)


def get_dca_daily_snapshot_history(config_id, days=30, symbol=None):
    """获取最近 N 天 DCA 快照曲线。"""
    return _dca_snapshot_store.get_snapshot_history(config_id, days=days, symbol=symbol)

# --- 数据分析与记录 ---

def save_summary(
    symbol,
    agent_name,
    content,
    strategy_logic,
    config_id=None,
    agent_type=None,
    reasoning_content=None,
    reasoning_tokens=0,
    report_json=None,
    run_id=None,
    timeframe='1h',
    decision_json=None,
    enqueue_memory=True,
    reasoning_diagnostics=None,
):
    """保存 AI 分析结果"""
    return _summary_store.save_summary(
        symbol,
        agent_name,
        content,
        strategy_logic,
        config_id=config_id,
        agent_type=agent_type,
        reasoning_content=reasoning_content,
        reasoning_tokens=reasoning_tokens,
        report_json=report_json,
        run_id=run_id,
        timeframe=timeframe,
        decision_json=decision_json,
        enqueue_memory=enqueue_memory,
        reasoning_diagnostics=reasoning_diagnostics,
    )

def get_active_agents(symbol):
    return _summary_store.get_active_agents(symbol)

def get_recent_summaries(symbol, agent_name=None, limit=10, config_id=None, agent_type=None):
    """获取最近的分析记录 (支持 agent_name, config_id 或 agent_type 隔离)"""
    return _summary_store.get_recent_summaries(symbol, agent_name=agent_name, limit=limit, config_id=config_id, agent_type=agent_type)

def get_recent_summary_logic(config_id, since_time=None, limit=12):
    return _summary_store.get_recent_summary_logic(config_id, since_time=since_time, limit=limit)

def get_summary_count(symbol, config_id=None):
    return _summary_store.get_summary_count(symbol, config_id=config_id)

def get_paginated_summaries(symbol, page=1, per_page=10, config_id=None):
    return _summary_store.get_paginated_summaries(symbol, page=page, per_page=per_page, config_id=config_id)

def delete_summaries_by_symbol(symbol):
    """删除指定币种的所有分析历史和决策流水"""
    return _summary_store.delete_by_symbol(symbol)

def save_balance_snapshot(symbol, balance, unrealized_pnl, config_id=None):
    """记录实盘资金快照。

    :param symbol: 交易对
    :param balance: 钱包余额（total_balance）
    :param unrealized_pnl: 未实现盈亏
    :param config_id: 可选，按策略配置记录，便于多配置隔离对比
    """
    _balance_history_store.save_snapshot(symbol, balance, unrealized_pnl, config_id=config_id)

def get_paginated_orders(config_id, page=1, per_page=10):
    """获取分页决策流水 (支持 Agent 隔离)"""
    return _order_persistence_store.get_paginated_orders(config_id, page=page, per_page=per_page)

def get_balance_history(symbol, limit=100, config_id=None):
    """获取资金曲线数据。
    若提供 config_id，优先返回该 config 的快照；旧数据无 config_id 时按 symbol 回退。
    """
    return _balance_history_store.get_history(symbol, limit=limit, config_id=config_id)


def auto_close_expired_mock_orders(config_id=None, symbol=None):
    """自动关闭已过期但仍为 OPEN 的未成交模拟挂单。
    返回关闭的订单数。已成交仓位不受影响。
    """
    return _mock_trading_store.auto_close_expired_orders(config_id=config_id, symbol=symbol)


def get_filled_mock_positions(config_id, symbol=None):
    """获取指定策略已入场但未平仓的模拟仓位（is_filled=1, status=OPEN）。"""
    return _mock_trading_store.get_filled_positions(config_id, symbol=symbol)


def save_trade_history(trades, config_id=None):
    """批量保存成交记录 (会自动忽略已存在的 trade_id)"""
    _trade_history_store.save_trades(trades, config_id=config_id)

def get_trade_history(symbol, limit=50):
    """获取历史成交"""
    return _trade_history_store.list_trades(symbol, limit=limit)

def clean_financial_data(symbol):
    """删除指定币种的资金和成交记录 (用于重置)"""
    return _trade_history_store.clean_symbol_data(symbol)


def create_chat_session(
    session_id: str,
    config_id: str,
    symbol: str,
    title: str,
    session_type: str = "task",
    runtime_json: str = "{}",
    parent_session_id: str | None = None,
    root_session_id: str | None = None,
    fork_message_index: int | None = None,
):
    _chat_session_store.create_session(
        session_id,
        config_id,
        symbol,
        title,
        session_type=session_type,
        runtime_json=runtime_json,
        parent_session_id=parent_session_id,
        root_session_id=root_session_id,
        fork_message_index=fork_message_index,
    )


def touch_chat_session(session_id: str):
    _chat_session_store.touch_session(session_id)


def get_chat_session(session_id: str):
    return _chat_session_store.get_session(session_id)


def get_chat_sessions(limit: int = 100):
    return _chat_session_store.list_sessions(limit=limit)


def update_chat_session_title(session_id: str, title: str, *, summary_token: str | None = None):
    return _chat_session_store.update_title(session_id, title, summary_token=summary_token)


def claim_chat_title_summary(session_id: str, summary_cursor: int) -> str | None:
    return _chat_session_store.claim_title_summary(session_id, summary_cursor)


def reset_chat_title_summary(session_id: str) -> None:
    _chat_session_store.reset_title_summary(session_id)


def delete_chat_session(session_id: str) -> int:
    return _chat_session_store.delete_session(session_id)


def delete_chat_sessions(session_ids):
    return _chat_session_store.delete_sessions(session_ids)

# --- 每日策略汇总 ---

def save_daily_summary(date_str, symbol, config_id, summary, source_count):
    """保存或更新某天某 config 的每日策略汇总"""
    _summary_memory_store.save_daily_summary(date_str, symbol, config_id, summary, source_count)

def update_daily_summary(date_str, config_id, summary):
    """更新某天某 config 的每日策略汇总文本"""
    _summary_memory_store.update_daily_summary(date_str, config_id, summary)

def delete_daily_summary(date_str, config_id):
    """Delete one daily summary by date and config."""
    return _summary_memory_store.delete_daily_summary(date_str, config_id)

def get_daily_summaries(config_id, days=7):
    """获取最近 N 天的每日策略汇总（按日期倒序）"""
    return _summary_memory_store.get_daily_summaries(config_id, days=days)


def list_daily_summaries(symbol=None, config_id=None, days=None, limit=200):
    """List daily summaries for admin management and export."""
    return _summary_memory_store.list_daily_summaries(symbol=symbol, config_id=config_id, days=days, limit=limit)


def get_pending_daily_summary_data(config_id, date_str):
    """获取指定日期、指定 config 的所有 strategy_logic 原文（用于 LLM 汇总）"""
    return _summary_memory_store.get_pending_daily_summary_data(config_id, date_str)

def get_summary_logic_between(config_id, start_time, end_time):
    return _summary_memory_store.get_summary_logic_between(config_id, start_time, end_time)


def save_short_memory(bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count, **metadata):
    _summary_memory_store.save_short_memory(bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count, **metadata)


def get_short_memories(config_id, limit=2):
    return _summary_memory_store.get_short_memories(config_id, limit=limit)


def list_short_memories(symbol=None, config_id=None, limit=200):
    return _summary_memory_store.list_short_memories(symbol=symbol, config_id=config_id, limit=limit)


def get_short_memory(config_id, bucket_start):
    return _summary_memory_store.get_short_memory(config_id, bucket_start)


def update_short_memory(config_id, bucket_start, market_summary, position_summary):
    return _summary_memory_store.update_short_memory(config_id, bucket_start, market_summary, position_summary)


def clear_short_memory_snapshot(config_id: str, bucket_start: str) -> int:
    return _summary_memory_store.clear_short_memory_snapshot(config_id, bucket_start)


def delete_short_memories(symbol=None, config_id=None, bucket_start_from=None, bucket_start_to=None, buckets=None):
    return _summary_memory_store.delete_short_memories(
        symbol=symbol,
        config_id=config_id,
        bucket_start_from=bucket_start_from,
        bucket_start_to=bucket_start_to,
        buckets=buckets,
    )


def save_news_snapshot(symbol, config_id, news_context):
    """Persist the latest news context shown to agents and the dashboard."""
    if not news_context:
        return
    headlines = news_context.get("headlines") or []
    source = news_context.get("source") or ""
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO news_snapshots (timestamp, symbol, config_id, headlines, source, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                _current_timestamp(),
                symbol,
                config_id,
                json.dumps(headlines, ensure_ascii=False),
                source,
                json.dumps(news_context, ensure_ascii=False),
            ),
        )
        conn.commit()


def get_latest_news_snapshot(symbol=None, config_id=None):
    clauses = []
    params = []
    if config_id:
        clauses.append("config_id = %s")
        params.append(str(config_id))
    if symbol:
        clauses.append("symbol = %s")
        params.append(str(symbol))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_db_conn() as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM news_snapshots
            {where}
            ORDER BY timestamp DESC NULLS LAST, id DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
    if not row:
        return None
    payload = storage.record(row)
    payload.pop("risk_level", None)
    try:
        payload["headlines"] = json.loads(payload.get("headlines") or "[]")
    except storage.Error:
        raise
    except Exception:
        payload["headlines"] = []
    try:
        payload["raw"] = json.loads(payload.get("raw_json") or "{}")
    except storage.Error:
        raise
    except Exception:
        payload["raw"] = {}
    payload["raw"].pop("risk_level", None)
    payload["raw"].pop("risk_reasons", None)
    return payload


def save_intelligence_source_cache(source_key, fetched_at, payload, last_error=None):
    """Persist the last successful normalized payload for a news/calendar source."""
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO intelligence_source_cache (source_key, fetched_at, payload_json, last_error)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(source_key) DO UPDATE SET
                fetched_at = excluded.fetched_at,
                payload_json = excluded.payload_json,
                last_error = excluded.last_error
            """,
            (str(source_key), str(fetched_at), json.dumps(payload, ensure_ascii=False), last_error),
        )
        conn.commit()


def get_intelligence_source_cache(source_key):
    with get_db_conn() as conn:
        row = conn.execute(
            "SELECT source_key, fetched_at, payload_json, last_error FROM intelligence_source_cache WHERE source_key = %s",
            (str(source_key),),
        ).fetchone()
    if not row:
        return None
    payload = storage.record(row)
    try:
        payload["payload"] = json.loads(payload.pop("payload_json") or "[]")
    except storage.Error:
        raise
    except Exception:
        payload["payload"] = []
    return payload


def upsert_position_history(
    config_id,
    symbol,
    position_key,
    side=None,
    status=None,
    source=None,
    opened_at=None,
    closed_at=None,
    entry_price=None,
    close_price=None,
    amount=None,
    realized_pnl=None,
    raw=None,
):
    _position_history_store.upsert(
        config_id=config_id,
        symbol=symbol,
        position_key=position_key,
        side=side,
        status=status,
        source=source,
        opened_at=opened_at,
        closed_at=closed_at,
        entry_price=entry_price,
        close_price=close_price,
        amount=amount,
        realized_pnl=realized_pnl,
        raw=raw,
    )


def get_position_history(config_id, since_time=None, limit=50):
    return _position_history_store.list(config_id, since_time=since_time, limit=limit)


def sync_open_position_history(config_id, symbol, positions, source="exchange_position"):
    _position_history_store.sync_open_positions(config_id, symbol, positions, source=source)


def sync_trade_position_history(config_id, symbol, trades, source="exchange_trade"):
    _position_history_store.sync_trade_positions(config_id, symbol, trades, source=source)


def _config_mode_for_stats(config_id):
    try:
        from backend.config import config as global_config

        cfg = global_config.get_config_by_id(config_id)
    except storage.Error:
        raise
    except Exception:
        cfg = None
    return str(cfg.get("mode") or "").upper() if cfg else ""


def get_closed_positions_7d(
    config_id: str,
    symbol: str | None = None,
    days: int = 7,
    mode: str | None = None,
) -> list[dict]:
    """Retrieve positions closed within the past N days from the runtime database.

    Standardized fields per record:
        opened_at: str,
        closed_at: str,
        symbol: str,
        side: str ('LONG' / 'SHORT'),
        entry_price: float,
        close_price: float,
        amount: float,
        take_profit: float | None,
        stop_loss: float | None,
        realized_pnl: float
    """
    now = datetime.now(TZ_CN)
    cutoff_str = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    mode = str(mode or _config_mode_for_stats(config_id)).upper()
    if mode == "REAL":
        from backend.config import config as global_config
        from backend.utils.execution_ledger import recent_activity_data, closed_positions_from_activity
        selected_symbol = symbol or (global_config.get_config_by_id(config_id) or {}).get('symbol')
        activity = recent_activity_data(config_id, selected_symbol, now, hours=days * 24)
        return closed_positions_from_activity(activity)
    clean_sym = symbol.split(':')[0] if symbol else None
    symbol_filter, symbol_params = '', []
    if clean_sym:
        if ':' in symbol:
            # Accept the legacy unsuffixed alias, but not another contract.
            symbol_filter = ' AND symbol IN (%s, %s)'
            symbol_params = [symbol, clean_sym]
        else:
            # Compare the complete pair: BTC/USD must not include BTC/USDT.
            symbol_filter = " AND split_part(symbol, ':', 1) = %s"
            symbol_params = [clean_sym]

    results: list[dict] = []

    with get_db_conn() as conn:
        cursor = conn.cursor()
        tables = storage.table_names(conn)

        # 1. STRATEGY mode: query mock_orders
        if mode == "STRATEGY":
            if "mock_orders" in tables:
                query = (
                    "SELECT timestamp, close_time, symbol, side, price, close_price, amount, "
                    "take_profit, stop_loss, realized_pnl FROM mock_orders "
                    "WHERE config_id = %s AND status = 'CLOSED' AND close_time >= %s"
                )
                params = [config_id, cutoff_str]
                if clean_sym:
                    query += symbol_filter
                    params.extend(symbol_params)
                query += " ORDER BY close_time DESC"

                for r in cursor.execute(query, params).fetchall():
                    side = "LONG" if "BUY" in str(r["side"]).upper() else "SHORT"
                    results.append({
                        "opened_at": r["timestamp"] or "未知",
                        "closed_at": r["close_time"],
                        "symbol": r["symbol"],
                        "side": side,
                        "entry_price": float(r["price"] or 0),
                        "close_price": float(r["close_price"] or 0),
                        "amount": float(r["amount"] or 0),
                        "take_profit": float(r["take_profit"]) if r["take_profit"] and r["take_profit"] > 0 else None,
                        "stop_loss": float(r["stop_loss"]) if r["stop_loss"] and r["stop_loss"] > 0 else None,
                        "realized_pnl": float(r["realized_pnl"] or 0),
                    })
            return results

        # Fallback for old/unknown configurations only. A known REAL config never
        # falls back to snapshot rows because they are not complete position cycles.
        if not results and "mock_orders" in tables:
            m_query = (
                "SELECT timestamp, close_time, symbol, side, price, close_price, amount, "
                "take_profit, stop_loss, realized_pnl FROM mock_orders "
                "WHERE config_id = %s AND status = 'CLOSED' AND close_time >= %s"
            )
            m_params = [config_id, cutoff_str]
            if clean_sym:
                m_query += symbol_filter
                m_params.extend(symbol_params)
            m_query += " ORDER BY close_time DESC"

            for r in cursor.execute(m_query, m_params).fetchall():
                side = "LONG" if "BUY" in str(r["side"]).upper() else "SHORT"
                results.append({
                    "opened_at": r["timestamp"] or "未知",
                    "closed_at": r["close_time"],
                    "symbol": r["symbol"],
                    "side": side,
                    "entry_price": float(r["price"] or 0),
                    "close_price": float(r["close_price"] or 0),
                    "amount": float(r["amount"] or 0),
                    "take_profit": float(r["take_profit"]) if r["take_profit"] and r["take_profit"] > 0 else None,
                    "stop_loss": float(r["stop_loss"]) if r["stop_loss"] and r["stop_loss"] > 0 else None,
                    "realized_pnl": float(r["realized_pnl"] or 0),
                })

    return results


def format_closed_positions_summary(positions: list[dict], days: int = 7) -> str:
    """Format closed positions and compute 7-day win rate and profit-loss ratio."""
    if not positions:
        return (
            f"【过去{days}天平仓记录（本地成交账本）】\n"
            f"账本中暂无已重建的完整平仓周期；若同步未覆盖该时段，这不代表期间没有交易。\n\n"
            f"【{days}天战绩统计】\n"
            "完整平仓周期: 0 笔 | 胜率: - | 盈亏比: - | 已确认累计盈亏: -"
        )

    currencies = {p.get('settlement_currency', 'USDT') for p in positions}
    if len(currencies) > 1:
        return '\n\n'.join(format_closed_positions_summary(
            [p for p in positions if p.get('settlement_currency', 'USDT') == currency], days)
            for currency in sorted(currencies))
    settlement = next(iter(currencies))
    lines = [f"【过去{days}天平仓记录（本地成交账本）】"]
    win_trades: list[float] = []
    loss_trades: list[float] = []
    tie_trades: list[float] = []
    unknown_trades = 0

    for p in positions:
        open_time = p.get("opened_at") or "未知"
        close_time = p.get("closed_at") or "未知"
        sym = p.get("symbol") or ""
        side = p.get("side") or ""
        side_zh = "多" if side == "LONG" else ("空" if side == "SHORT" else side)
        ep = p.get("entry_price", 0.0)
        cp = p.get("close_price", 0.0)
        amt = p.get("amount", 0.0)
        tp = p.get("take_profit")
        sl = p.get("stop_loss")
        pnl = p.get("realized_pnl")
        currency = p.get('settlement_currency', 'USDT')

        tp_str = f"{tp:.2f}" if tp is not None and tp > 0 else "未设置"
        sl_str = f"{sl:.2f}" if sl is not None and sl > 0 else "未设置"

        cost = ep * amt if ep and amt else 0.0
        roi_str = f" ({pnl / cost * 100:+.2f}%)" if pnl is not None and cost > 0 else ""

        if pnl is None:
            unknown_trades += 1
            outcome = "已实现盈亏: 未知（成交源未返回完整盈亏）"
        elif pnl > 1e-6:
            win_trades.append(pnl)
            outcome = f"盈利: +{pnl:.2f} {currency}{roi_str}"
        elif pnl < -1e-6:
            loss_trades.append(abs(pnl))
            outcome = f"亏损: {pnl:.2f} {currency}{roi_str}"
        else:
            tie_trades.append(0.0)
            outcome = f"平手: +0.00 USDT{roi_str}"

        detail = (
            f"- [开仓: {open_time} -> 平仓: {close_time}] {sym} {side} ({side_zh}) | "
            f"开单价: {ep:.2f} | 平仓价: {cp:.2f} | 数量: {amt} | "
            f"TP: {tp_str} | SL: {sl_str} | {outcome} | 周期ID: {p.get('position_id', '模拟订单')} | "
            f"加仓: {p.get('add_count', 0)} 次 | 退出委托: {p.get('exit_count', 1)} 次 | "
            f"退出原因: {p.get('exit_reason') or '未知'} | 已知手续费（空值不代表无费用）: {json.dumps(p.get('fees', {}))} | "
            f"保护版本数: {len(p.get('protection_history', []))}"
        )
        if len(lines) <= 15:
            lines.append(detail)

    total_count = len(positions)
    win_count = len(win_trades)
    loss_count = len(loss_trades)
    decided_count = win_count + loss_count
    win_rate_str = f"{win_count / decided_count * 100:.1f}%" if decided_count > 0 else "-"

    total_profit = sum(win_trades)
    total_loss = sum(loss_trades)
    net_pnl = total_profit - total_loss

    avg_profit = (total_profit / win_count) if win_count > 0 else 0.0
    avg_loss = (total_loss / loss_count) if loss_count > 0 else 0.0
    known_count = win_count + loss_count + len(tie_trades)
    confirmed_pnl = f"{net_pnl:+.2f} {settlement}" if known_count else "-"

    if avg_loss > 0:
        plr_str = f"{avg_profit / avg_loss:.2f}"
    elif avg_profit > 0:
        plr_str = "∞ (全胜无亏损)"
    else:
        plr_str = "-"

    stats_line = (
        f"【{days}天战绩统计】\n"
        f"完整平仓周期: {total_count} 笔 | 胜率: {win_rate_str} ({win_count}胜 {loss_count}负"
        + (f" {len(tie_trades)}平" if tie_trades else "")
        + (f" {unknown_trades}笔盈亏未知" if unknown_trades else "")
        + f") | 盈亏比: {plr_str} | 平均盈利: +{avg_profit:.2f} {settlement} | 平均亏损: -{avg_loss:.2f} {settlement} | "
        f"已确认累计盈亏（手续费前）: {confirmed_pnl}"
    )

    if len(positions) > 15:
        lines.append(f'另有 {len(positions) - 15} 个周期未展示明细；统计包含全部周期。')
    return "\n".join(lines) + "\n\n" + stats_line


def get_history_pnl_stats_for_configs(symbol, config_ids):
    """Aggregate only the displayed task identities using their original sources."""
    normalized_ids = list(dict.fromkeys(
        str(value or "").strip() for value in config_ids or [] if str(value or "").strip()
    ))
    configs = [(config_id, _config_mode_for_stats(config_id)) for config_id in normalized_ids]
    return _statistics_store.history_for_configs(symbol, configs)


def get_history_pnl_stats(symbol, config_id='ALL'):
    """Preserve current-task filtering and the legacy no-current-task ALL query."""
    if config_id == 'ALL':
        try:
            from backend.config import config as global_config
            config_ids = [cfg.get("config_id") for cfg in global_config.get_configs_by_symbol(symbol) if cfg.get("config_id")]
        except storage.Error:
            raise
        except Exception:
            config_ids = []
        if config_ids:
            return get_history_pnl_stats_for_configs(symbol, config_ids)
        return _statistics_store.unscoped_history(symbol)
    return _statistics_store.history_for_configs(symbol, [(config_id, _config_mode_for_stats(config_id))])


def get_agent_trade_stats(config_id):
    """Get the existing order-log categories, preserving public error semantics."""
    with get_db_conn() as conn:
        try:
            return _statistics_store.agent_orders(config_id, connection=conn)
        except storage.Error:
            raise
        except Exception as exc:
            logger.error(f"❌ get_agent_trade_stats error: {exc}")
            return {"total_orders": 0, "error": str(exc)}


def get_financial_statistics_snapshot(symbol: str) -> dict:
    """Read the financial page data through the shared connection boundary."""
    return _statistics_store.financial_snapshot(symbol)


def get_config_dependency_counts(config_id: str):
    """统计指定 config_id 在各表中的依赖数量。"""
    return _config_cleanup_store.get_dependency_counts(config_id)


def soft_delete_config_runtime_data(config_id: str):
    """
    软删除策略对应的数据清理：
    - 清理强绑定运行态数据（会话、模拟账户、模拟余额）
    - 关闭仍处于 OPEN 的模拟单/决策单
    - 保留历史审计数据（orders/summaries/token_usage/daily_summaries）
    """
    return _config_cleanup_store.soft_delete_runtime_data(config_id)


def purge_config_all_data(config_id: str, *, connection: storage.Connection | None = None):
    """清理任务历史与运行数据，保留已接受交接所需的原执行证据。"""
    return _config_cleanup_store.purge_all_data(config_id, connection=connection)


def export_database_bytes() -> tuple[bytes, str]:
    """Export a PostgreSQL custom-format dump using the shared backup service."""
    from backend.storage.backup import export_database_bytes as export_dump
    return export_dump()
