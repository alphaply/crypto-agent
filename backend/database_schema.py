"""Runtime PostgreSQL version guard; DDL lives in storage/migrations."""
from backend import storage


def initialize_schema(conn: storage.Connection) -> None:
    from backend.storage.migrate import check_schema
    check_schema(conn)
    cursor = conn.cursor()
    # Clean up orphaned real_protection_plans for deleted agent configs
    tables = storage.table_names(conn)
    if "agent_configs" in tables and "real_protection_plans" in tables:
        agent_rows = cursor.execute("SELECT config_id FROM agent_configs").fetchall()
        if agent_rows:
            active_agents = {r[0] for r in agent_rows}
            mcp_profiles = set()
            if "mcp_records" in tables:
                mcp_profiles = {'mcp:' + r[0] for r in cursor.execute("SELECT id FROM mcp_records WHERE kind='profile'").fetchall()}
            allowed = active_agents | mcp_profiles
            placeholders = ','.join('%s' for _ in allowed)
            cursor.execute(f"DELETE FROM real_protection_plans WHERE config_id NOT IN ({placeholders})", tuple(allowed))
    conn.commit()
