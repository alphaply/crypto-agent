import sqlite3


def _execute_best_effort(cursor: sqlite3.Cursor, statement: str) -> None:
    try:
        cursor.execute(statement)
    except Exception:
        pass


def initialize_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()

    cursor.execute("PRAGMA journal_mode=WAL")

    cursor.execute('''CREATE TABLE IF NOT EXISTS summaries (
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
    _execute_best_effort(cursor, "ALTER TABLE summaries ADD COLUMN agent_name TEXT")
    _execute_best_effort(cursor, "ALTER TABLE summaries ADD COLUMN config_id TEXT")
    _execute_best_effort(cursor, "ALTER TABLE summaries ADD COLUMN agent_type TEXT")

    cursor.execute('''CREATE TABLE IF NOT EXISTS mock_orders (
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
    _execute_best_effort(cursor, "ALTER TABLE mock_orders ADD COLUMN agent_name TEXT")
    _execute_best_effort(cursor, "ALTER TABLE mock_orders ADD COLUMN config_id TEXT")
    _execute_best_effort(cursor, "ALTER TABLE mock_orders ADD COLUMN expire_at REAL")
    _execute_best_effort(cursor, "ALTER TABLE mock_orders ADD COLUMN close_price REAL")
    _execute_best_effort(cursor, "ALTER TABLE mock_orders ADD COLUMN realized_pnl REAL")
    _execute_best_effort(cursor, "ALTER TABLE mock_orders ADD COLUMN close_time TEXT")
    _execute_best_effort(cursor, "ALTER TABLE mock_orders ADD COLUMN is_filled INTEGER DEFAULT 0")

    cursor.execute('''CREATE TABLE IF NOT EXISTS orders (
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
    _execute_best_effort(cursor, "ALTER TABLE orders ADD COLUMN trade_mode TEXT")
    _execute_best_effort(cursor, "ALTER TABLE orders ADD COLUMN config_id TEXT")
    _execute_best_effort(cursor, "ALTER TABLE orders ADD COLUMN amount REAL")
    _execute_best_effort(cursor, "ALTER TABLE orders ADD COLUMN filled_amount REAL DEFAULT 0")
    _execute_best_effort(cursor, "ALTER TABLE orders ADD COLUMN filled_cost REAL DEFAULT 0")
    _execute_best_effort(cursor, "ALTER TABLE orders ADD COLUMN avg_fill_price REAL DEFAULT 0")
    _execute_best_effort(cursor, "ALTER TABLE orders ADD COLUMN filled_at TEXT")

    cursor.execute('''CREATE TABLE IF NOT EXISTS balance_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    symbol TEXT,
                    config_id TEXT,
                    total_balance REAL,
                    unrealized_pnl REAL,
                    total_equity REAL
                )''')
    _execute_best_effort(cursor, "ALTER TABLE balance_history ADD COLUMN config_id TEXT")
    _execute_best_effort(cursor, "CREATE INDEX IF NOT EXISTS idx_balance_history_config_ts ON balance_history(config_id, timestamp)")
    _execute_best_effort(cursor, "CREATE INDEX IF NOT EXISTS idx_balance_history_symbol_ts ON balance_history(symbol, timestamp)")

    cursor.execute('''CREATE TABLE IF NOT EXISTS trade_history (
                    trade_id TEXT PRIMARY KEY,
                    order_id TEXT,
                    config_id TEXT,
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
    _execute_best_effort(cursor, "ALTER TABLE trade_history ADD COLUMN order_id TEXT")
    _execute_best_effort(cursor, "ALTER TABLE trade_history ADD COLUMN config_id TEXT")

    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT,
                    config_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS agent_configs (
                    config_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    mode TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    sort_order INTEGER,
                    updated_at TEXT NOT NULL
                )''')
    _execute_best_effort(cursor, "ALTER TABLE agent_configs ADD COLUMN symbol TEXT NOT NULL DEFAULT ''")
    _execute_best_effort(cursor, "ALTER TABLE agent_configs ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    _execute_best_effort(cursor, "ALTER TABLE agent_configs ADD COLUMN mode TEXT NOT NULL DEFAULT 'STRATEGY'")
    _execute_best_effort(cursor, "ALTER TABLE agent_configs ADD COLUMN data_json TEXT NOT NULL DEFAULT '{}'")
    _execute_best_effort(cursor, "ALTER TABLE agent_configs ADD COLUMN sort_order INTEGER")
    _execute_best_effort(cursor, "ALTER TABLE agent_configs ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")

    cursor.execute('''CREATE TABLE IF NOT EXISTS secret_store (
                    scope TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    secret_key TEXT NOT NULL,
                    encrypted_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope, scope_id, secret_key)
                )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS llm_providers (
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
    _execute_best_effort(cursor, "ALTER TABLE llm_providers ADD COLUMN thinking_enabled INTEGER")
    _execute_best_effort(cursor, "ALTER TABLE llm_providers ADD COLUMN reasoning_effort TEXT")

    cursor.execute('''CREATE TABLE IF NOT EXISTS exchange_profiles (
                    profile_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    market_type TEXT,
                    updated_at TEXT NOT NULL
                )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    symbol TEXT,
                    config_id TEXT,
                    model TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER
                )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS model_pricing (
                    model TEXT PRIMARY KEY,
                    input_price_per_m REAL DEFAULT 0,
                    output_price_per_m REAL DEFAULT 0,
                    currency TEXT DEFAULT 'USD'
                )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS daily_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT,
                    symbol TEXT,
                    config_id TEXT,
                    summary TEXT,
                    source_count INTEGER DEFAULT 0,
                    created_at TEXT,
                    UNIQUE(date, config_id)
                )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS mock_accounts (
                    config_id TEXT PRIMARY KEY,
                    symbol TEXT,
                    balance REAL DEFAULT 10000.0,
                    failures INTEGER DEFAULT 0
                )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS mock_balance_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_id TEXT,
                    symbol TEXT,
                    timestamp TEXT,
                    balance REAL,
                    unrealized_pnl REAL DEFAULT 0,
                    total_equity REAL
                )''')
    _execute_best_effort(cursor, "ALTER TABLE mock_balance_history ADD COLUMN unrealized_pnl REAL DEFAULT 0")
    _execute_best_effort(cursor, "ALTER TABLE mock_balance_history ADD COLUMN total_equity REAL")
    _execute_best_effort(cursor, "CREATE INDEX IF NOT EXISTS idx_mock_balance_history_config_ts ON mock_balance_history(config_id, timestamp)")

    cursor.execute('''CREATE TABLE IF NOT EXISTS spot_order_fills (
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

    cursor.execute('''CREATE TABLE IF NOT EXISTS dca_daily_snapshots (
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

    cursor.execute('''CREATE TABLE IF NOT EXISTS short_memories (
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

    cursor.execute('''CREATE TABLE IF NOT EXISTS position_history (
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_short_memories_config_bucket ON short_memories(config_id, bucket_start)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_position_history_config_time ON position_history(config_id, updated_at)")

    conn.commit()