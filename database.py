import os
import sqlite3
import uuid
from datetime import datetime
from contextlib import contextmanager
from utils.logger import setup_logger

# 使用绝对路径定位数据库文件
# 强制获取项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "trading_data.db")
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

def init_db():
    logger.info(f"🔍 正在检查数据库位置: {DB_NAME}")
    with get_db_conn() as conn:
        c = conn.cursor()
        # 开启 WAL 模式提高并发性能
        c.execute("PRAGMA journal_mode=WAL")
        
        # 1. Summaries 表
        c.execute('''CREATE TABLE IF NOT EXISTS summaries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        symbol TEXT,
                        agent_name TEXT, 
                        config_id TEXT,
                        agent_type TEXT,
                        timeframe TEXT,
                        content TEXT,
                        strategy_logic TEXT
                    )''')
        # 自动迁移：添加缺失列
        try: c.execute("ALTER TABLE summaries ADD COLUMN agent_name TEXT")
        except: pass
        try: c.execute("ALTER TABLE summaries ADD COLUMN config_id TEXT")
        except: pass
        try: c.execute("ALTER TABLE summaries ADD COLUMN agent_type TEXT")
        except: pass

        # 2. Mock Orders
        c.execute('''CREATE TABLE IF NOT EXISTS mock_orders (
                        order_id TEXT PRIMARY KEY,
                        timestamp TEXT,
                        symbol TEXT,
                        agent_name TEXT,
                        config_id TEXT,
                        side TEXT,
                        type TEXT,
                        price REAL,
                        amount REAL,
                        stop_loss REAL,
                        take_profit REAL,
                        expire_at REAL,
                        status TEXT DEFAULT 'OPEN'
                    )''')
        try: c.execute("ALTER TABLE mock_orders ADD COLUMN agent_name TEXT")
        except: pass
        try: c.execute("ALTER TABLE mock_orders ADD COLUMN config_id TEXT")
        except: pass
        try: c.execute("ALTER TABLE mock_orders ADD COLUMN expire_at REAL")
        except: pass

        # 3. Orders 表
        c.execute('''CREATE TABLE IF NOT EXISTS orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT,
                        timestamp TEXT,
                        symbol TEXT,
                        agent_name TEXT, 
                        config_id TEXT,
                        trade_mode TEXT,
                        side TEXT,
                        entry_price REAL,
                        take_profit REAL,
                        stop_loss REAL,
                        reason TEXT,
                        status TEXT DEFAULT 'OPEN'
                    )''')
        try: c.execute("ALTER TABLE orders ADD COLUMN trade_mode TEXT")
        except: pass
        try: c.execute("ALTER TABLE orders ADD COLUMN config_id TEXT")
        except: pass

        # 4. 账户净值历史 (用于画盈亏曲线)
        c.execute('''CREATE TABLE IF NOT EXISTS balance_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        symbol TEXT,
                        total_balance REAL,    -- 钱包余额
                        unrealized_pnl REAL,   -- 未实现盈亏
                        total_equity REAL      -- 净值 (余额+未实现)
                    )''')

        # 5. 实盘成交记录 (从交易所同步)
        c.execute('''CREATE TABLE IF NOT EXISTS trade_history (
                        trade_id TEXT PRIMARY KEY, -- 交易所的 trade id
                        timestamp TEXT,
                        symbol TEXT,
                        side TEXT,
                        price REAL,
                        amount REAL,
                        cost REAL,
                        fee REAL,
                        fee_currency TEXT,
                        realized_pnl REAL          -- 部分交易所支持返回该字段
                    )''')

        c.execute('''CREATE TABLE IF NOT EXISTS chat_sessions (
                        session_id TEXT PRIMARY KEY,
                        title TEXT,
                        config_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )''')

        # 7. LLM Token 使用统计
        c.execute('''CREATE TABLE IF NOT EXISTS token_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        symbol TEXT,
                        config_id TEXT,
                        model TEXT,
                        prompt_tokens INTEGER,
                        completion_tokens INTEGER,
                        total_tokens INTEGER
                    )''')

        # 8. 模型计价表 (单位: 美元/百万Token)
        c.execute('''CREATE TABLE IF NOT EXISTS model_pricing (
                        model TEXT PRIMARY KEY,
                        input_price_per_m REAL DEFAULT 0,
                        output_price_per_m REAL DEFAULT 0,
                        currency TEXT DEFAULT 'USD'
                    )''')

        # 9. 每日策略汇总表
        c.execute('''CREATE TABLE IF NOT EXISTS daily_summaries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        date TEXT,
                        symbol TEXT,
                        config_id TEXT,
                        summary TEXT,
                        source_count INTEGER DEFAULT 0,
                        created_at TEXT,
                        UNIQUE(date, config_id)
                    )''')

        conn.commit()

