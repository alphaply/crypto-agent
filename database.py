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

    # 2. Mock Orders 表 (活跃挂单池) - 增加 agent_name
    c.execute('''CREATE TABLE IF NOT EXISTS mock_orders (
                    order_id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    symbol TEXT,
                    agent_name TEXT,      -- 新增: 策略名称隔离
                    side TEXT,
                    type TEXT,
                    price REAL,
                    amount REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    expire_at REAL,
                    status TEXT DEFAULT 'OPEN'
                )''')
    try:
        c.execute("ALTER TABLE mock_orders ADD COLUMN agent_name TEXT")
    except: pass

    # 3. Orders 表 (历史订单/日志)
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT,
                    timestamp TEXT,
                    symbol TEXT,
                    agent_name TEXT, 
                    trade_mode TEXT,
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

    # 4. 账户净值历史
    c.execute('''CREATE TABLE IF NOT EXISTS balance_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    symbol TEXT,
                    total_balance REAL,
                    unrealized_pnl REAL,
                    total_equity REAL
                )''')

    # 5. 实盘成交记录
    c.execute('''CREATE TABLE IF NOT EXISTS trade_history (
                    trade_id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    symbol TEXT,
                    side TEXT,
                    price REAL,
                    amount REAL,
                    cost REAL,
                    fee REAL,
                    fee_currency TEXT,
                    realized_pnl REAL
                )''')

    conn.commit()
    conn.close()

# --- 模拟交易 / 挂单池功能 ---

def get_mock_orders(symbol=None, agent_name=None):
    """
    获取当前活跃的模拟挂单 (增加 agent_name 过滤)
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    current_ts = datetime.now().timestamp()
    
    query = "SELECT * FROM mock_orders WHERE status='OPEN' AND (expire_at IS NULL OR expire_at > ?)"
    params = [current_ts]

    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    
    if agent_name:
        query += " AND agent_name = ?"
        params.append(agent_name)

    c.execute(query, tuple(params))
        
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows

def create_mock_order(symbol, side, price, amount, stop_loss, take_profit, agent_name, order_id=None, expire_at=None):
    """
    创建一个模拟挂单 (必须传入 agent_name)
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
    valid_mode = "REAL" if trade_mode == "REAL" else "STRATEGY"
    
    c.execute("""
        INSERT INTO orders (order_id, timestamp, symbol, agent_name, side, entry_price, take_profit, stop_loss, reason, trade_mode) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(order_id), timestamp, symbol, str(agent_name), side, entry, tp, sl, reason, valid_mode))
    conn.commit()
    conn.close()

# (其余函数保持不变...)
def save_summary(symbol, agent_name, content, strategy_logic):
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
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM summaries WHERE symbol = ? ORDER BY id DESC LIMIT ?", (symbol, limit))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows

def save_balance_snapshot(symbol, balance, unrealized_pnl):
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

def save_trade_history(trades):
    if not trades: return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    for t in trades:
        try:
            pnl = t.get('realizedPnl')
            if pnl is None and 'info' in t:
                pnl = t['info'].get('realizedPnl')
            if pnl is None: pnl = 0
            fee_cost = 0
            fee_currency = ''
            if t.get('fee'):
                fee_cost = float(t['fee'].get('cost', 0) or 0)
                fee_currency = t['fee'].get('currency', '')
            c.execute('''
                INSERT OR IGNORE INTO trade_history 
                (trade_id, timestamp, symbol, side, price, amount, cost, fee, fee_currency, realized_pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (str(t['id']), datetime.fromtimestamp(t['timestamp']/1000).strftime('%Y-%m-%d %H:%M:%S'), t['symbol'], t['side'], float(t['price']), float(t['amount']), float(t['cost']), fee_cost, fee_currency, float(pnl)))
        except Exception as e:
            logger.error(f"Save trade error: {e}")
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    logger.info("Database initialized.")