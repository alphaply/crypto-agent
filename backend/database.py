import os
import sqlite3
import uuid
import pytz
from datetime import datetime, timedelta
import json
from contextlib import contextmanager
from backend.storage_paths import DATA_DIR, PROJECT_ROOT, data_file
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
                        config_id TEXT,
                        total_balance REAL,    -- 钱包余额
                        unrealized_pnl REAL,   -- 未实现盈亏
                        total_equity REAL      -- 净值 (余额+未实现)
                    )''')
        try: c.execute("ALTER TABLE balance_history ADD COLUMN config_id TEXT")
        except: pass
        try: c.execute("CREATE INDEX IF NOT EXISTS idx_balance_history_config_ts ON balance_history(config_id, timestamp)")
        except: pass
        try: c.execute("CREATE INDEX IF NOT EXISTS idx_balance_history_symbol_ts ON balance_history(symbol, timestamp)")
        except: pass

        # 5. 实盘成交记录 (从交易所同步)
        c.execute('''CREATE TABLE IF NOT EXISTS trade_history (
                        trade_id TEXT PRIMARY KEY, -- 交易所的 trade id
                        order_id TEXT,             -- 关联的订单 ID
                        config_id TEXT,
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

        c.execute('''CREATE TABLE IF NOT EXISTS app_settings (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )''')

        c.execute('''CREATE TABLE IF NOT EXISTS agent_configs (
                        config_id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        mode TEXT NOT NULL,
                        data_json TEXT NOT NULL,
                        sort_order INTEGER,
                        updated_at TEXT NOT NULL
                    )''')
        try: c.execute("ALTER TABLE agent_configs ADD COLUMN symbol TEXT NOT NULL DEFAULT ''")
        except: pass
        try: c.execute("ALTER TABLE agent_configs ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        except: pass
        try: c.execute("ALTER TABLE agent_configs ADD COLUMN mode TEXT NOT NULL DEFAULT 'STRATEGY'")
        except: pass
        try: c.execute("ALTER TABLE agent_configs ADD COLUMN data_json TEXT NOT NULL DEFAULT '{}'")
        except: pass
        try: c.execute("ALTER TABLE agent_configs ADD COLUMN sort_order INTEGER")
        except: pass
        try: c.execute("ALTER TABLE agent_configs ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        except: pass

        c.execute('''CREATE TABLE IF NOT EXISTS secret_store (
                        scope TEXT NOT NULL,
                        scope_id TEXT NOT NULL,
                        secret_key TEXT NOT NULL,
                        encrypted_value TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(scope, scope_id, secret_key)
                    )''')

        # 7. LLM Token 使用统计
        c.execute('''CREATE TABLE IF NOT EXISTS llm_providers (
                        provider_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        model TEXT NOT NULL,
                        api_base TEXT,
                        temperature REAL,
                        role TEXT NOT NULL DEFAULT 'agent',
                        extra_body TEXT NOT NULL DEFAULT '{}',
                        thinking_enabled INTEGER,
                        reasoning_effort TEXT,
                        updated_at TEXT NOT NULL
                    )''')
        try: c.execute("ALTER TABLE llm_providers ADD COLUMN thinking_enabled INTEGER")
        except: pass
        try: c.execute("ALTER TABLE llm_providers ADD COLUMN reasoning_effort TEXT")
        except: pass

        c.execute('''CREATE TABLE IF NOT EXISTS exchange_profiles (
                        profile_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        exchange TEXT NOT NULL,
                        market_type TEXT,
                        updated_at TEXT NOT NULL
                    )''')

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
                        balance REAL,
                        unrealized_pnl REAL DEFAULT 0,
                        total_equity REAL
                    )''')
        try: c.execute("ALTER TABLE mock_balance_history ADD COLUMN unrealized_pnl REAL DEFAULT 0")
        except: pass
        try: c.execute("ALTER TABLE mock_balance_history ADD COLUMN total_equity REAL")
        except: pass
        try: c.execute("CREATE INDEX IF NOT EXISTS idx_mock_balance_history_config_ts ON mock_balance_history(config_id, timestamp)")
        except: pass

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

        c.execute('''CREATE TABLE IF NOT EXISTS short_memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        bucket_start TEXT NOT NULL,
                        bucket_end TEXT NOT NULL,
                        symbol TEXT,
                        config_id TEXT NOT NULL,
                        market_summary TEXT,
                        position_summary TEXT,
                        source_count INTEGER DEFAULT 0,
                        created_at TEXT,
                        UNIQUE(config_id, bucket_start)
                    )''')

        c.execute('''CREATE TABLE IF NOT EXISTS position_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        config_id TEXT NOT NULL,
                        symbol TEXT,
                        position_key TEXT NOT NULL,
                        side TEXT,
                        status TEXT,
                        source TEXT,
                        opened_at TEXT,
                        closed_at TEXT,
                        entry_price REAL,
                        close_price REAL,
                        amount REAL,
                        realized_pnl REAL,
                        raw_json TEXT,
                        updated_at TEXT,
                        UNIQUE(config_id, position_key)
                    )''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_short_memories_config_bucket ON short_memories(config_id, bucket_start)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_position_history_config_time ON position_history(config_id, updated_at)")

        conn.commit()

    try:
        from backend.config_store import ensure_runtime_config_initialized

        ensure_runtime_config_initialized()
    except Exception as e:
        logger.error(f"❌ 初始化 SQLite 运行配置失败: {e}")

    try:
        from backend.config import config as runtime_config

        runtime_config.reload_config()
    except Exception as e:
        logger.error(f"❌ 重新加载运行配置失败: {e}")

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


