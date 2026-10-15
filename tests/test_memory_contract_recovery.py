import pytest
"""Legacy diagnostic columns survive, but contract-recovery work is retired."""
from backend import database
from backend.agent import memory_updates, context_projections as projections
from test_context_projections import sources, simulation


def test_legacy_failed_rows_are_not_requeued_by_schema(sources):
    with database.get_db_conn() as conn:
        memory_updates._initialize(conn)
        conn.execute("INSERT INTO memory_update_jobs(config_id,run_id,summary_id,status,attempts,error) VALUES('cfg','legacy',%s,'failed',3,'ProjectionReview missing memory')", (sources,))
        memory_updates._initialize(conn)
        projections.initialize_schema(conn)
        row = conn.execute("SELECT * FROM memory_update_jobs WHERE run_id='legacy'").fetchone()
        assert row['status'] == 'failed' and row['attempts'] == 3 and row['error']
    assert not hasattr(projections, 'process_projection_updates')
    assert not hasattr(projections, 'prepare_review')


pytestmark = pytest.mark.usefixtures("pg")
