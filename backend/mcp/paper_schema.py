"""Paper ledger migrations, without committing the caller's transaction."""


def initialize_schema(conn):
    statements = [
        '''CREATE TABLE IF NOT EXISTS paper_accounts (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,payload TEXT NOT NULL,
            PRIMARY KEY(namespace,user_id))''',
        '''CREATE TABLE IF NOT EXISTS paper_operations (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,operation_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,principal TEXT NOT NULL,result TEXT NOT NULL,
            PRIMARY KEY(namespace,user_id,operation_id))''',
        '''CREATE TABLE IF NOT EXISTS paper_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,namespace TEXT NOT NULL,user_id TEXT NOT NULL,
            timestamp REAL NOT NULL,kind TEXT NOT NULL,operation_id TEXT,payload TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS paper_events_user ON paper_events(namespace,user_id,id)',
        '''CREATE TABLE IF NOT EXISTS paper_orders (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,order_id TEXT NOT NULL,
            state TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
            PRIMARY KEY(namespace,user_id,order_id))''',
        'CREATE INDEX IF NOT EXISTS paper_orders_state ON paper_orders(state,namespace,user_id)',
        '''CREATE TABLE IF NOT EXISTS paper_batches (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,operation_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,payload TEXT NOT NULL,worker_pid INTEGER NOT NULL,
            worker_token TEXT NOT NULL,updated_at REAL NOT NULL,
            PRIMARY KEY(namespace,user_id,operation_id))''',
        '''CREATE TABLE IF NOT EXISTS paper_batch_children (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,child_operation_id TEXT NOT NULL,
            parent_operation_id TEXT NOT NULL,action_index INTEGER NOT NULL,
            PRIMARY KEY(namespace,user_id,child_operation_id),
            UNIQUE(namespace,user_id,parent_operation_id,action_index))''',
    ]
    for statement in statements:
        conn.execute(statement)
