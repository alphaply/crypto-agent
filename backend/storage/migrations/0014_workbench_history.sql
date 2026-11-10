-- Valuation revisions are accounting baselines, never executions or cash flows.
ALTER TABLE spot_cost_basis ADD COLUMN revision BIGINT NOT NULL DEFAULT 1;
ALTER TABLE spot_cost_basis ADD COLUMN realized_carry_json TEXT;
CREATE TABLE spot_cost_basis_revisions (
    config_id TEXT NOT NULL, symbol TEXT NOT NULL, revision BIGINT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL, avg_cost DOUBLE PRECISION NOT NULL,
    order_baselines_json TEXT NOT NULL, updated_at TEXT NOT NULL, realized_carry_json TEXT,
    PRIMARY KEY(config_id,symbol,revision)
);
INSERT INTO spot_cost_basis_revisions SELECT config_id,symbol,revision,quantity,avg_cost,order_baselines_json,updated_at,realized_carry_json FROM spot_cost_basis;
ALTER TABLE dca_daily_snapshots ADD COLUMN remaining_cost DOUBLE PRECISION;
ALTER TABLE dca_daily_snapshots ADD COLUMN market_value DOUBLE PRECISION;
ALTER TABLE dca_daily_snapshots ADD COLUMN holding_return_pct DOUBLE PRECISION;
ALTER TABLE dca_daily_snapshots ADD COLUMN realized_pnl DOUBLE PRECISION;
ALTER TABLE dca_daily_snapshots ADD COLUMN cost_basis_revision BIGINT;
ALTER TABLE dca_daily_snapshots ADD COLUMN valuation_status TEXT;
CREATE TABLE agent_trade_calls (
    config_id TEXT NOT NULL, operation_id TEXT NOT NULL,
    tool_name TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
    reason TEXT, tool_call_id TEXT, arguments_json TEXT NOT NULL, result_json TEXT,
    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(config_id,operation_id)
);
CREATE INDEX agent_trade_calls_history ON agent_trade_calls(config_id,created_at DESC);

-- Backfill only original stored requests. Do not infer motives from summaries.
INSERT INTO agent_trade_calls
 (config_id,operation_id,tool_name,symbol,action,reason,arguments_json,result_json,status,created_at,updated_at)
SELECT q.config_id,
 CASE WHEN jsonb_array_length(q.request_json::jsonb->'actions')=1 THEN q.operation_id
      ELSE COALESCE(c.child_operation_id,q.operation_id || ':history:' || (a.ordinality-1)::text) END,
 CASE a.value->>'action' WHEN 'open' THEN 'open_position' WHEN 'close' THEN 'close_position'
   WHEN 'edit' THEN 'edit_order' WHEN 'cancel' THEN 'cancel_order' WHEN 'protect' THEN 'set_position_protection' END,
 COALESCE(a.value->>'symbol',r.symbol),a.value->>'action',a.value->>'reason',
 (a.value-'action'-'depends_on')::text,
 CASE WHEN jsonb_array_length(q.request_json::jsonb->'actions')=1 THEN r.result
      ELSE COALESCE(cr.result,(r.result::jsonb->'results'->(a.ordinality-1)::int)::text) END,
 COALESCE(cr.status,(r.result::jsonb->'results'->(a.ordinality-1)::int)->>'status',r.status),
 to_char(to_timestamp(r.created_at) AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'),
 to_char(to_timestamp(r.updated_at) AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"')
FROM trade_operation_requests q JOIN trade_action_runs r USING(config_id,operation_id)
CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(q.request_json::jsonb->'actions')='array'
 THEN q.request_json::jsonb->'actions' ELSE '[]'::jsonb END) WITH ORDINALITY a
LEFT JOIN unified_trade_children c ON c.config_id=q.config_id AND c.parent_operation_id=q.operation_id
 AND c.action_index=a.ordinality-1
LEFT JOIN trade_action_runs cr ON cr.config_id=c.config_id AND cr.operation_id=c.child_operation_id
WHERE a.value->>'action' IN ('open','close','edit','cancel','protect')
ON CONFLICT DO NOTHING;
