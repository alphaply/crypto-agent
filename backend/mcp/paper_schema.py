"""Paper ledger migrations, without committing the caller's transaction."""


def initialize_schema(conn):
    """Schema is installed by the explicit PostgreSQL migration command."""
    return None
