-- A manual valuation starting point is separate from actual orders and budgets.
CREATE TABLE spot_cost_basis (
    config_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL CHECK (quantity >= 0 AND quantity < 'Infinity'::float8),
    avg_cost DOUBLE PRECISION NOT NULL CHECK (avg_cost >= 0 AND avg_cost < 'Infinity'::float8),
    order_baselines_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (config_id, symbol)
);