# --- 模型计价管理 ---

def get_all_pricing():
    """获取所有模型的计价信息"""
    with get_db_conn() as conn:
        c = conn.cursor()
        rows = c.execute("SELECT * FROM model_pricing").fetchall()
        return {r['model']: dict(r) for r in rows}

def update_model_pricing(model, input_price, output_price, currency='USD'):
    """更新或插入模型计价"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            INSERT INTO model_pricing (model, input_price_per_m, output_price_per_m, currency)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(model) DO UPDATE SET 
                input_price_per_m=excluded.input_price_per_m,
                output_price_per_m=excluded.output_price_per_m,
                currency=excluded.currency
        ''', (model, input_price, output_price, currency))
        conn.commit()

# --- 模拟交易 / 挂单池功能 ---

def save_token_usage(symbol, config_id, model, prompt_tokens, completion_tokens):
    """记录 LLM Token 使用情况"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    current_ts = datetime.now().timestamp()
    
    with get_db_conn() as conn:
        c = conn.cursor()
        # 基础查询：状态开启 + 未过期
        query = "SELECT * FROM mock_orders WHERE status='OPEN' AND (expire_at IS NULL OR expire_at > ?)"
        params = [current_ts]

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        
        # 🔥 隔离逻辑：优先匹配 config_id，兼容旧数据的 agent_name
        if config_id and agent_name:
            query += " AND (config_id = ? OR agent_name = ?)"
            params.extend([config_id, agent_name])
        elif config_id:
            query += " AND config_id = ?"
            params.append(config_id)
        elif agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)

        c.execute(query, tuple(params))
        return [dict(row) for row in c.fetchall()]

def create_mock_order(symbol, side, price, amount, stop_loss, take_profit, agent_name, config_id=None, order_id=None, expire_at=None):
    """创建模拟挂单 (必须传入 agent_name 和 config_id)"""
    if not order_id:
        order_id = f"ST-{uuid.uuid4().hex[:6]}"

    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO mock_orders (order_id, symbol, agent_name, config_id, side, price, amount, stop_loss, take_profit, timestamp, expire_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (order_id, symbol, agent_name, config_id or agent_name, side, price, amount, stop_loss, take_profit, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), expire_at))
            conn.commit()
        except Exception as e:
            logger.error(f"❌ DB Error (create_mock_order): {e}")

def cancel_mock_order(order_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM mock_orders WHERE order_id = ?", (order_id,))
        c.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = ?", (order_id,))
        conn.commit()


def save_order_log(order_id, symbol, agent_name, side, entry, tp, sl, reason, trade_mode="STRATEGY", config_id=None):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 确保 trade_mode 格式统一
    if trade_mode == "REAL":
        valid_mode = "REAL"
    elif trade_mode == "SPOT_DCA":
        valid_mode = "SPOT_DCA"
    else:
        valid_mode = "STRATEGY"
    
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO orders (order_id, timestamp, symbol, agent_name, config_id, side, entry_price, take_profit, stop_loss, reason, trade_mode) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(order_id), timestamp, symbol, str(agent_name), config_id or str(agent_name), side, entry, tp, sl, reason, valid_mode))
        conn.commit()

# --- 数据分析与记录 ---

