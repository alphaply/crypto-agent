import sqlite3
import uuid
from datetime import datetime
from utils.logger import setup_logger

DB_NAME = "trading_data.db"
logger = setup_logger("Database")

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. Summaries 表
    c.execute('''CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    symbol TEXT,
                    agent_name TEXT, 
                    timeframe TEXT,
                    content TEXT,
                    strategy_logic TEXT
                )''')
    try:
        c.execute("ALTER TABLE summaries ADD COLUMN agent_name TEXT")
    except: pass

    c.execute('''CREATE TABLE IF NOT EXISTS mock_orders (
                    order_id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    symbol TEXT,
                    agent_name TEXT,      -- 隔离字段
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
    try: c.execute("ALTER TABLE mock_orders ADD COLUMN expire_at REAL")
    except: pass

    # 3. Orders 表 (历史订单/日志) - 包含 trade_mode
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT,
                    timestamp TEXT,
                    symbol TEXT,
                    agent_name TEXT, 
                    trade_mode TEXT,  -- 'REAL' 或 'STRATEGY'
                    side TEXT,
                    entry_price REAL,
                    take_profit REAL,
                    stop_loss REAL,
                    reason TEXT,
                    status TEXT DEFAULT 'OPEN'
                )''')
    try:
        c.execute("ALTER TABLE orders ADD COLUMN trade_mode TEXT")
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

    conn.commit()
    conn.close()

# --- 模拟交易 / 挂单池功能 ---

def get_mock_orders(symbol=None, agent_name=None):
    """
    获取活跃模拟挂单 (支持 Agent 隔离)
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    current_ts = datetime.now().timestamp()
    
    # 基础查询：状态开启 + 未过期
    query = "SELECT * FROM mock_orders WHERE status='OPEN' AND (expire_at IS NULL OR expire_at > ?)"
    params = [current_ts]

    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    
    # 🔥 隔离逻辑：如果传入 agent_name，则只查该 Agent 的单
    if agent_name:
        query += " AND agent_name = ?"
        params.append(agent_name)

    c.execute(query, tuple(params))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows

def create_mock_order(symbol, side, price, amount, stop_loss, take_profit, agent_name, order_id=None, expire_at=None):
    """
    创建模拟挂单 (必须传入 agent_name)
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    if not order_id:
        order_id = f"ST-{uuid.uuid4().hex[:6]}"

    try:
        c.execute('''
            INSERT INTO mock_orders (order_id, symbol, agent_name, side, price, amount, stop_loss, take_profit, timestamp, expire_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (order_id, symbol, agent_name, side, price, amount, stop_loss, take_profit, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), expire_at))
        conn.commit()
    except Exception as e:
        logger.error(f"❌ DB Error (create_mock_order): {e}")
    finally:
        conn.close()

def cancel_mock_order(order_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM mock_orders WHERE order_id = ?", (order_id,))
    c.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()


def save_order_log(order_id, symbol, agent_name, side, entry, tp, sl, reason, trade_mode="STRATEGY"):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 确保 trade_mode 格式统一
    valid_mode = "REAL" if trade_mode == "REAL" else "STRATEGY"
    
    c.execute("""
        INSERT INTO orders (order_id, timestamp, symbol, agent_name, side, entry_price, take_profit, stop_loss, reason, trade_mode) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(order_id), timestamp, symbol, str(agent_name), side, entry, tp, sl, reason, valid_mode))
    conn.commit()
    conn.close()

# --- 数据分析与记录 ---

def save_summary(symbol, agent_name, content, strategy_logic):
    """保存 AI 分析结果"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    c.execute("""
        INSERT INTO summaries (timestamp, symbol, timeframe, agent_name, content, strategy_logic) 
        VALUES (?, ?, ?, ?, ?, ?)
    """, (timestamp, symbol, "15m", agent_name, content, strategy_logic))
    
    conn.commit()
    conn.close()


def get_recent_summaries(symbol, agent_name=None, limit=10):
    """获取最近的分析记录 (增加 agent_name 隔离)"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    if agent_name:
        # 🔥 核心修改：增加 AND agent_name = ?
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
        
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows
def get_summary_count(symbol):
    """获取某币种的分析记录总数"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        count = c.execute("SELECT COUNT(*) FROM summaries WHERE symbol = ?", (symbol,)).fetchone()[0]
    except:
        count = 0
    conn.close()
    return count

def get_paginated_summaries(symbol, page=1, per_page=10):
    """分页获取分析历史"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    offset = (page - 1) * per_page
    c = conn.cursor()
    c.execute(
        "SELECT * FROM summaries WHERE symbol = ? ORDER BY id DESC LIMIT ? OFFSET ?", 
        (symbol, per_page, offset)
    )
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows

def delete_summaries_by_symbol(symbol):
    """删除指定币种的所有分析历史"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM summaries WHERE symbol = ?", (symbol,))
    deleted_count = c.rowcount
    conn.commit()
    conn.close()
    return deleted_count


def save_balance_snapshot(symbol, balance, unrealized_pnl):
    """记录资金快照"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    equity = balance + unrealized_pnl
    
    c.execute('''
        INSERT INTO balance_history (timestamp, symbol, total_balance, unrealized_pnl, total_equity)
        VALUES (?, ?, ?, ?, ?)
    ''', (timestamp, symbol, balance, unrealized_pnl, equity))
    conn.commit()
    conn.close()

def get_balance_history(symbol, limit=100):
    """获取资金曲线数据"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM balance_history WHERE symbol = ? ORDER BY id ASC LIMIT ?", (symbol, limit))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows


def save_trade_history(trades):
    """批量保存成交记录 (会自动忽略已存在的 trade_id)"""
    if not trades: return
    conn = sqlite3.connect(DB_NAME)
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
    conn.close()

def get_trade_history(symbol, limit=50):
    """获取历史成交"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM trade_history WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?", (symbol, limit))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows

def clean_financial_data(symbol):
    """删除指定币种的资金和成交记录 (用于重置)"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM balance_history WHERE symbol = ?", (symbol,))
    c1 = c.rowcount
    c.execute("DELETE FROM trade_history WHERE symbol = ?", (symbol,))
    c2 = c.rowcount
    conn.commit()
    conn.close()
    return c1 + c2

if __name__ == "__main__":
    init_db()
    logger.info("Database initialized.")