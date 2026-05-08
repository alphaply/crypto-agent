import os
import sqlite3
import uuid
import pytz
from datetime import datetime, timedelta
import json
from contextlib import contextmanager
from utils.logger import setup_logger

# 设置时区
TZ_CN = pytz.timezone('Asia/Shanghai')

# 使用绝对路径定位数据库文件
# 强制获取项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "trading_data.db")
logger = setup_logger("Database")


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_json(data):
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return "{}"

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
                        status TEXT DEFAULT 'OPEN',
                        close_price REAL,
                        realized_pnl REAL,
                        close_time TEXT
                    )''')
        try: c.execute("ALTER TABLE mock_orders ADD COLUMN agent_name TEXT")
        except: pass
        try: c.execute("ALTER TABLE mock_orders ADD COLUMN config_id TEXT")
        except: pass
        try: c.execute("ALTER TABLE mock_orders ADD COLUMN expire_at REAL")
        except: pass
        try: c.execute("ALTER TABLE mock_orders ADD COLUMN close_price REAL")
        except: pass
        try: c.execute("ALTER TABLE mock_orders ADD COLUMN realized_pnl REAL")
        except: pass
        try: c.execute("ALTER TABLE mock_orders ADD COLUMN close_time TEXT")
        except: pass
        try: c.execute("ALTER TABLE mock_orders ADD COLUMN is_filled INTEGER DEFAULT 0")
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
                        amount REAL,
                        take_profit REAL,
                        stop_loss REAL,
                        reason TEXT,
                        status TEXT DEFAULT 'OPEN'
                    )''')
        # 自动迁移：添加缺失列
        try: c.execute("ALTER TABLE orders ADD COLUMN trade_mode TEXT")
        except: pass
        try: c.execute("ALTER TABLE orders ADD COLUMN config_id TEXT")
        except: pass
        try: c.execute("ALTER TABLE orders ADD COLUMN amount REAL")
        except: pass
        try: c.execute("ALTER TABLE orders ADD COLUMN filled_amount REAL DEFAULT 0")
        except: pass
        try: c.execute("ALTER TABLE orders ADD COLUMN filled_cost REAL DEFAULT 0")
        except: pass
        try: c.execute("ALTER TABLE orders ADD COLUMN avg_fill_price REAL DEFAULT 0")
        except: pass
        try: c.execute("ALTER TABLE orders ADD COLUMN filled_at TEXT")
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
                        order_id TEXT,             -- 关联的订单 ID
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
        try: c.execute("ALTER TABLE trade_history ADD COLUMN order_id TEXT")
        except: pass
        try: c.execute("ALTER TABLE trade_history ADD COLUMN config_id TEXT")
        except: pass

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

        c.execute('''CREATE TABLE IF NOT EXISTS short_memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        bucket_start TEXT,
                        bucket_end TEXT,
                        symbol TEXT,
                        config_id TEXT,
                        market_summary TEXT,
                        position_summary TEXT,
                        source_summary_count INTEGER DEFAULT 0,
                        source_position_count INTEGER DEFAULT 0,
                        fingerprint TEXT,
                        created_at TEXT,
                        UNIQUE(bucket_start, config_id)
                    )''')

        c.execute('''CREATE TABLE IF NOT EXISTS position_history (
                        event_id TEXT PRIMARY KEY,
                        config_id TEXT,
                        symbol TEXT,
                        source TEXT,
                        event_type TEXT,
                        order_id TEXT,
                        side TEXT,
                        amount REAL,
                        entry_price REAL,
                        close_price REAL,
                        realized_pnl REAL,
                        open_time TEXT,
                        close_time TEXT,
                        raw_data TEXT,
                        created_at TEXT
                    )''')

        # 10. 模拟账户资金表
        c.execute('''CREATE TABLE IF NOT EXISTS mock_accounts (
                        config_id TEXT PRIMARY KEY,
                        symbol TEXT,
                        balance REAL DEFAULT 10000.0,
                        failures INTEGER DEFAULT 0
                    )''')

        # 11. 模拟账户资金流水表
        c.execute('''CREATE TABLE IF NOT EXISTS mock_balance_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        config_id TEXT,
                        symbol TEXT,
                        timestamp TEXT,
                        balance REAL
                    )''')

        # 12. SPOT_DCA 成交同步状态表（每个订单一行）
        c.execute('''CREATE TABLE IF NOT EXISTS spot_order_fills (
                        order_id TEXT PRIMARY KEY,
                        config_id TEXT,
                        symbol TEXT,
                        status TEXT,
                        filled_qty REAL DEFAULT 0,
                        filled_cost REAL DEFAULT 0,
                        avg_fill_price REAL DEFAULT 0,
                        filled_at TEXT,
                        last_sync_at TEXT
                    )''')

        # 13. SPOT_DCA 每日统计快照（支持 History 曲线）
        c.execute('''CREATE TABLE IF NOT EXISTS dca_daily_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        snapshot_date TEXT,
                        config_id TEXT,
                        symbol TEXT,
                        total_invested REAL,
                        total_qty REAL,
                        avg_cost REAL,
                        buy_count INTEGER,
                        first_buy TEXT,
                        last_buy TEXT,
                        actual_balance REAL,
                        updated_at TEXT,
                        UNIQUE(snapshot_date, config_id)
                    )''')

        conn.commit()

    # 初始化完成后，从文件同步计价配置
    load_model_pricing_from_file()

