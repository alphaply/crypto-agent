import sqlite3
import uuid
from datetime import datetime

DB_NAME = "trading_data.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. 行情总结 (保持不变)
    c.execute('''CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    symbol TEXT,
                    timeframe TEXT,
                    content TEXT,
                    strategy_logic TEXT
                )''')
    
    # 2. 模拟挂单表 (Mock Orders - 活跃池)
    # 这个表只存"当前有效的单子"，撤单或成交后会删除
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
                
    # 3. 订单日志表 (Orders Log - 永久存档) <--- 修复：加回这个表给 Dashboard 用
# 修改 database.py 中的 orders 建表语句
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT,  -- 增加这一行，存储 uuid
                    timestamp TEXT,
                    symbol TEXT,
                    side TEXT,
                    entry_price REAL,
                    take_profit REAL,
                    stop_loss REAL,
                    reason TEXT,
                    status TEXT DEFAULT 'OPEN' -- 增加状态：OPEN, CANCELLED, FILLED
                )''')
                
    conn.commit()
    conn.close()

# --- 模拟交易核心功能 ---

def get_mock_orders(symbol=None):
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
    """撤销模拟挂单并同步更新日志状态"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # 1. 从活跃池删除
    c.execute("DELETE FROM mock_orders WHERE order_id = ?", (order_id,))
    # 2. 更新日志池状态 (将对应的订单标记为 CANCELLED)
    c.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()

# --- 日志功能 ---

def save_order_log(symbol, side, entry, tp, sl, reason):
    """<--- 修复：专门给 Dashboard 看的日志记录"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO orders (timestamp, symbol, side, entry_price, take_profit, stop_loss, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (timestamp, symbol, side, entry, tp, sl, reason))
    conn.commit()
    conn.close()

def save_summary(symbol, content, strategy_logic):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO summaries (timestamp, symbol, timeframe, content, strategy_logic) VALUES (?, ?, ?, ?, ?)",
              (timestamp, symbol, "15m", content, strategy_logic))
    conn.commit()
    conn.close()

def get_recent_summaries(symbol, limit=3):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM summaries WHERE symbol = ? ORDER BY id DESC LIMIT ?", (symbol, limit))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows

# 初始化
init_db()