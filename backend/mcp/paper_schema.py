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
        '''CREATE TABLE IF NOT EXISTS paper_resource_origins (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,kind TEXT NOT NULL,resource_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL,classification TEXT NOT NULL,reason_code TEXT NOT NULL,policy_version INTEGER NOT NULL,
            PRIMARY KEY(namespace,user_id,kind,resource_key))''',
        '''CREATE TABLE IF NOT EXISTS paper_origin_scans (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,kind TEXT NOT NULL,resource_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL,payload TEXT NOT NULL,
            PRIMARY KEY(namespace,user_id,kind,resource_key))''',
        '''CREATE TABLE IF NOT EXISTS paper_performance_events (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,event_id INTEGER NOT NULL,state TEXT NOT NULL,
            PRIMARY KEY(namespace,user_id,event_id))''',
        'CREATE INDEX IF NOT EXISTS paper_performance_unresolved ON paper_performance_events(namespace,user_id,state,event_id)',
        '''CREATE TABLE IF NOT EXISTS paper_scan_progress (
            worker TEXT PRIMARY KEY,cursor INTEGER NOT NULL,payload TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS paper_ranking_projection (
            namespace TEXT NOT NULL,generation TEXT NOT NULL,user_id TEXT NOT NULL,
            eligible INTEGER NOT NULL,pnl REAL,return_pct REAL,payload TEXT NOT NULL,
            PRIMARY KEY(namespace,generation,user_id))''',
        "CREATE INDEX IF NOT EXISTS paper_ranking_pnl ON paper_ranking_projection(namespace,generation,eligible,pnl DESC,json_extract(payload,'$.participant_id'))",
        "CREATE INDEX IF NOT EXISTS paper_ranking_return ON paper_ranking_projection(namespace,generation,eligible,return_pct DESC,json_extract(payload,'$.participant_id'))",
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
        '''CREATE TABLE IF NOT EXISTS paper_batch_status (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,operation_id TEXT NOT NULL,status TEXT NOT NULL,
            PRIMARY KEY(namespace,user_id,operation_id))''',
        '''CREATE TABLE IF NOT EXISTS paper_receipt_revisions (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,operation_id TEXT NOT NULL,
            revision INTEGER NOT NULL,observed_at REAL NOT NULL,payload TEXT NOT NULL,
            PRIMARY KEY(namespace,user_id,operation_id,revision))''',
        '''CREATE TABLE IF NOT EXISTS paper_receipt_watch (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,operation_id TEXT NOT NULL,active INTEGER NOT NULL,
            PRIMARY KEY(namespace,user_id,operation_id))''',
        'CREATE INDEX IF NOT EXISTS paper_receipt_watch_active ON paper_receipt_watch(active)',
        '''CREATE TABLE IF NOT EXISTS paper_raw_versions (
            namespace TEXT NOT NULL,user_id TEXT NOT NULL,kind TEXT NOT NULL,record_id TEXT NOT NULL,
            revision INTEGER NOT NULL,PRIMARY KEY(namespace,user_id,kind,record_id))''',
    ]
    for statement in statements:
        conn.execute(statement)
    # A version token can be checked before every bounded byte read without
    # decoding or hashing a potentially very large historical JSON value.
    for table, kind, identifier in (('paper_accounts', 'account', 'user_id'),
                                     ('paper_orders', 'order', 'order_id'),
                                     ('paper_events', 'event', 'id'),
                                     ('paper_batches', 'batch', 'operation_id')):
        for event in ('INSERT', 'UPDATE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_audit_{event.lower()}
                AFTER {event} ON {table} BEGIN
                INSERT INTO paper_raw_versions VALUES(NEW.namespace,NEW.user_id,'{kind}',CAST(NEW.{identifier} AS TEXT),1)
                ON CONFLICT(namespace,user_id,kind,record_id) DO UPDATE SET revision=revision+1;
                END''')
