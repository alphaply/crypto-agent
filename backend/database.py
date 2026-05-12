import os
import sqlite3
import uuid
import pytz
from datetime import datetime, timedelta
import json
from contextlib import contextmanager
from backend.database_balance import BalanceHistoryStore
from backend.database_chat import ChatSessionStore
from backend.database_cleanup import ConfigCleanupStore
from backend.database_dca import DcaSnapshotStore
from backend.database_memory import SummaryMemoryStore
from backend.database_mock import MockTradingStore
from backend.database_orders import OrderPersistenceStore
from backend.database_position import PositionHistoryStore
from backend.database_pricing import PricingStore
from backend.database_summary import SummaryStore
from backend.database_trade import TradeHistoryStore
from backend.storage_paths import DATA_DIR, PROJECT_ROOT, data_file
from backend.database_schema import initialize_schema
from backend.utils.logger import setup_logger

# 设置时区
TZ_CN = pytz.timezone('Asia/Shanghai')

# 使用绝对路径定位数据库文件
# 强制获取项目根目录
BASE_DIR = str(PROJECT_ROOT)
DB_NAME = str(data_file("TRADING_DB_PATH", "trading_data.db"))
logger = setup_logger("Database")

@contextmanager
def get_db_conn():
    """数据库连接上下文管理器，自动处理关闭和超时"""
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _initialize_runtime_config() -> None:
    try:
        from backend.config_store import ensure_runtime_config_initialized

        ensure_runtime_config_initialized()
    except Exception as e:
        logger.error(f"❌ 初始化 SQLite 运行配置失败: {e}")


def _reload_runtime_config() -> None:
    try:
        from backend.config import config as runtime_config

        runtime_config.reload_config()
    except Exception as e:
        logger.error(f"❌ 重新加载运行配置失败: {e}")


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

def init_db():
    logger.info(f"🔍 正在检查数据库位置: {DB_NAME}")
    with get_db_conn() as conn:
        initialize_schema(conn)

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

def get_mock_equity_history(config_id, days=30):
    """获取指定策略模拟账户的资金曲线（按天聚合的最后一条），最多保留 30 天。
    优先使用 total_equity (钱包余额+未实现盈亏)，旧数据回退 balance。
    """
    return _mock_trading_store.get_equity_history(config_id, days=days)

def save_token_usage(symbol, config_id, model, prompt_tokens, completion_tokens):
    """记录 LLM Token 使用情况"""
    timestamp = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    total_tokens = prompt_tokens + completion_tokens
    
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO token_usage (timestamp, symbol, config_id, model, prompt_tokens, completion_tokens, total_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (timestamp, symbol, config_id, model, prompt_tokens, completion_tokens, total_tokens))
            conn.commit()
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
    _mock_trading_store.close_order(order_id, close_price=close_price, realized_pnl=realized_pnl)


def save_order_log(order_id, symbol, agent_name, side, entry, tp, sl, reason, trade_mode="STRATEGY", config_id=None, amount=0, status="OPEN"):
    _order_persistence_store.save_order_log(order_id, symbol, agent_name, side, entry, tp, sl, reason, trade_mode=trade_mode, config_id=config_id, amount=amount, status=status)


def update_order_fill_status(order_id, status, filled_qty=0.0, filled_cost=0.0, avg_fill_price=0.0, filled_at=None):
    """更新 orders 表的成交状态信息（主要用于 SPOT_DCA）。"""
    _order_persistence_store.update_fill_status(order_id, status, filled_qty=filled_qty, filled_cost=filled_cost, avg_fill_price=avg_fill_price, filled_at=filled_at)


def upsert_spot_order_fill(order_id, config_id, symbol, status, filled_qty=0.0, filled_cost=0.0, avg_fill_price=0.0, filled_at=None):
    """写入或更新现货订单成交同步状态。"""
    _order_persistence_store.upsert_spot_fill(order_id, config_id, symbol, status, filled_qty=filled_qty, filled_cost=filled_cost, avg_fill_price=avg_fill_price, filled_at=filled_at)


def save_dca_daily_snapshot(config_id, symbol, stats):
    """按天保存 DCA 统计快照（同一天覆盖更新）。"""
    _dca_snapshot_store.save_snapshot(config_id, symbol, stats)


def get_dca_daily_snapshot_history(config_id, days=30):
    """获取最近 N 天 DCA 快照曲线。"""
    return _dca_snapshot_store.get_snapshot_history(config_id, days=days)

# --- 数据分析与记录 ---

def save_summary(symbol, agent_name, content, strategy_logic, config_id=None, agent_type=None):
    """保存 AI 分析结果"""
    _summary_store.save_summary(symbol, agent_name, content, strategy_logic, config_id=config_id, agent_type=agent_type)

def get_active_agents(symbol):
    return _summary_store.get_active_agents(symbol)

def get_recent_summaries(symbol, agent_name=None, limit=10, config_id=None, agent_type=None):
    """获取最近的分析记录 (支持 agent_name, config_id 或 agent_type 隔离)"""
    return _summary_store.get_recent_summaries(symbol, agent_name=agent_name, limit=limit, config_id=config_id, agent_type=agent_type)

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


def create_chat_session(session_id: str, config_id: str, symbol: str, title: str):
    _chat_session_store.create_session(session_id, config_id, symbol, title)


def touch_chat_session(session_id: str):
    _chat_session_store.touch_session(session_id)


def get_chat_session(session_id: str):
    return _chat_session_store.get_session(session_id)


def get_chat_sessions(limit: int = 100):
    return _chat_session_store.list_sessions(limit=limit)


def update_chat_session_title(session_id: str, title: str):
    _chat_session_store.update_title(session_id, title)


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


