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




        # 4. 新增: 账户净值历史 (用于画盈亏曲线)
    c.execute('''CREATE TABLE IF NOT EXISTS balance_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    symbol TEXT,
                    total_balance REAL,    -- 钱包余额
                    unrealized_pnl REAL,   -- 未实现盈亏
                    total_equity REAL      -- 净值 (余额+未实现)
                )''')

    # 5. 新增: 实盘成交记录 (从交易所同步回来)
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

            # 4. 手续费处理 (防报错)
            fee_cost = 0
            fee_currency = ''
            if t.get('fee'):
                fee_cost = float(t['fee'].get('cost', 0) or 0)
                fee_currency = t['fee'].get('currency', '')

            # 尝试插入，如果 trade_id 重复则忽略
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
                float(pnl) # ✅ 这里现在能存入真实的盈亏了
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