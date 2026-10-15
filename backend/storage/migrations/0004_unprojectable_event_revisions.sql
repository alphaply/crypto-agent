-- A raw JSON TEXT value may be readable by Python while JSONB cannot represent
-- an unrelated field (for example an escaped NUL). Such records must still
-- invalidate in-flight history snapshots when they change.
CREATE OR REPLACE FUNCTION track_execution_source_revision() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    prior JSONB;
    following JSONB;
    fallback_symbols TEXT[] := ARRAY[]::TEXT[];
    fallback_configs TEXT[] := ARRAY[]::TEXT[];
    scope RECORD;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        prior := CASE WHEN TG_TABLE_NAME = 'real_protection_events'
            THEN storage_json(OLD.payload) ELSE to_jsonb(OLD) END;
        IF TG_TABLE_NAME = 'real_protection_events' AND prior IS NULL THEN
            fallback_symbols := array_append(fallback_symbols, split_part(OLD.symbol, ':', 1));
            fallback_configs := array_append(fallback_configs, OLD.config_id);
        END IF;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        following := CASE WHEN TG_TABLE_NAME = 'real_protection_events'
            THEN storage_json(NEW.payload) ELSE to_jsonb(NEW) END;
        IF TG_TABLE_NAME = 'real_protection_events' AND following IS NULL THEN
            fallback_symbols := array_append(fallback_symbols, split_part(NEW.symbol, ':', 1));
            fallback_configs := array_append(fallback_configs, NEW.config_id);
        END IF;
    END IF;
    FOR scope IN
        SELECT account_scope,symbol FROM (
            SELECT value->>'account_scope' AS account_scope, value->>'symbol' AS symbol
            FROM (VALUES(prior),(following)) AS sources(value)
            WHERE value->>'account_scope' IS NOT NULL AND value->>'symbol' IS NOT NULL
            UNION
            -- Rebuild creates its revision row before taking its source snapshot.
            -- Retry only already-known candidate scopes; do not invent ownership
            -- or modify the source filters used by Python cycle reconstruction.
            SELECT r.account_scope,r.symbol FROM execution_source_revisions r
            WHERE split_part(r.symbol, ':', 1) = ANY(fallback_symbols)
               OR EXISTS (
                    SELECT 1 FROM execution_order_links l
                    WHERE l.account_scope=r.account_scope AND l.symbol=r.symbol
                      AND l.config_id=ANY(fallback_configs)
               )
        ) AS affected ORDER BY account_scope,symbol
    LOOP
        INSERT INTO execution_source_revisions(account_scope,symbol,revision)
        VALUES(scope.account_scope,scope.symbol,1)
        ON CONFLICT(account_scope,symbol) DO UPDATE
            SET revision=execution_source_revisions.revision+1;
    END LOOP;
    RETURN NULL;
END;
$$;