def delete_model_pricing(model):
    """删除模型计价。"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM model_pricing WHERE model = ?", (model,))
        deleted = c.rowcount
        conn.commit()

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
    """更新模拟账户余额，处理爆仓逻辑，并记录一条流水（以 realized_pnl 触发）。
    注意：这条快照仅作为余额变动点保留，未实现盈亏/总权益字段默认与余额相同；
    调度器会在每分钟调用 save_mock_equity_snapshot 写入包含未实现盈亏的完整快照。
    """
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
        c.execute(
            "INSERT INTO mock_balance_history (config_id, symbol, timestamp, balance, unrealized_pnl, total_equity) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (config_id, symbol, timestamp, new_balance, 0.0, new_balance),
        )
        conn.commit()
    return new_balance, failures


def save_mock_equity_snapshot(config_id, symbol, balance, unrealized_pnl):
    """写入包含未实现盈亏的策略模式权益快照。

    :param balance: 钱包余额（已实现）
    :param unrealized_pnl: 已成交模拟仓位的未实现盈亏（未成交挂单不计）
    """
    try:
        balance = float(balance or 0)
        unrealized_pnl = float(unrealized_pnl or 0)
    except Exception:
        balance = 0.0
        unrealized_pnl = 0.0
    total_equity = balance + unrealized_pnl
    timestamp = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            c.execute(
                "INSERT INTO mock_balance_history (config_id, symbol, timestamp, balance, unrealized_pnl, total_equity) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(config_id), symbol, timestamp, balance, unrealized_pnl, total_equity),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"❌ DB Error (save_mock_equity_snapshot): {e}")
    return total_equity

def get_mock_equity_history(config_id, days=30):
    """获取指定策略模拟账户的资金曲线（按天聚合的最后一条），最多保留 30 天。
    优先使用 total_equity (钱包余额+未实现盈亏)，旧数据回退 balance。
    """
    cutoff = (datetime.now(TZ_CN) - timedelta(days=days)).strftime('%Y-%m-%d 00:00:00')
    with get_db_conn() as conn:
        c = conn.cursor()
        # 每天取最后一条（按 id 排序），并返回 balance + total_equity 兼容字段
        c.execute('''
            SELECT date(h.timestamp) as date,
                   h.balance as balance,
                   COALESCE(h.total_equity, h.balance) as equity,
                   COALESCE(h.unrealized_pnl, 0) as unrealized_pnl
            FROM mock_balance_history h
            INNER JOIN (
                SELECT date(timestamp) as d, MAX(id) as max_id
                FROM mock_balance_history
                WHERE config_id = ? AND timestamp >= ?
                GROUP BY date(timestamp)
            ) latest ON date(h.timestamp) = latest.d AND h.id = latest.max_id
            WHERE h.config_id = ?
            ORDER BY date(h.timestamp) ASC
        ''', (config_id, cutoff, config_id))
        rows = [dict(r) for r in c.fetchall()]

        # 如果历史为空，至少返回当前本金点亮图表
        if not rows:
            acc = get_mock_account(config_id, "")
            now_str = datetime.now(TZ_CN).strftime('%Y-%m-%d')
            rows = [{
                "date": now_str,
                "balance": acc['balance'],
                "equity": acc['balance'],
                "unrealized_pnl": 0.0,
            }]

        return rows

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

    created_at = datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')
    with get_db_conn() as conn:
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO mock_orders (order_id, symbol, agent_name, config_id, side, price, amount, stop_loss, take_profit, timestamp, expire_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (order_id, symbol, agent_name, config_id or agent_name, side, price, amount, stop_loss, take_profit, created_at, expire_at))
            conn.commit()
            upsert_position_history(
                config_id=config_id or agent_name,
                symbol=symbol,
                position_key=str(order_id),
                side="LONG" if "BUY" in str(side).upper() else "SHORT",
                status="OPEN",
                source="mock_order",
                opened_at=created_at,
                entry_price=price,
                amount=amount,
                raw={"order_id": order_id, "stop_loss": stop_loss, "take_profit": take_profit, "expire_at": expire_at},
            )
        except Exception as e:
            logger.error(f"❌ DB Error (create_mock_order): {e}")

def cancel_mock_order(order_id):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM mock_orders WHERE order_id = ?", (order_id,))
        deleted = c.rowcount > 0
        c.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = ?", (order_id,))
        conn.commit()
        return deleted

def update_mock_order_filled(order_id):
    """标记模拟挂单已成交 (入场)"""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE mock_orders SET is_filled = 1 WHERE order_id = ?", (order_id,))
        conn.commit()

def close_mock_order(order_id, close_price=0.0, realized_pnl=0.0):
    closed_position = None
    """平仓模拟挂单"""
    with get_db_conn() as conn:
        c = conn.cursor()
        close_time = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
        
        # 先更新虚拟账户与流水
        row = c.execute("SELECT * FROM mock_orders WHERE order_id=?", (order_id,)).fetchone()
        if row:
            update_mock_account_balance(row['config_id'], row['symbol'], realized_pnl)
            closed_position = dict(row)

        c.execute('''
            UPDATE mock_orders 
            SET status='CLOSED', close_price=?, realized_pnl=?, close_time=? 
            WHERE order_id=? AND status='OPEN'
        ''', (close_price, realized_pnl, close_time, order_id))
        
        c.execute("UPDATE orders SET status = 'CLOSED' WHERE order_id = ?", (order_id,))
        conn.commit()

    if closed_position:
        upsert_position_history(
            config_id=closed_position.get("config_id"),
            symbol=closed_position.get("symbol"),
            position_key=str(order_id),
            side="LONG" if "BUY" in str(closed_position.get("side", "")).upper() else "SHORT",
            status="CLOSED",
            source="mock_order",
            opened_at=closed_position.get("timestamp"),
            closed_at=close_time,
            entry_price=closed_position.get("price"),
            close_price=close_price,
            amount=closed_position.get("amount"),
            realized_pnl=realized_pnl,
            raw={**closed_position, "close_price": close_price, "realized_pnl": realized_pnl, "close_time": close_time},
        )


def save_order_log(order_id, symbol, agent_name, side, entry, tp, sl, reason, trade_mode="STRATEGY", config_id=None, amount=0, status="OPEN"):
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
            INSERT INTO orders (order_id, timestamp, symbol, agent_name, config_id, side, entry_price, amount, take_profit, stop_loss, reason, trade_mode, status) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(order_id), timestamp, symbol, str(agent_name), config_id or str(agent_name), side, entry, amount, tp, sl, reason, valid_mode, status))
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

def save_balance_snapshot(symbol, balance, unrealized_pnl, config_id=None):
    """记录实盘资金快照。

    :param symbol: 交易对
    :param balance: 钱包余额（total_balance）
    :param unrealized_pnl: 未实现盈亏
    :param config_id: 可选，按策略配置记录，便于多配置隔离对比
    """
    timestamp = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    equity = float(balance or 0) + float(unrealized_pnl or 0)

    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute('''
            INSERT INTO balance_history (timestamp, symbol, config_id, total_balance, unrealized_pnl, total_equity)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (timestamp, symbol, str(config_id) if config_id else None, balance, unrealized_pnl, equity))
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

def get_balance_history(symbol, limit=100, config_id=None):
    """获取资金曲线数据。
    若提供 config_id，优先返回该 config 的快照；旧数据无 config_id 时按 symbol 回退。
    """
    with get_db_conn() as conn:
        c = conn.cursor()
        if config_id:
            c.execute(
                """
                SELECT * FROM balance_history
                WHERE config_id = ? AND symbol = ?
                ORDER BY id ASC LIMIT ?
                """,
                (str(config_id), symbol, limit),
            )
            rows = [dict(row) for row in c.fetchall()]
            if rows:
                return rows
        c.execute(
            "SELECT * FROM balance_history WHERE symbol = ? ORDER BY id ASC LIMIT ?",
            (symbol, limit),
        )
        return [dict(row) for row in c.fetchall()]


def auto_close_expired_mock_orders(config_id=None, symbol=None):
    """自动关闭已过期但仍为 OPEN 的未成交模拟挂单。
    返回关闭的订单数。已成交仓位不受影响。
    """
    current_ts = datetime.now().timestamp()
    close_time = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    closed_count = 0
    expired_orders = []
    with get_db_conn() as conn:
        c = conn.cursor()
        query = (
            "SELECT order_id, config_id, symbol, side, price, amount, agent_name, timestamp "
            "FROM mock_orders WHERE status='OPEN' "
            "AND COALESCE(is_filled, 0) = 0 "
            "AND expire_at IS NOT NULL AND expire_at <= ?"
        )
        params = [current_ts]
        if config_id:
            query += " AND config_id = ?"
            params.append(config_id)
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        rows = c.execute(query, tuple(params)).fetchall()
        expired_orders = [dict(r) for r in rows]

        if expired_orders:
            order_ids = [o["order_id"] for o in expired_orders]
            placeholders = ",".join(["?"] * len(order_ids))
            c.execute(
                f"UPDATE mock_orders SET status='CANCELLED', close_time=? "
                f"WHERE order_id IN ({placeholders}) AND status='OPEN'",
                (close_time, *order_ids),
            )
            closed_count = c.rowcount
            c.execute(
                f"UPDATE orders SET status='CANCELLED' "
                f"WHERE order_id IN ({placeholders}) AND status='OPEN'",
                tuple(order_ids),
            )
            conn.commit()

    # 同步 position_history 状态
    for o in expired_orders:
        try:
            upsert_position_history(
                config_id=o.get("config_id"),
                symbol=o.get("symbol"),
                position_key=str(o.get("order_id")),
                side="LONG" if "BUY" in str(o.get("side", "")).upper() else "SHORT",
                status="CLOSED",
                source="mock_order",
                opened_at=o.get("timestamp"),
                closed_at=close_time,
                entry_price=o.get("price"),
                amount=o.get("amount"),
                realized_pnl=0,
                raw={**o, "expired": True, "close_time": close_time},
            )
        except Exception as exc:
            logger.warning(f"Failed to sync expired mock order position: {exc}")

    return closed_count


def get_filled_mock_positions(config_id, symbol=None):
    """获取指定策略已入场但未平仓的模拟仓位（is_filled=1, status=OPEN）。"""
    with get_db_conn() as conn:
        c = conn.cursor()
        query = (
            "SELECT order_id, symbol, side, price, amount, stop_loss, take_profit "
            "FROM mock_orders WHERE config_id = ? AND status='OPEN' "
            "AND COALESCE(is_filled, 0) = 1"
        )
        params = [str(config_id)]
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        rows = c.execute(query, tuple(params)).fetchall()
        return [dict(r) for r in rows]


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
                    str(config_id or t.get('config_id') or '') or None,
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
        conn.commit()

def delete_daily_summary(date_str, config_id):
    """Delete one daily summary by date and config."""
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "DELETE FROM daily_summaries WHERE date = ? AND config_id = ?",
            (date_str, config_id),
        )
        deleted = c.rowcount
        conn.commit()
        return deleted

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


def list_daily_summaries(symbol=None, config_id=None, days=None, limit=200):
    """List daily summaries for admin management and export."""
    with get_db_conn() as conn:
        c = conn.cursor()
        clauses = []
        params = []
        if symbol and symbol != "ALL":
            clauses.append("symbol = ?")
            params.append(symbol)
        if config_id and config_id != "ALL":
            clauses.append("config_id = ?")
            params.append(config_id)
        if days:
            cutoff = (datetime.now(TZ_CN) - timedelta(days=int(days))).strftime("%Y-%m-%d")
            clauses.append("date >= ?")
            params.append(cutoff)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit or 200))
        rows = c.execute(
            f"""
            SELECT id, date, symbol, config_id, summary, source_count, created_at
            FROM daily_summaries
            {where}
            ORDER BY date DESC, config_id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


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

