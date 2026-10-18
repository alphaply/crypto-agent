"""Read-only runtime version guard; migrations and explicit cleanup own writes."""
from backend import storage


def initialize_schema(conn: storage.Connection) -> None:
    """Validate readiness without deleting history or committing caller work.

    A removed configuration can still have exchange orders or recovery evidence.
    Startup is not authorization to discard its persisted protection plans.
    """
    from backend.storage.migrate import check_schema
    check_schema(conn)
