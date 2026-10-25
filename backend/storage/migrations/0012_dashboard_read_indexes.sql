-- Dashboard requests filter by task and read its newest records. These indexes
-- also serve existing single-task order pages and latest-summary lookups.
CREATE INDEX idx_orders_config_id_desc ON orders(config_id, id DESC);
CREATE INDEX idx_summaries_config_id_desc ON summaries(config_id, id DESC);