# --- 模型计价管理 ---

def load_model_pricing_from_file():
    """从 pricing.json 加载模型计价并同步到数据库"""
    pricing_file = os.path.join(BASE_DIR, "pricing.json")
    if not os.path.exists(pricing_file):
        logger.warning(f"⚠️ pricing.json 不存在，跳过初始化计价。")
        return

    try:
        with open(pricing_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        for model, info in data.items():
            input_price = info.get('input_price_per_m', 0)
            output_price = info.get('output_price_per_m', 0)
            currency = info.get('currency', 'USD')
            update_model_pricing(model, input_price, output_price, currency, persist_file=False)
        
        logger.info(f"✅ 已从 pricing.json 同步 {len(data)} 个模型的计价信息。")
    except Exception as e:
        logger.error(f"❌ 加载 pricing.json 失败: {e}")

def get_all_pricing():
    """获取所有模型的计价信息"""
    with get_db_conn() as conn:
        c = conn.cursor()
        rows = c.execute("SELECT * FROM model_pricing").fetchall()
        return {r['model']: dict(r) for r in rows}


def _write_pricing_file_from_db():
    """将当前数据库中的定价信息同步写入 pricing.json。"""
    pricing_file = os.path.join(BASE_DIR, "pricing.json")
    pricing = get_all_pricing()
    serialized = {
        model: {
            "input_price_per_m": row.get("input_price_per_m", 0),
            "output_price_per_m": row.get("output_price_per_m", 0),
            "currency": row.get("currency", "USD"),
        }
        for model, row in sorted(pricing.items(), key=lambda item: item[0])
    }
    with open(pricing_file, 'w', encoding='utf-8') as f:
        json.dump(serialized, f, ensure_ascii=False, indent=2)


def _sync_pricing_file_safe():
    try:
        _write_pricing_file_from_db()
    except Exception as e:
        logger.error(f"❌ 同步 pricing.json 失败: {e}")

def update_model_pricing(model, input_price, output_price, currency='USD', persist_file=True):
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

    if persist_file:
        _sync_pricing_file_safe()


def delete_model_pricing(model, persist_file=True):
    """删除模型计价。"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM model_pricing WHERE model = ?", (model,))
        deleted = c.rowcount
        conn.commit()

    if deleted and persist_file:
        _sync_pricing_file_safe()
    return deleted

# --- 模拟交易资金池 / 挂单池功能 ---

def get_mock_account(config_id, symbol):
    """获取/初始化模拟账户"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM mock_accounts WHERE config_id = ?", (config_id,))
        row = c.fetchone()
        if not row:
            c.execute("INSERT INTO mock_accounts (config_id, symbol, balance, failures) VALUES (?, ?, ?, ?)",
                      (config_id, symbol, 10000.0, 0))
            conn.commit()
            return {"config_id": config_id, "symbol": symbol, "balance": 10000.0, "failures": 0}
        return dict(row)