def save_summary(symbol, agent_name, content, strategy_logic, config_id=None, agent_type=None):
    """保存 AI 分析结果"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO summaries (timestamp, symbol, timeframe, agent_name, config_id, agent_type, content, strategy_logic) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (timestamp, symbol, "15m", agent_name, config_id or agent_name, agent_type, content, strategy_logic))
        conn.commit()

def get_active_agents(symbol):
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            # 获取该币种下所有不为空的 agent_name
            rows = c.execute("SELECT DISTINCT agent_name FROM summaries WHERE symbol = ? AND agent_name IS NOT NULL", (symbol,)).fetchall()
            return [r[0] for r in rows if r[0]]
        except:
            return []

def get_recent_summaries(symbol, agent_name=None, limit=10, config_id=None, agent_type=None):
    """获取最近的分析记录 (支持 agent_name, config_id 或 agent_type 隔离)"""
    with get_db_conn() as conn:
        c = conn.cursor()
        if agent_type:
             # 新增：按 agent_type 隔离（用于事件合约）
            c.execute("""
                SELECT * FROM summaries 
                WHERE symbol = ? AND agent_type = ? 
                ORDER BY id DESC LIMIT ?
            """, (symbol, agent_type, limit))
        elif config_id:
            # 优先使用 config_id
            c.execute("""
                SELECT * FROM summaries 
                WHERE symbol = ? AND config_id = ? 
                ORDER BY id DESC LIMIT ?
            """, (symbol, config_id, limit))
        elif agent_name:
            # 退而求其次使用 agent_name
            c.execute("""
                SELECT * FROM summaries 
                WHERE symbol = ? AND agent_name = ? 
                ORDER BY id DESC LIMIT ?
            """, (symbol, agent_name, limit))
        else:
            # 兼容旧逻辑或全局查看
            c.execute("""
                SELECT * FROM summaries 
                WHERE symbol = ? 
                ORDER BY id DESC LIMIT ?
            """, (symbol, limit))
            
        return [dict(row) for row in c.fetchall()]

def get_summary_count(symbol, agent_name=None):
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            sql = "SELECT COUNT(*) FROM summaries WHERE symbol = ?"
            params = [symbol]
            
            if agent_name and agent_name != 'ALL':
                sql += " AND agent_name = ?"
                params.append(agent_name)
                
            return c.execute(sql, tuple(params)).fetchone()[0]
        except:
            return 0

def get_paginated_summaries(symbol, page=1, per_page=10, agent_name=None):
    offset = (page - 1) * per_page
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            # 动态构建 SQL
            sql = "SELECT * FROM summaries WHERE symbol = ?"
            params = [symbol]

            if agent_name and agent_name != 'ALL':
                sql += " AND agent_name = ?"
                params.append(agent_name)

            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([per_page, offset])

            c.execute(sql, tuple(params))
            return [dict(row) for row in c.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get paginated summaries: symbol={symbol}, page={page}, per_page={per_page}, agent={agent_name}, error={e}")
            return []

def delete_summaries_by_symbol(symbol):
    """删除指定币种的所有分析历史和决策流水"""
    with get_db_conn() as conn:
        c = conn.cursor()
        # 1. 删除分析历史
        c.execute("DELETE FROM summaries WHERE symbol = ?", (symbol,))
        s_count = c.rowcount
        # 2. 删除决策流水 (日志)
        c.execute("DELETE FROM orders WHERE symbol = ?", (symbol,))
        o_count = c.rowcount
        # 3. 删除模拟挂单
        c.execute("DELETE FROM mock_orders WHERE symbol = ?", (symbol,))

        conn.commit()
        logger.info(f"🗑️ Cleaned {symbol}: {s_count} summaries, {o_count} orders.")
        return s_count

def save_balance_snapshot(symbol, balance, unrealized_pnl):
    """记录资金快照"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    equity = balance + unrealized_pnl
    
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            INSERT INTO balance_history (timestamp, symbol, total_balance, unrealized_pnl, total_equity)
            VALUES (?, ?, ?, ?, ?)
        ''', (timestamp, symbol, balance, unrealized_pnl, equity))
        conn.commit()

def get_paginated_orders(config_id, page=1, per_page=10):
    """获取分页决策流水 (支持 Agent 隔离)"""
    offset = (page - 1) * per_page
    with get_db_conn() as conn:
        c = conn.cursor()
        # 兼容旧数据的 config_id 逻辑
        query = "SELECT * FROM orders WHERE config_id = ? ORDER BY id DESC LIMIT ? OFFSET ?"
        c.execute(query, (config_id, per_page, offset))
        orders = [dict(row) for row in c.fetchall()]
        
        c.execute("SELECT COUNT(*) FROM orders WHERE config_id = ?", (config_id,))
        total = c.fetchone()[0]
        return orders, total

def get_balance_history(symbol, limit=100):
    """获取资金曲线数据"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM balance_history WHERE symbol = ? ORDER BY id ASC LIMIT ?", (symbol, limit))
        return [dict(row) for row in c.fetchall()]


def save_trade_history(trades):
    """批量保存成交记录 (会自动忽略已存在的 trade_id)"""
    if not trades: return
    with get_db_conn() as conn:
        c = conn.cursor()
        for t in trades:
            try:
                # 1. 尝试从 CCXT 根对象获取 (有些交易所支持)
                pnl = t.get('realizedPnl')
                
                # 2. 如果没有，去 'info' (交易所原始响应) 里找 (Binance 在这里)
                if pnl is None and 'info' in t:
                    pnl = t['info'].get('realizedPnl')
                
                # 3. 还是没有，就默认为 0
                if pnl is None:
                    pnl = 0

                # 4. 手续费处理
                fee_cost = 0
                fee_currency = ''
                if t.get('fee'):
                    fee_cost = float(t['fee'].get('cost', 0) or 0)
                    fee_currency = t['fee'].get('currency', '')

                c.execute('''
                    INSERT OR IGNORE INTO trade_history 
                    (trade_id, timestamp, symbol, side, price, amount, cost, fee, fee_currency, realized_pnl)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    str(t['id']), 
                    datetime.fromtimestamp(t['timestamp']/1000).strftime('%Y-%m-%d %H:%M:%S'),
                    t['symbol'],
                    t['side'],
                    float(t['price']),
                    float(t['amount']),
                    float(t['cost']),
                    fee_cost,
                    fee_currency,
                    float(pnl)
                ))
            except Exception as e:
                logger.error(f"Save trade error: {e}")
        conn.commit()

