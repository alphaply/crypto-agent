-- Only configuration policy is upgraded. Trade identities, observations,
-- original actors, money and execution history are deliberately untouched.
UPDATE agent_configs
SET data_json = (data_json::jsonb ||
    '{"position_management_policy":"visible_first_claim","management_scopes":[],"management_policy_version":3}'::jsonb)::text
WHERE data_json::jsonb->>'position_management_policy' IN ('strict_owned', 'exclusive_scope', 'visible_first_claim');

UPDATE mcp_records
SET payload = (payload::jsonb ||
    '{"position_management_policy":"visible_first_claim","management_scopes":[],"management_policy_version":3}'::jsonb)::text
WHERE kind='profile'
  AND payload::jsonb->>'position_management_policy' IN ('strict_owned', 'exclusive_scope', 'visible_first_claim');