def update_mock_account_balance(config_id, symbol, realized_pnl):
    """更新模拟账户余额，处理爆仓逻辑，并记录流水"""
    acc = get_mock_account(config_id, symbol)
    new_balance = acc['balance'] + realized_pnl
    failures = acc['failures']
    
    if new_balance < 1000:
        new_balance = 10000.0  # 破产重置
        failures += 1
        
    timestamp = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE mock_accounts SET balance = ?, failures = ? WHERE config_id = ?",
                  (new_balance, failures, config_id))
        c.execute("INSERT INTO mock_balance_history (config_id, symbol, timestamp, balance) VALUES (?, ?, ?, ?)",
                  (config_id, symbol, timestamp, new_balance))
        conn.commit()
    return new_balance, failures

def get_mock_equity_history(config_id, days=30):
    """获取指定策略模拟账户的资金曲线（按天聚合的最后一条），最多保留 30 天"""
    cutoff = (datetime.now(TZ_CN) - timedelta(days=days)).strftime('%Y-%m-%d 00:00:00')
    with get_db_conn() as conn:
        c = conn.cursor()
        # 每天取最后一条
        c.execute('''
            SELECT date(timestamp) as date, balance
            FROM mock_balance_history
            WHERE config_id = ? AND timestamp >= ?
            GROUP BY date(timestamp)
            ORDER BY date(timestamp) ASC
        ''', (config_id, cutoff))
        rows = c.fetchall()
        
        # 如果历史为空，至少返回当前本金点亮图表
        if not rows:
            acc = get_mock_account(config_id, "")
            rows = [{"date": datetime.now(TZ_CN).strftime('%Y-%m-%d'), "balance": acc['balance']}]
            
        return [dict(r) for r in rows]

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