def get_trade_history(symbol, limit=50):
    """获取历史成交"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM trade_history WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?", (symbol, limit))
        return [dict(row) for row in c.fetchall()]

def clean_financial_data(symbol):
    """删除指定币种的资金和成交记录 (用于重置)"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM balance_history WHERE symbol = ?", (symbol,))
        c1 = c.rowcount
        c.execute("DELETE FROM trade_history WHERE symbol = ?", (symbol,))
        c2 = c.rowcount
        conn.commit()
        return c1 + c2


def create_chat_session(session_id: str, config_id: str, symbol: str, title: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            '''
            INSERT INTO chat_sessions (session_id, title, config_id, symbol, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (session_id, title, config_id, symbol, now, now),
        )
        conn.commit()


def touch_chat_session(session_id: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        conn.commit()


def get_chat_session(session_id: str):
    with get_db_conn() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT * FROM chat_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None


def get_chat_sessions(limit: int = 100):
    with get_db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT * FROM chat_sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def update_chat_session_title(session_id: str, title: str):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE chat_sessions SET title = ? WHERE session_id = ?",
            (title, session_id),
        )
        conn.commit()


def delete_chat_session(session_id: str) -> int:
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
        deleted = c.rowcount
        conn.commit()
        return deleted


def delete_chat_sessions(session_ids):
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return 0
    with get_db_conn() as conn:
        c = conn.cursor()
        placeholders = ",".join(["?"] * len(ids))
        c.execute(f"DELETE FROM chat_sessions WHERE session_id IN ({placeholders})", tuple(ids))
        deleted = c.rowcount
        conn.commit()
        return deleted

# --- 每日策略汇总 ---

def save_daily_summary(date_str, symbol, config_id, summary, source_count):
    """保存或更新某天某 config 的每日策略汇总"""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO daily_summaries (date, symbol, config_id, summary, source_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, config_id) DO UPDATE SET
                    summary = excluded.summary,
                    source_count = excluded.source_count,
                    created_at = excluded.created_at
            ''', (date_str, symbol, config_id, summary, source_count, created_at))
            conn.commit()
        except Exception as e:
            logger.error(f"❌ DB Error (save_daily_summary): {e}")


def get_daily_summaries(config_id, days=7):
    """获取最近 N 天的每日策略汇总（按日期倒序）"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            SELECT date, symbol, config_id, summary, source_count, created_at
            FROM daily_summaries
            WHERE config_id = ?
            ORDER BY date DESC
            LIMIT ?
        ''', (config_id, days))
        return [dict(row) for row in c.fetchall()]


def get_pending_daily_summary_data(config_id, date_str):
    """获取指定日期、指定 config 的所有 strategy_logic 原文（用于 LLM 汇总）"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            SELECT strategy_logic, timestamp
            FROM summaries
            WHERE config_id = ? AND date(timestamp) = ?
            ORDER BY id ASC
        ''', (config_id, date_str))
        return [dict(row) for row in c.fetchall()]


if __name__ == "__main__":
    init_db()
    logger.info("Database initialized.")
