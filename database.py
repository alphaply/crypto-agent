import sqlite3
import uuid
from datetime import datetime

DB_NAME = "trading_data.db"

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

    # 3. Orders 日志表 (历史记录)
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT,
                    timestamp TEXT,
                    symbol TEXT,
                    agent_name TEXT, 
                    side TEXT,
                    entry_price REAL,
                    take_profit REAL,
                    stop_loss REAL,
                    reason TEXT,
                    status TEXT DEFAULT 'OPEN'
                )''')
    try:
        c.execute("ALTER TABLE orders ADD COLUMN agent_name TEXT")
    except sqlite3.OperationalError:
        pass 

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

def create_mock_order(symbol, side, price, amount, sl, tp):
    """创建一个模拟挂单"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    order_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    c.execute("""
        INSERT INTO mock_orders (order_id, timestamp, symbol, side, type, price, amount, stop_loss, take_profit)
        VALUES (?, ?, ?, ?, 'LIMIT', ?, ?, ?, ?)
    """, (order_id, timestamp, symbol, side, price, amount, sl, tp))
    
    conn.commit()
    conn.close()
    return order_id

def cancel_mock_order(order_id):
    """撤销模拟挂单"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM mock_orders WHERE order_id = ?", (order_id,))
    c.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()

# --- 日志与分析功能 ---

def save_order_log(order_id, symbol, agent_name, side, entry, tp, sl, reason):
    """保存订单日志 (包括 agent_name)"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 确保 agent_name 是字符串
    agent_str = str(agent_name) if agent_name else "Unknown"

    c.execute("""
        INSERT INTO orders (order_id, timestamp, symbol, agent_name, side, entry_price, take_profit, stop_loss, reason) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (order_id, timestamp, symbol, agent_str, side, entry, tp, sl, reason))
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

if __name__ == "__main__":
    init_db()
    print("Database initialized.")