def update_mock_order_filled(order_id):
    """标记模拟挂单已成交 (入场)"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE mock_orders SET is_filled = 1 WHERE order_id = ?", (order_id,))
        conn.commit()
    record_mock_position_open(order_id)

def close_mock_order(order_id, close_price=0.0, realized_pnl=0.0):
    """平仓模拟挂单"""
    event_row = None
    did_close = False
    close_time = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        c = conn.cursor()
        
        # 先更新虚拟账户与流水
        row = c.execute("SELECT * FROM mock_orders WHERE order_id=?", (order_id,)).fetchone()
        if row:
            event_row = dict(row)
            update_mock_account_balance(row['config_id'], row['symbol'], realized_pnl)

        c.execute('''
            UPDATE mock_orders 
            SET status='CLOSED', close_price=?, realized_pnl=?, close_time=? 
            WHERE order_id=? AND status='OPEN'
        ''', (close_price, realized_pnl, close_time, order_id))
        did_close = c.rowcount > 0
        
        c.execute("UPDATE orders SET status = 'CLOSED' WHERE order_id = ?", (order_id,))
        conn.commit()

    if event_row and did_close:
        if int(event_row.get("is_filled") or 0):
            record_mock_position_open(order_id, event_row)
        upsert_position_history({
            "event_id": f"mock:{order_id}:close",
            "config_id": event_row.get("config_id"),
            "symbol": event_row.get("symbol"),
            "source": "mock",
            "event_type": "CLOSE",
            "order_id": order_id,
            "side": "LONG" if "BUY" in str(event_row.get("side", "")).upper() else "SHORT",
            "amount": _safe_float(event_row.get("amount")),
            "entry_price": _safe_float(event_row.get("price")),
            "close_price": _safe_float(close_price),
            "realized_pnl": _safe_float(realized_pnl),
            "open_time": event_row.get("timestamp"),
            "close_time": close_time,
            "raw_data": event_row,
        })


def upsert_position_history(event):
    """Insert or update a normalized position event/snapshot."""
    if not event or not event.get("event_id"):
        return False
    created_at = event.get("created_at") or datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    raw_data = event.get("raw_data")
    raw_text = raw_data if isinstance(raw_data, str) else _safe_json(raw_data or {})
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            '''
            INSERT INTO position_history (
                event_id, config_id, symbol, source, event_type, order_id, side,
                amount, entry_price, close_price, realized_pnl,
                open_time, close_time, raw_data, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                config_id = excluded.config_id,
                symbol = excluded.symbol,
                source = excluded.source,
                event_type = excluded.event_type,
                order_id = excluded.order_id,
                side = excluded.side,
                amount = excluded.amount,
                entry_price = excluded.entry_price,
                close_price = excluded.close_price,
                realized_pnl = excluded.realized_pnl,
                open_time = excluded.open_time,
                close_time = excluded.close_time,
                raw_data = excluded.raw_data,
                created_at = excluded.created_at
            ''',
            (
                str(event.get("event_id")),
                event.get("config_id"),
                event.get("symbol"),
                event.get("source"),
                event.get("event_type"),
                event.get("order_id"),
                event.get("side"),
                _safe_float(event.get("amount")),
                _safe_float(event.get("entry_price")),
                _safe_float(event.get("close_price")),
                _safe_float(event.get("realized_pnl")),
                event.get("open_time"),
                event.get("close_time"),
                raw_text,
                created_at,
            ),
        )
        conn.commit()
        return True


def record_mock_position_open(order_id, row_data=None):
    """Record a mock position once the pending order becomes a filled position."""
    if row_data is None:
        with get_db_conn() as conn:
            row = conn.execute("SELECT * FROM mock_orders WHERE order_id = ?", (order_id,)).fetchone()
            if not row:
                return False
            row_data = dict(row)

    if not int(row_data.get("is_filled") or 0):
        return False

    return upsert_position_history({
        "event_id": f"mock:{order_id}:open",
        "config_id": row_data.get("config_id"),
        "symbol": row_data.get("symbol"),
        "source": "mock",
        "event_type": "OPEN",
        "order_id": order_id,
        "side": "LONG" if "BUY" in str(row_data.get("side", "")).upper() else "SHORT",
        "amount": _safe_float(row_data.get("amount")),
        "entry_price": _safe_float(row_data.get("price")),
        "close_price": 0,
        "realized_pnl": 0,
        "open_time": row_data.get("timestamp"),
        "close_time": None,
        "raw_data": row_data,
    })


def sync_real_position_history(config_id, symbol, positions=None, trades=None):
    """Persist exchange-backed position snapshots and close events when available."""
    now = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    count = 0

    for pos in positions or []:
        side = str(pos.get("side") or "").upper() or "UNKNOWN"
        amount = _safe_float(pos.get("amount") or pos.get("contracts") or pos.get("qty"))
        entry_price = _safe_float(pos.get("entry_price") or pos.get("entryPrice"))
        if amount <= 0:
            continue
        event_id = f"real:{config_id}:{symbol}:{side}:snapshot:{now[:13]}"
        if upsert_position_history({
            "event_id": event_id,
            "config_id": config_id,
            "symbol": symbol,
            "source": "exchange",
            "event_type": "SNAPSHOT",
            "order_id": "",
            "side": side,
            "amount": amount,
            "entry_price": entry_price,
            "close_price": 0,
            "realized_pnl": _safe_float(pos.get("unrealized_pnl")),
            "open_time": now,
            "close_time": None,
            "raw_data": pos,
            "created_at": now,
        }):
            count += 1

    for trade in trades or []:
        pnl = trade.get("realizedPnl")
        if pnl is None and isinstance(trade.get("info"), dict):
            pnl = trade["info"].get("realizedPnl")
        pnl = _safe_float(pnl)
        if pnl == 0:
            continue

        trade_id = str(trade.get("id") or trade.get("trade_id") or "")
        if not trade_id:
            continue
        side_raw = str(trade.get("side") or "").lower()
        event_side = "LONG" if side_raw == "sell" else "SHORT"
        amount = _safe_float(trade.get("amount"))
        close_price = _safe_float(trade.get("price"))
        entry_price = 0
        if amount > 0:
            entry_price = close_price - (pnl / amount) if side_raw == "sell" else close_price + (pnl / amount)

        close_time = now
        if trade.get("timestamp"):
            try:
                close_time = datetime.fromtimestamp(float(trade["timestamp"]) / 1000).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        elif trade.get("datetime"):
            close_time = str(trade.get("datetime"))[:19].replace("T", " ")

        if upsert_position_history({
            "event_id": f"real:{config_id}:{trade_id}:close",
            "config_id": config_id,
            "symbol": trade.get("symbol") or symbol,
            "source": "exchange",
            "event_type": "CLOSE",
            "order_id": str(trade.get("order") or trade.get("order_id") or ""),
            "side": event_side,
            "amount": amount,
            "entry_price": entry_price,
            "close_price": close_price,
            "realized_pnl": pnl,
            "open_time": None,
            "close_time": close_time,
            "raw_data": trade,
            "created_at": now,
        }):
            count += 1

    return count


def save_order_log(order_id, symbol, agent_name, side, entry, tp, sl, reason, trade_mode="STRATEGY", config_id=None, amount=0):
    timestamp = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
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
            INSERT INTO orders (order_id, timestamp, symbol, agent_name, config_id, side, entry_price, amount, take_profit, stop_loss, reason, trade_mode) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(order_id), timestamp, symbol, str(agent_name), config_id or str(agent_name), side, entry, amount, tp, sl, reason, valid_mode))
        conn.commit()


def update_order_fill_status(order_id, status, filled_qty=0.0, filled_cost=0.0, avg_fill_price=0.0, filled_at=None):
    """更新 orders 表的成交状态信息（主要用于 SPOT_DCA）。"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            '''
            UPDATE orders
            SET status = ?,
                filled_amount = ?,
                filled_cost = ?,
                avg_fill_price = ?,
                filled_at = ?
            WHERE order_id = ?
            ''',
            (status, float(filled_qty or 0), float(filled_cost or 0), float(avg_fill_price or 0), filled_at, str(order_id)),
        )
        conn.commit()


def upsert_spot_order_fill(order_id, config_id, symbol, status, filled_qty=0.0, filled_cost=0.0, avg_fill_price=0.0, filled_at=None):
    """写入或更新现货订单成交同步状态。"""
    last_sync_at = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            '''
            INSERT INTO spot_order_fills (order_id, config_id, symbol, status, filled_qty, filled_cost, avg_fill_price, filled_at, last_sync_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                config_id = excluded.config_id,
                symbol = excluded.symbol,
                status = excluded.status,
                filled_qty = excluded.filled_qty,
                filled_cost = excluded.filled_cost,
                avg_fill_price = excluded.avg_fill_price,
                filled_at = excluded.filled_at,
                last_sync_at = excluded.last_sync_at
            ''',
            (
                str(order_id),
                str(config_id),
                str(symbol),
                str(status),
                float(filled_qty or 0),
                float(filled_cost or 0),
                float(avg_fill_price or 0),
                filled_at,
                last_sync_at,
            ),
        )
        conn.commit()


def save_dca_daily_snapshot(config_id, symbol, stats):
    """按天保存 DCA 统计快照（同一天覆盖更新）。"""
    snapshot_date = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    updated_at = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            '''
            INSERT INTO dca_daily_snapshots (
                snapshot_date, config_id, symbol, total_invested, total_qty, avg_cost,
                buy_count, first_buy, last_buy, actual_balance, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date, config_id) DO UPDATE SET
                symbol = excluded.symbol,
                total_invested = excluded.total_invested,
                total_qty = excluded.total_qty,
                avg_cost = excluded.avg_cost,
                buy_count = excluded.buy_count,
                first_buy = excluded.first_buy,
                last_buy = excluded.last_buy,
                actual_balance = excluded.actual_balance,
                updated_at = excluded.updated_at
            ''',
            (
                snapshot_date,
                str(config_id),
                str(symbol),
                float(stats.get("total_invested", 0) or 0),
                float(stats.get("total_qty", 0) or 0),
                float(stats.get("avg_cost", 0) or 0),
                int(stats.get("buy_count", 0) or 0),
                stats.get("first_buy"),
                stats.get("last_buy"),
                float(stats.get("actual_balance", 0) or 0),
                updated_at,
            ),
        )
        conn.commit()


def get_dca_daily_snapshot_history(config_id, days=30):
    """获取最近 N 天 DCA 快照曲线。"""
    with get_db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            '''
            SELECT snapshot_date, total_invested, total_qty, avg_cost, buy_count, actual_balance, updated_at
            FROM dca_daily_snapshots
            WHERE config_id = ?
            ORDER BY snapshot_date ASC
            LIMIT ?
            ''',
            (str(config_id), int(days)),
        ).fetchall()
        return [dict(r) for r in rows]

# --- 数据分析与记录 ---

def save_summary(symbol, agent_name, content, strategy_logic, config_id=None, agent_type=None):
    """保存 AI 分析结果"""
    timestamp = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    
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
            # 获取该币种下所有不为空的 config_id
            rows = c.execute("SELECT DISTINCT config_id FROM summaries WHERE symbol = ? AND config_id IS NOT NULL", (symbol,)).fetchall()
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

def get_summary_count(symbol, config_id=None):
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            sql = "SELECT COUNT(*) FROM summaries WHERE symbol = ?"
            params = [symbol]
            
            if config_id and config_id != 'ALL':
                sql += " AND config_id = ?"
                params.append(config_id)
                
            return c.execute(sql, tuple(params)).fetchone()[0]
        except:
            return 0

def get_paginated_summaries(symbol, page=1, per_page=10, config_id=None):
    offset = (page - 1) * per_page
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            # 动态构建 SQL
            sql = "SELECT * FROM summaries WHERE symbol = ?"
            params = [symbol]

            if config_id and config_id != 'ALL':
                sql += " AND config_id = ?"
                params.append(config_id)

            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([per_page, offset])

            c.execute(sql, tuple(params))
            return [dict(row) for row in c.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get paginated summaries: symbol={symbol}, page={page}, per_page={per_page}, config_id={config_id}, error={e}")
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
    timestamp = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
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


def save_trade_history(trades, config_id=None):
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
                    (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, fee, fee_currency, realized_pnl)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    str(t['id']),
                    str(t.get('order', t.get('order_id', ''))),
                    config_id,
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
    now = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
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
    now = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
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
    created_at = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
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

def update_daily_summary(date_str, config_id, summary):
    """更新某天某 config 的每日策略汇总文本"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            UPDATE daily_summaries
            SET summary = ?
            WHERE date = ? AND config_id = ?
        ''', (summary, date_str, config_id))
        updated = c.rowcount
        conn.commit()
        return updated > 0

