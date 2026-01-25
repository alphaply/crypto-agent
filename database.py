import sqlite3
import uuid
from datetime import datetime

DB_NAME = "trading_data.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. Summaries 表 (已包含 agent_name)
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

    # 2. Mock Orders 表
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

    # 3. Orders 日志表 (核心修改：增加 agent_name)
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT,
                    timestamp TEXT,
                    symbol TEXT,
                    agent_name TEXT,  -- 新增字段
                    side TEXT,
                    entry_price REAL,
                    take_profit REAL,
                    stop_loss REAL,
                    reason TEXT,
                    status TEXT DEFAULT 'OPEN'
                )''')
    
    # [自动迁移] 尝试给旧的 orders 表添加 agent_name
    try:
        c.execute("ALTER TABLE orders ADD COLUMN agent_name TEXT")
    except sqlite3.OperationalError:
        pass 

    conn.commit()
    conn.close()

# --- 这里只列出修改过的 save_order_log，其他函数保持不变 ---

def save_order_log(order_id, symbol, agent_name, side, entry, tp, sl, reason):
    """
    更新：接收 agent_name
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 插入时记录 agent_name
    c.execute("""
        INSERT INTO orders (order_id, timestamp, symbol, agent_name, side, entry_price, take_profit, stop_loss, reason) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (order_id, timestamp, symbol, agent_name, side, entry, tp, sl, reason))
    conn.commit()
    conn.close()

# 为了兼容旧代码，其他读取函数逻辑基本不变，
# 但 get_db_data 的逻辑我们移到 dashboard.py 里灵活处理