def get_summary_logic_between(config_id, start_time, end_time):
    with get_db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            '''
            SELECT strategy_logic, timestamp
            FROM summaries
            WHERE config_id = ?
              AND timestamp >= ?
              AND timestamp < ?
              AND strategy_logic IS NOT NULL
              AND strategy_logic != ''
            ORDER BY id ASC
            ''',
            (config_id, start_time, end_time),
        ).fetchall()
        return [dict(row) for row in rows]


def save_short_memory(bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count):
    created_at = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            '''
            INSERT INTO short_memories (
                bucket_start, bucket_end, symbol, config_id, market_summary,
                position_summary, source_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_id, bucket_start) DO UPDATE SET
                bucket_end = excluded.bucket_end,
                symbol = excluded.symbol,
                market_summary = excluded.market_summary,
                position_summary = excluded.position_summary,
                source_count = excluded.source_count,
                created_at = excluded.created_at
            ''',
            (bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, int(source_count or 0), created_at),
        )
        conn.commit()


def get_short_memories(config_id, limit=2):
    with get_db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            '''
            SELECT bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count, created_at
            FROM short_memories
            WHERE config_id = ?
            ORDER BY bucket_start DESC
            LIMIT ?
            ''',
            (config_id, int(limit or 2)),
        ).fetchall()
        return [dict(row) for row in rows]