def get_daily_summaries(config_id, days=5):
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


def save_short_memory(bucket_start, bucket_end, symbol, config_id, market_summary,
                      position_summary, source_summary_count=0, source_position_count=0,
                      fingerprint=None):
    created_at = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            '''
            INSERT INTO short_memories (
                bucket_start, bucket_end, symbol, config_id, market_summary, position_summary,
                source_summary_count, source_position_count, fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_start, config_id) DO UPDATE SET
                bucket_end = excluded.bucket_end,
                symbol = excluded.symbol,
                market_summary = excluded.market_summary,
                position_summary = excluded.position_summary,
                source_summary_count = excluded.source_summary_count,
                source_position_count = excluded.source_position_count,
                fingerprint = excluded.fingerprint,
                created_at = excluded.created_at
            ''',
            (
                bucket_start,
                bucket_end,
                symbol,
                config_id,
                market_summary,
                position_summary,
                int(source_summary_count or 0),
                int(source_position_count or 0),
                fingerprint,
                created_at,
            ),
        )
        conn.commit()


def get_short_memory(config_id, bucket_start):
    with get_db_conn() as conn:
        row = conn.execute(
            '''
            SELECT *
            FROM short_memories
            WHERE config_id = ? AND bucket_start = ?
            LIMIT 1
            ''',
            (config_id, bucket_start),
        ).fetchone()
        return dict(row) if row else None