def save_short_memory(bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count):
    _summary_memory_store.save_short_memory(bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count)


def get_short_memories(config_id, limit=2):
    return _summary_memory_store.get_short_memories(config_id, limit=limit)


def list_short_memories(symbol=None, config_id=None, limit=200):
    return _summary_memory_store.list_short_memories(symbol=symbol, config_id=config_id, limit=limit)


def get_short_memory(config_id, bucket_start):
    return _summary_memory_store.get_short_memory(config_id, bucket_start)


def update_short_memory(config_id, bucket_start, market_summary, position_summary):
    return _summary_memory_store.update_short_memory(config_id, bucket_start, market_summary, position_summary)


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


def get_history_pnl_stats(symbol, config_id='ALL'):
    """获取标的的盈亏统计，整合实盘和模拟盘数据"""
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        realized_pnls = []
        
        if config_id == 'ALL':
            trades = c.execute("SELECT realized_pnl FROM trade_history WHERE symbol LIKE ? AND realized_pnl IS NOT NULL AND realized_pnl != 0", (symbol + '%',)).fetchall()
            realized_pnls.extend([t['realized_pnl'] for t in trades])
            
            mocks = c.execute("SELECT realized_pnl FROM mock_orders WHERE symbol = ? AND status='CLOSED' AND realized_pnl IS NOT NULL", (symbol,)).fetchall()
            realized_pnls.extend([m['realized_pnl'] for m in mocks])
        else:
            mocks = c.execute("SELECT realized_pnl FROM mock_orders WHERE symbol = ? AND config_id = ? AND status='CLOSED' AND realized_pnl IS NOT NULL", (symbol, config_id)).fetchall()
            realized_pnls.extend([m['realized_pnl'] for m in mocks])
            
            # 如果是 REAL 模式，把 trade_history 也加上 (因为目前 trade_history 没有 config_id 字段)
            # 先查一下这个 config_id 的模式
            try:
                from backend.config import config as global_config
                cfg = global_config.get_config_by_id(config_id)
                if cfg and cfg.get('mode', '').upper() == 'REAL':
                    trades = c.execute(
                        """
                        SELECT realized_pnl
                        FROM trade_history
                        WHERE symbol LIKE ?
                          AND config_id = ?
                          AND realized_pnl IS NOT NULL
                          AND realized_pnl != 0
                        """,
                        (symbol + '%', config_id),
                    ).fetchall()
                    realized_pnls.extend([t['realized_pnl'] for t in trades])
            except Exception:
                pass
            
        total_pnl = sum(realized_pnls)
        win_trades = [p for p in realized_pnls if p > 0]
        lose_trades = [p for p in realized_pnls if p < 0]
        
        total_count = len(realized_pnls)
        win_rate = (len(win_trades) / total_count * 100) if total_count > 0 else 0
        
        return {
            "total_trades": total_count,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "win_count": len(win_trades),
            "lose_count": len(lose_trades)
        }

# --- Agent 做单统计 ---

def get_agent_trade_stats(config_id):
    """获取指定 Agent 的做单统计 (从 orders 表聚合)"""
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            # 总订单数
            total = c.execute(
                "SELECT COUNT(*) FROM orders WHERE config_id = ?", (config_id,)
            ).fetchone()[0]

            if total == 0:
                return {
                    "total_orders": 0, "buy_count": 0, "sell_count": 0,
                    "cancel_count": 0, "close_count": 0,
                    "long_short_ratio": "N/A", "cancel_rate": 0,
                    "first_order_at": None, "last_order_at": None,
                }

            # 分类计数
            rows = c.execute("""
                SELECT 
                    SUM(CASE WHEN LOWER(side) LIKE '%buy%' THEN 1 ELSE 0 END) as buy_count,
                    SUM(CASE WHEN LOWER(side) LIKE '%sell%' THEN 1 ELSE 0 END) as sell_count,
                    SUM(CASE WHEN LOWER(side) LIKE '%cancel%' THEN 1 ELSE 0 END) as cancel_count,
                    SUM(CASE WHEN LOWER(side) LIKE '%close%' THEN 1 ELSE 0 END) as close_count,
                    MIN(timestamp) as first_order_at,
                    MAX(timestamp) as last_order_at
                FROM orders WHERE config_id = ?
            """, (config_id,)).fetchone()

            buy = rows['buy_count'] or 0
            sell = rows['sell_count'] or 0
            cancel = rows['cancel_count'] or 0
            close = rows['close_count'] or 0

            # 有效开仓单 = buy + sell (排除 cancel 和 close)
            open_orders = buy + sell
            ls_ratio = "N/A"
            if sell > 0:
                ls_ratio = f"{round(buy / sell, 2)}"
            elif buy > 0:
                ls_ratio = "∞ (纯多)"

            cancel_rate = round(cancel / total * 100, 1) if total > 0 else 0

            return {
                "total_orders": total,
                "buy_count": buy,
                "sell_count": sell,
                "cancel_count": cancel,
                "close_count": close,
                "long_short_ratio": ls_ratio,
                "cancel_rate": cancel_rate,
                "first_order_at": rows['first_order_at'],
                "last_order_at": rows['last_order_at'],
            }
        except Exception as e:
            logger.error(f"❌ get_agent_trade_stats error: {e}")
            return {"total_orders": 0, "error": str(e)}


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


def purge_config_all_data(config_id: str):
    """彻底删除指定 config_id 的历史与运行数据，避免历史页残留。"""
    return _config_cleanup_store.purge_all_data(config_id)


if __name__ == "__main__":
    init_db()
    logger.info("Database initialized.")