def list_short_memories(symbol=None, config_id=None, limit=200):
    with get_db_conn() as conn:
        c = conn.cursor()
        sql = '''
            SELECT id, bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count, created_at
            FROM short_memories
            WHERE 1=1
        '''
        params = []
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        if config_id and config_id != "ALL":
            sql += " AND config_id = ?"
            params.append(config_id)
        sql += " ORDER BY bucket_start DESC LIMIT ?"
        params.append(int(limit or 200))
        rows = c.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]


def get_short_memory(config_id, bucket_start):
    with get_db_conn() as conn:
        c = conn.cursor()
        row = c.execute(
            '''
            SELECT bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count, created_at
            FROM short_memories
            WHERE config_id = ? AND bucket_start = ?
            ''',
            (config_id, bucket_start),
        ).fetchone()
        return dict(row) if row else None


def update_short_memory(config_id, bucket_start, market_summary, position_summary):
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            '''
            UPDATE short_memories
            SET market_summary = ?, position_summary = ?
            WHERE config_id = ? AND bucket_start = ?
            ''',
            (market_summary, position_summary, config_id, bucket_start),
        )
        conn.commit()
        return c.rowcount


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
    if not config_id or not position_key:
        return

    updated_at = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    raw_json = json.dumps(raw or {}, ensure_ascii=False, default=str)
    with get_db_conn() as conn:
        c = conn.cursor()
        c.execute(
            '''
            INSERT INTO position_history (
                config_id, symbol, position_key, side, status, source, opened_at,
                closed_at, entry_price, close_price, amount, realized_pnl, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_id, position_key) DO UPDATE SET
                symbol = excluded.symbol,
                side = COALESCE(excluded.side, position_history.side),
                status = CASE
                    WHEN position_history.status = 'CLOSED' AND excluded.status = 'OPEN' THEN position_history.status
                    ELSE COALESCE(excluded.status, position_history.status)
                END,
                source = COALESCE(excluded.source, position_history.source),
                opened_at = COALESCE(position_history.opened_at, excluded.opened_at),
                closed_at = COALESCE(excluded.closed_at, position_history.closed_at),
                entry_price = COALESCE(excluded.entry_price, position_history.entry_price),
                close_price = COALESCE(excluded.close_price, position_history.close_price),
                amount = COALESCE(excluded.amount, position_history.amount),
                realized_pnl = COALESCE(excluded.realized_pnl, position_history.realized_pnl),
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
            ''',
            (
                str(config_id),
                symbol,
                str(position_key),
                side,
                status,
                source,
                opened_at,
                closed_at,
                entry_price,
                close_price,
                amount,
                realized_pnl,
                raw_json,
                updated_at,
            ),
        )
        conn.commit()