def get_recent_short_memories(config_id, limit=2):
    with get_db_conn() as conn:
        rows = conn.execute(
            '''
            SELECT bucket_start, bucket_end, symbol, config_id, market_summary, position_summary,
                   source_summary_count, source_position_count, created_at
            FROM short_memories
            WHERE config_id = ?
            ORDER BY bucket_start DESC
            LIMIT ?
            ''',
            (config_id, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]


def get_pending_short_memory_data(config_id, bucket_start, bucket_end):
    with get_db_conn() as conn:
        summaries = conn.execute(
            '''
            SELECT timestamp, content, strategy_logic
            FROM summaries
            WHERE config_id = ? AND timestamp >= ? AND timestamp < ?
            ORDER BY id ASC
            ''',
            (config_id, bucket_start, bucket_end),
        ).fetchall()
        positions = conn.execute(
            '''
            SELECT *
            FROM position_history
            WHERE config_id = ?
              AND COALESCE(close_time, open_time, created_at) >= ?
              AND COALESCE(close_time, open_time, created_at) < ?
            ORDER BY COALESCE(close_time, open_time, created_at) ASC
            ''',
            (config_id, bucket_start, bucket_end),
        ).fetchall()
        return {
            "summaries": [dict(row) for row in summaries],
            "positions": [dict(row) for row in positions],
        }


def get_recent_position_events(config_id, hours=4, limit=20):
    cutoff = (datetime.now(TZ_CN) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        rows = conn.execute(
            '''
            SELECT *
            FROM position_history
            WHERE config_id = ?
              AND COALESCE(close_time, open_time, created_at) >= ?
            ORDER BY COALESCE(close_time, open_time, created_at) DESC
            LIMIT ?
            ''',
            (config_id, cutoff, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

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
                from config import config as global_config
                cfg = global_config.get_config_by_id(config_id)
                if cfg and cfg.get('mode', '').upper() == 'REAL':
                    trades = c.execute(
                        """
                        SELECT realized_pnl FROM trade_history
                        WHERE symbol LIKE ?
                          AND config_id = ?
                          AND realized_pnl IS NOT NULL
                          AND realized_pnl != 0
                        """,
                        (symbol + '%', config_id),
                    ).fetchall()
                    if not trades:
                        trades = c.execute(
                            """
                            SELECT realized_pnl FROM trade_history
                            WHERE symbol LIKE ?
                              AND config_id IS NULL
                              AND realized_pnl IS NOT NULL
                              AND realized_pnl != 0
                            """,
                            (symbol + '%',),
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
    with get_db_conn() as conn:
        c = conn.cursor()
        tables = [
            "chat_sessions",
            "mock_accounts",
            "mock_balance_history",
            "mock_orders",
            "orders",
            "summaries",
            "token_usage",
            "trade_history",
            "daily_summaries",
            "short_memories",
            "position_history",
        ]
        counts = {}
        for table in tables:
            counts[table] = c.execute(
                f"SELECT COUNT(*) FROM {table} WHERE config_id = ?",
                (config_id,),
            ).fetchone()[0]

        counts["open_mock_orders"] = c.execute(
            "SELECT COUNT(*) FROM mock_orders WHERE config_id = ? AND status = 'OPEN'",
            (config_id,),
        ).fetchone()[0]
        counts["open_orders"] = c.execute(
            "SELECT COUNT(*) FROM orders WHERE config_id = ? AND status = 'OPEN'",
            (config_id,),
        ).fetchone()[0]
        return counts


def soft_delete_config_runtime_data(config_id: str):
    """
    软删除策略对应的数据清理：
    - 清理强绑定运行态数据（会话、模拟账户、模拟余额）
    - 关闭仍处于 OPEN 的模拟单/决策单
    - 保留历史审计数据（orders/summaries/token_usage/daily_summaries）
    """
    with get_db_conn() as conn:
        c = conn.cursor()

        deleted_chat_sessions = c.execute(
            "DELETE FROM chat_sessions WHERE config_id = ?",
            (config_id,),
        ).rowcount
        deleted_mock_accounts = c.execute(
            "DELETE FROM mock_accounts WHERE config_id = ?",
            (config_id,),
        ).rowcount
        deleted_mock_balance_history = c.execute(
            "DELETE FROM mock_balance_history WHERE config_id = ?",
            (config_id,),
        ).rowcount

        closed_open_mock_orders = c.execute(
            "UPDATE mock_orders SET status = 'CLOSED', close_time = ? WHERE config_id = ? AND status = 'OPEN'",
            (datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S"), config_id),
        ).rowcount
        cancelled_open_orders = c.execute(
            "UPDATE orders SET status = 'CANCELLED' WHERE config_id = ? AND status = 'OPEN'",
            (config_id,),
        ).rowcount

        conn.commit()

        return {
            "chat_sessions_deleted": deleted_chat_sessions,
            "mock_accounts_deleted": deleted_mock_accounts,
            "mock_balance_history_deleted": deleted_mock_balance_history,
            "open_mock_orders_closed": closed_open_mock_orders,
            "open_orders_cancelled": cancelled_open_orders,
        }


def purge_config_all_data(config_id: str):
    """彻底删除指定 config_id 的历史与运行数据，避免历史页残留。"""
    with get_db_conn() as conn:
        c = conn.cursor()

        cleanup = {
            "chat_sessions_deleted": c.execute(
                "DELETE FROM chat_sessions WHERE config_id = ?",
                (config_id,),
            ).rowcount,
            "mock_accounts_deleted": c.execute(
                "DELETE FROM mock_accounts WHERE config_id = ?",
                (config_id,),
            ).rowcount,
            "mock_balance_history_deleted": c.execute(
                "DELETE FROM mock_balance_history WHERE config_id = ?",
                (config_id,),
            ).rowcount,
            "mock_orders_deleted": c.execute(
                "DELETE FROM mock_orders WHERE config_id = ?",
                (config_id,),
            ).rowcount,
            "orders_deleted": c.execute(
                "DELETE FROM orders WHERE config_id = ?",
                (config_id,),
            ).rowcount,
            "summaries_deleted": c.execute(
                "DELETE FROM summaries WHERE config_id = ?",
                (config_id,),
            ).rowcount,
            "token_usage_deleted": c.execute(
                "DELETE FROM token_usage WHERE config_id = ?",
                (config_id,),
            ).rowcount,
            "trade_history_deleted": c.execute(
                "DELETE FROM trade_history WHERE config_id = ?",
                (config_id,),
            ).rowcount,
            "daily_summaries_deleted": c.execute(
                "DELETE FROM daily_summaries WHERE config_id = ?",
                (config_id,),
            ).rowcount,
            "short_memories_deleted": c.execute(
                "DELETE FROM short_memories WHERE config_id = ?",
                (config_id,),
            ).rowcount,
            "position_history_deleted": c.execute(
                "DELETE FROM position_history WHERE config_id = ?",
                (config_id,),
            ).rowcount,
        }

        conn.commit()
        return cleanup


if __name__ == "__main__":
    init_db()
    logger.info("Database initialized.")
