import sqlite3
import uuid
from datetime import datetime
from tool.logger import setup_logger

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

    # 2. Mock Orders 表 (活跃挂单池)
    c.execute('''CREATE TABLE IF NOT EXISTS mock_orders (
                    order_id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    symbol TEXT,
                    side TEXT,
                    type TEXT,
                    price REAL,
                    amount REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    status TEXT DEFAULT 'OPEN'
                )''')

# 3. 修改 Orders 表，增加 trade_mode
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT,
                    timestamp TEXT,
                    symbol TEXT,
                    agent_name TEXT, 
                    trade_mode TEXT,  -- 新增: 'REAL_EXEC' 或 'STRATEGY_IDEA'
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
    conn.commit()
    conn.close()

# --- 模拟交易 / 挂单池功能 ---

def get_mock_orders(symbol=None):
    """获取当前活跃的模拟挂单"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if symbol:
        c.execute("SELECT * FROM mock_orders WHERE symbol = ? AND status='OPEN'", (symbol,))
    else:
        c.execute("SELECT * FROM mock_orders WHERE status='OPEN'")
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows

# database.py

def create_mock_order(symbol, side, price, amount, stop_loss, take_profit, order_id=None):
    """
    创建一个模拟挂单
    新增参数: order_id (必须传入，保证和日志一致)
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    if not order_id:
        import uuid
        order_id = f"ST-{uuid.uuid4().hex[:6]}"

    try:
        c.execute('''
            INSERT INTO mock_orders (order_id, symbol, side, price, amount, stop_loss, take_profit, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (order_id, symbol, side, price, amount, stop_loss, take_profit, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
    except Exception as e:
        logger.error(f"❌ DB Error (create_mock_order): {e}")
    finally:
        conn.close()

def cancel_mock_order(order_id):
    """撤销模拟挂单"""
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
    
    # 确保 trade_mode 只有两种合法值，方便前端展示
    valid_mode = "REAL" if trade_mode == "REAL" else "STRATEGY"
    
    c.execute("""
        INSERT INTO orders (order_id, timestamp, symbol, agent_name, side, entry_price, take_profit, stop_loss, reason, trade_mode) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(order_id), timestamp, symbol, str(agent_name), side, entry, tp, sl, reason, valid_mode))
    conn.commit()
    conn.close()

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

def get_recent_summaries(symbol, limit=10):
    """获取最近的分析记录"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM summaries WHERE symbol = ? ORDER BY id DESC LIMIT ?", (symbol, limit))
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
    # 按时间倒序排列
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


if __name__ == "__main__":
    init_db()
    logger.info("Database initialized.")