def get_position_history(config_id, since_time=None, limit=50):
    with get_db_conn() as conn:
        c = conn.cursor()
        params = [config_id]
        where = "WHERE config_id = ?"
        if since_time:
            where += " AND (updated_at >= ? OR closed_at >= ? OR opened_at >= ?)"
            params.extend([since_time, since_time, since_time])
        params.append(int(limit or 50))
        rows = c.execute(
            f'''
            SELECT config_id, symbol, position_key, side, status, source, opened_at, closed_at,
                   entry_price, close_price, amount, realized_pnl, updated_at
            FROM position_history
            {where}
            ORDER BY COALESCE(closed_at, updated_at, opened_at) DESC
            LIMIT ?
            ''',
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


def sync_open_position_history(config_id, symbol, positions, source="exchange_position"):
    for pos in positions or []:
        try:
            side = str(pos.get("side") or "").upper() or "UNKNOWN"
            amount = float(pos.get("amount", pos.get("contracts", 0)) or 0)
            entry_price = float(pos.get("entry_price", pos.get("entryPrice", 0)) or 0)
            if amount <= 0:
                continue
            position_key = f"{source}:{symbol}:{side}:{entry_price}"
            upsert_position_history(
                config_id=config_id,
                symbol=symbol,
                position_key=position_key,
                side=side,
                status="OPEN",
                source=source,
                entry_price=entry_price,
                amount=amount,
                raw=pos,
            )
        except Exception as exc:
            logger.warning(f"Failed to sync open position history: {exc}")


def sync_trade_position_history(config_id, symbol, trades, source="exchange_trade"):
    for trade in trades or []:
        try:
            info = trade.get("info") or {}
            raw_pnl = trade.get("realizedPnl")
            if raw_pnl is None:
                raw_pnl = info.get("realizedPnl")
            realized_pnl = float(raw_pnl or 0)
            if realized_pnl == 0:
                continue

            timestamp = trade.get("timestamp")
            closed_at = None
            if timestamp:
                closed_at = datetime.fromtimestamp(float(timestamp) / 1000, TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
            side = str(trade.get("side") or info.get("side") or "").upper()
            position_side = str(info.get("positionSide") or "").upper()
            if position_side in {"LONG", "SHORT"}:
                side = position_side
            position_key = f"{source}:{trade.get('id') or trade.get('order') or uuid.uuid4().hex}"
            upsert_position_history(
                config_id=config_id,
                symbol=trade.get("symbol") or symbol,
                position_key=position_key,
                side=side,
                status="CLOSED",
                source=source,
                closed_at=closed_at,
                close_price=float(trade.get("price", 0) or 0),
                amount=float(trade.get("amount", 0) or 0),
                realized_pnl=realized_pnl,
                raw=trade,
            )
        except Exception as exc:
            logger.warning(f"Failed to sync trade position history: {exc}")


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
