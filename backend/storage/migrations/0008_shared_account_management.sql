CREATE TABLE management_shared_accounts (
    physical_account TEXT NOT NULL, market_type TEXT NOT NULL, legacy_scope TEXT NOT NULL,
    PRIMARY KEY(physical_account,market_type)
);
INSERT INTO management_shared_accounts(physical_account,market_type,legacy_scope)
SELECT physical_account,market_type,MIN(legacy_scope)
FROM management_execution_scopes GROUP BY physical_account,market_type ON CONFLICT DO NOTHING;
CREATE TABLE management_shared_participants (
    claim_id TEXT NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(claim_id,actor)
);
INSERT INTO management_shared_participants(claim_id,actor)
SELECT claim_id,owner FROM visible_management_claims ON CONFLICT DO NOTHING;
