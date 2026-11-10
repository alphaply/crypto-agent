-- Signed external flows belong to the exchange account, not a trading symbol.
CREATE TABLE real_cashflows (
    account_scope TEXT NOT NULL, income_type TEXT NOT NULL, transaction_id TEXT NOT NULL,
    currency TEXT NOT NULL, timestamp_ms BIGINT NOT NULL, amount NUMERIC NOT NULL,
    PRIMARY KEY(account_scope,income_type,transaction_id)
);
CREATE INDEX real_cashflows_window ON real_cashflows(account_scope,currency,timestamp_ms);
CREATE TABLE real_cashflow_coverage (
    account_scope TEXT NOT NULL, start_ms BIGINT NOT NULL, end_ms BIGINT NOT NULL,
    checked_at_ms BIGINT NOT NULL, CHECK(end_ms >= start_ms),
    PRIMARY KEY(account_scope,start_ms,end_ms)
);
CREATE TABLE real_cashflow_sync (
    account_scope TEXT PRIMARY KEY, attempted_at_ms BIGINT NOT NULL,
    lease_until_ms BIGINT NOT NULL, status TEXT NOT NULL, error_code TEXT,
    query_span_ms BIGINT NOT NULL DEFAULT 604800000 CHECK(query_span_ms > 0)
);
