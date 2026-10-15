-- Ownership indexing consumes identities and intent, not mutable observations.
-- Keep this projection aligned with ExecutionLedger._index_plan and
-- execution_plan_index._plan_facts. Raw payloads and execution-history revision
-- tracking remain unchanged. Unprojectable evidence must still invalidate.
CREATE FUNCTION ownership_plan_source(raw TEXT) RETURNS JSONB
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
    plan JSONB := storage_json(raw);
    projected JSONB;
    collection TEXT;
    records JSONB;
    item JSONB;
    identity JSONB;
BEGIN
    IF plan IS NULL OR jsonb_typeof(plan) <> 'object' THEN RETURN NULL; END IF;
    SELECT COALESCE(jsonb_object_agg(key,value), '{}'::jsonb) INTO projected
    FROM jsonb_each(plan) WHERE key = ANY(ARRAY[
        'account_scope','symbol','side','episode_id','execution_mode',
        'stop_loss','take_profit','exit_reason']);
    FOREACH collection IN ARRAY ARRAY['entries','legs','exits','exit_order'] LOOP
        IF NOT plan ? collection THEN CONTINUE; END IF;
        IF collection = 'exit_order' THEN
            IF jsonb_typeof(plan->collection) <> 'object' THEN RETURN NULL; END IF;
            records := jsonb_build_array(plan->collection);
        ELSE
            IF jsonb_typeof(plan->collection) <> 'array' THEN RETURN NULL; END IF;
            records := plan->collection;
        END IF;
        projected := projected || jsonb_build_object(collection, '[]'::jsonb);
        FOR item IN SELECT value FROM jsonb_array_elements(records) LOOP
            IF jsonb_typeof(item) <> 'object' THEN RETURN NULL; END IF;
            SELECT COALESCE(jsonb_object_agg(key,value), '{}'::jsonb) INTO identity
            FROM jsonb_each(item) WHERE key = ANY(ARRAY[
                'id','price','execution_order_id','kind','order_type','contract_version',
                'exit_type','exchange_order_id','order_id','exchange_channel','trigger_price']);
            projected := jsonb_set(projected, ARRAY[collection], projected->collection || jsonb_build_array(identity));
        END LOOP;
    END LOOP;
    RETURN projected;
END;
$$;

CREATE OR REPLACE FUNCTION track_plan_source_revision() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_symbol TEXT;
    new_symbol TEXT;
    affected TEXT;
    prior JSONB;
    following JSONB;
BEGIN
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'real_protection_plans' THEN
        prior := ownership_plan_source(OLD.payload);
        following := ownership_plan_source(NEW.payload);
        IF OLD.config_id IS NOT DISTINCT FROM NEW.config_id
           AND OLD.symbol IS NOT DISTINCT FROM NEW.symbol
           AND OLD.side IS NOT DISTINCT FROM NEW.side
           AND prior IS NOT NULL AND following IS NOT NULL AND prior = following THEN
            RETURN NULL;
        END IF;
    END IF;
    IF TG_OP <> 'INSERT' THEN old_symbol := OLD.symbol; END IF;
    IF TG_OP <> 'DELETE' THEN new_symbol := NEW.symbol; END IF;
    FOR affected IN
        SELECT DISTINCT symbol FROM (VALUES(old_symbol),(new_symbol)) AS sources(symbol)
        WHERE symbol IS NOT NULL ORDER BY symbol
    LOOP
        INSERT INTO plan_source_revisions(symbol,revision) VALUES(affected,1)
        ON CONFLICT(symbol) DO UPDATE SET revision=plan_source_revisions.revision+1;
    END LOOP;
    RETURN NULL;
END;
$$;
