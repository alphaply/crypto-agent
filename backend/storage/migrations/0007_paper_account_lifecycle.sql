CREATE TABLE paper_bank_rates (
    revision BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    effective_at DOUBLE PRECISION NOT NULL,
    daily_rate NUMERIC NOT NULL CHECK (daily_rate >= 0)
);
CREATE INDEX paper_bank_rates_time ON paper_bank_rates(effective_at, revision);
CREATE TABLE paper_interest_settlements (
    namespace TEXT NOT NULL, user_id TEXT NOT NULL, account_epoch BIGINT NOT NULL,
    period_end DOUBLE PRECISION NOT NULL, amount NUMERIC NOT NULL,
    PRIMARY KEY(namespace,user_id,account_epoch,period_end)
);
CREATE TABLE paper_transfers (
    namespace TEXT NOT NULL, sender TEXT NOT NULL, operation_id TEXT NOT NULL,
    recipient TEXT NOT NULL, amount NUMERIC NOT NULL CHECK(amount > 0),
    sender_epoch BIGINT NOT NULL, recipient_epoch BIGINT NOT NULL,
    PRIMARY KEY(namespace,sender,operation_id)
);
ALTER TABLE paper_ranking_projection ADD COLUMN reset_count BIGINT NOT NULL DEFAULT 0;
CREATE INDEX paper_ranking_resets ON paper_ranking_projection(namespace,generation,reset_count DESC);
