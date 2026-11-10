-- Rebuildable read models. Original paper_accounts / paper_events remain authoritative.
CREATE TABLE paper_statistics_accounts (
    namespace TEXT NOT NULL, user_id TEXT NOT NULL,
    source_updated_at DOUBLE PRECISION NOT NULL, current_revision BIGINT NOT NULL DEFAULT 0,
    current_epoch INTEGER, cycle_start_id BIGINT NOT NULL DEFAULT 0,
    cursor BIGINT NOT NULL DEFAULT 0, cursor_epoch INTEGER,
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    current_data JSONB NOT NULL,
    PRIMARY KEY(namespace,user_id)
);
CREATE TABLE paper_statistics_events (
    event_id BIGINT PRIMARY KEY, namespace TEXT NOT NULL, user_id TEXT NOT NULL,
    timestamp DOUBLE PRECISION NOT NULL, epoch INTEGER, kind TEXT NOT NULL,
    is_fill BOOLEAN NOT NULL DEFAULT FALSE, is_liquidation BOOLEAN NOT NULL DEFAULT FALSE,
    fee NUMERIC, realized_net NUMERIC, raw_realized NUMERIC, interest NUMERIC,
    income_issue TEXT, fee_issue TEXT, count_issue TEXT
);
CREATE INDEX paper_statistics_events_scope ON paper_statistics_events(namespace,user_id,timestamp,event_id);
CREATE TABLE paper_statistics_cycles (
    namespace TEXT NOT NULL, user_id TEXT NOT NULL, cycle_key TEXT NOT NULL,
    epoch INTEGER, first_event_id BIGINT NOT NULL, last_event_id BIGINT NOT NULL,
    closed_at DOUBLE PRECISION, net NUMERIC, state JSONB NOT NULL,
    PRIMARY KEY(namespace,user_id,cycle_key)
);
CREATE INDEX paper_statistics_cycles_scope ON paper_statistics_cycles(namespace,user_id,closed_at);
