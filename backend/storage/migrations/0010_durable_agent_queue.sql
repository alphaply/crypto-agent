ALTER TABLE scheduler_runs ADD COLUMN trigger_key TEXT NOT NULL DEFAULT '';
ALTER TABLE scheduler_runs DROP CONSTRAINT scheduler_runs_config_id_job_type_scheduled_at_key;
ALTER TABLE scheduler_runs ADD CONSTRAINT scheduler_runs_trigger_unique
    UNIQUE(config_id, job_type, scheduled_at, trigger_key);

CREATE TABLE agent_run_queue (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    config_id TEXT NOT NULL,
    trigger_key TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    run_id TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'QUEUED'
        CHECK(status IN ('QUEUED','CLAIMED','RUNNING','FINISHED','FAILED','CANCELLED','INTERRUPTED')),
    scheduler_run_id BIGINT REFERENCES scheduler_runs(id) ON DELETE SET NULL,
    worker_owner TEXT,
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    reason TEXT,
    UNIQUE(config_id, trigger_key)
);
CREATE INDEX agent_queue_ready ON agent_run_queue(id) WHERE status='QUEUED';
CREATE INDEX agent_queue_config_status ON agent_run_queue(config_id,status,id);
CREATE UNIQUE INDEX agent_queue_one_worker_per_config ON agent_run_queue(config_id)
    WHERE status IN ('CLAIMED','RUNNING');
