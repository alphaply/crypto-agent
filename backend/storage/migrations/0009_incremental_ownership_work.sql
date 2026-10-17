-- Commit-visible per-source versions: identity allocation order is not commit order.
-- Extract each identity once. Appending one JSONB array element per iteration
-- was quadratic for large plans; keep the existing comparison projection shape.
CREATE OR REPLACE FUNCTION ownership_plan_source(raw TEXT) RETURNS JSONB
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
    plan JSONB := storage_json(raw);
    projected JSONB;
    collection TEXT;
    records JSONB;
    identities JSONB;
BEGIN
    IF plan IS NULL OR jsonb_typeof(plan) <> 'object' THEN RETURN NULL; END IF;
    SELECT COALESCE(jsonb_object_agg(key,value), '{}'::jsonb) INTO projected
        FROM jsonb_each(plan) WHERE key = ANY(ARRAY[
        'account_scope','symbol','side','episode_id','execution_mode',
        'stop_loss','take_profit','exit_reason']);
    FOREACH collection IN ARRAY ARRAY['entries','legs','exits','exit_order'] LOOP
        IF NOT plan ? collection THEN CONTINUE; END IF;
        IF collection = 'exit_order' THEN
            IF plan->collection = 'null'::jsonb THEN CONTINUE; END IF;
            IF jsonb_typeof(plan->collection) <> 'object' THEN RETURN NULL; END IF;
            records := jsonb_build_array(plan->collection);
        ELSE
            IF jsonb_typeof(plan->collection) <> 'array' THEN RETURN NULL; END IF;
            records := plan->collection;
        END IF;
        IF EXISTS(SELECT 1 FROM jsonb_array_elements(records) item
                  WHERE jsonb_typeof(item) <> 'object') THEN RETURN NULL; END IF;
        SELECT COALESCE(jsonb_agg(COALESCE((
            SELECT jsonb_object_agg(key,value) FROM jsonb_each(item)
            WHERE key = ANY(ARRAY['id','price','execution_order_id','kind','order_type',
              'contract_version','exit_type','exchange_order_id','order_id',
              'exchange_channel','trigger_price'])), '{}'::jsonb) ORDER BY ordinal), '[]'::jsonb)
            INTO identities FROM jsonb_array_elements(records) WITH ORDINALITY AS items(item,ordinal);
        projected := projected || jsonb_build_object(collection,identities);
    END LOOP;
    RETURN projected;
END;
$$;

CREATE TABLE ownership_sources (
    source TEXT NOT NULL, source_id BIGINT NOT NULL, symbol TEXT NOT NULL,
    owner TEXT NOT NULL, version BIGINT NOT NULL DEFAULT 1,
    payload TEXT, deleted BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY(source,source_id,symbol)
);
CREATE INDEX ownership_sources_symbol ON ownership_sources(symbol,source,source_id);
ALTER TABLE execution_plan_index_records ADD COLUMN source_version BIGINT NOT NULL DEFAULT 0;
ALTER TABLE execution_plan_index_records ADD COLUMN relevant BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE execution_plan_index_records ADD COLUMN source_symbol TEXT NOT NULL DEFAULT '';
ALTER TABLE execution_plan_index_records DROP CONSTRAINT execution_plan_index_records_index_id_source_source_id_key;
ALTER TABLE execution_plan_index_records ADD UNIQUE(index_id,source,source_id,source_symbol);
ALTER TABLE execution_plan_index_runs ADD COLUMN incremental BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX execution_index_fingerprint ON execution_plan_index_records(index_id,fingerprint) WHERE relevant;
CREATE TABLE ownership_projections (
    index_id TEXT NOT NULL, fingerprint TEXT NOT NULL, owner TEXT NOT NULL,
    payload TEXT NOT NULL, facts JSONB NOT NULL,
    published BOOLEAN NOT NULL DEFAULT FALSE,
    publish_cursor BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY(index_id,fingerprint)
);
CREATE TABLE execution_history_work (
    config_id TEXT NOT NULL, account_scope TEXT NOT NULL, symbol TEXT NOT NULL,
    version BIGINT NOT NULL DEFAULT 1,
    attempted_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY(config_id,account_scope,symbol)
);

CREATE FUNCTION ownership_source_value(table_name TEXT, raw JSONB) RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE projected JSONB;
BEGIN
    IF table_name = 'orders' THEN
        IF raw->>'trade_mode' IS DISTINCT FROM 'REAL' THEN RETURN '{}'::TEXT; END IF;
        RETURN (SELECT jsonb_object_agg(key,value) FROM jsonb_each(raw)
                WHERE key = ANY(ARRAY['config_id','order_id','side','entry_price',
                                     'take_profit','stop_loss','event_type','reason']))::TEXT;
    END IF;
    projected := ownership_plan_source(raw->>'payload');
    IF projected ? 'exit_order' THEN
        projected := jsonb_set(projected, ARRAY['exit_order'],projected->'exit_order'->0);
    END IF;
    RETURN projected::TEXT;
END;
$$;

CREATE FUNCTION capture_ownership_source() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    src TEXT := CASE TG_TABLE_NAME WHEN 'orders' THEN 'legacy'
                 WHEN 'real_protection_events' THEN 'events' ELSE 'plans' END;
    prior TEXT;
    following TEXT;
    active_old BOOLEAN := FALSE;
    active_new BOOLEAN := FALSE;
BEGIN
    IF TG_OP <> 'DELETE' THEN
        following := ownership_source_value(TG_TABLE_NAME, to_jsonb(NEW));
        active_new := NEW.symbol IS NOT NULL AND NEW.config_id IS NOT NULL AND
            (TG_TABLE_NAME <> 'orders' OR COALESCE(to_jsonb(NEW)->>'trade_mode' = 'REAL',FALSE));
    END IF;
    IF TG_OP <> 'INSERT' THEN
        prior := ownership_source_value(TG_TABLE_NAME, to_jsonb(OLD));
        active_old := OLD.symbol IS NOT NULL AND OLD.config_id IS NOT NULL AND
            (TG_TABLE_NAME <> 'orders' OR COALESCE(to_jsonb(OLD)->>'trade_mode' = 'REAL',FALSE));
        IF TG_OP = 'UPDATE' AND OLD.symbol = NEW.symbol AND OLD.config_id = NEW.config_id
           AND prior IS NOT NULL AND following IS NOT NULL AND prior = following THEN
            RETURN NULL;
        END IF;
        IF active_old AND (TG_OP = 'DELETE' OR NOT active_new OR OLD.symbol IS DISTINCT FROM NEW.symbol) THEN
            INSERT INTO ownership_sources(source,source_id,symbol,owner,deleted)
            VALUES(src,OLD.storage_id,OLD.symbol,OLD.config_id,TRUE)
            ON CONFLICT(source,source_id,symbol) DO UPDATE
                SET version=ownership_sources.version+1,payload=NULL,deleted=TRUE;
        END IF;
    END IF;
    IF active_new THEN
        INSERT INTO ownership_sources(source,source_id,symbol,owner,payload)
        VALUES(src,NEW.storage_id,NEW.symbol,NEW.config_id,following)
        ON CONFLICT(source,source_id,symbol) DO UPDATE
            SET version=ownership_sources.version+1,owner=excluded.owner,
                payload=excluded.payload,deleted=FALSE;
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER ownership_events AFTER INSERT OR UPDATE OR DELETE ON real_protection_events
    FOR EACH ROW EXECUTE FUNCTION capture_ownership_source();
CREATE TRIGGER ownership_plans AFTER INSERT OR UPDATE OR DELETE ON real_protection_plans
    FOR EACH ROW EXECUTE FUNCTION capture_ownership_source();
CREATE TRIGGER ownership_orders AFTER INSERT OR UPDATE OR DELETE ON orders
    FOR EACH ROW EXECUTE FUNCTION capture_ownership_source();

-- Set-based initial capture runs in the migration transaction, before workers start.
INSERT INTO ownership_sources(source,source_id,symbol,owner,payload)
SELECT 'events',storage_id,symbol,config_id,ownership_source_value('real_protection_events',to_jsonb(e)) FROM real_protection_events e;
INSERT INTO ownership_sources(source,source_id,symbol,owner,payload)
SELECT 'plans',storage_id,symbol,config_id,ownership_source_value('real_protection_plans',to_jsonb(p)) FROM real_protection_plans p;
INSERT INTO ownership_sources(source,source_id,symbol,owner,payload)
SELECT 'legacy',storage_id,symbol,config_id,ownership_source_value('orders',to_jsonb(o)) FROM orders o
WHERE trade_mode='REAL' AND symbol IS NOT NULL AND config_id IS NOT NULL;

-- Queue history in the source transaction, including raw external fills that
-- can close an owned cycle. A worker crash cannot lose the refresh request.
CREATE FUNCTION queue_execution_history() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.payload IS NOT DISTINCT FROM NEW.payload
       AND OLD.config_id IS NOT DISTINCT FROM NEW.config_id THEN RETURN NULL; END IF;
    IF TG_OP <> 'DELETE' THEN
        INSERT INTO execution_history_work(config_id,account_scope,symbol)
        SELECT DISTINCT config_id,account_scope,symbol FROM execution_order_links
            WHERE account_scope=NEW.account_scope AND symbol=NEW.symbol
        UNION SELECT NEW.config_id,NEW.account_scope,NEW.symbol WHERE NEW.config_id IS NOT NULL
        ON CONFLICT(config_id,account_scope,symbol) DO UPDATE
            SET version=execution_history_work.version+1;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        INSERT INTO execution_history_work(config_id,account_scope,symbol)
        SELECT DISTINCT config_id,account_scope,symbol FROM execution_order_links
            WHERE account_scope=OLD.account_scope AND symbol=OLD.symbol
        UNION SELECT OLD.config_id,OLD.account_scope,OLD.symbol WHERE OLD.config_id IS NOT NULL
        ON CONFLICT(config_id,account_scope,symbol) DO UPDATE
            SET version=execution_history_work.version+1;
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER history_fills AFTER INSERT OR UPDATE OR DELETE ON execution_fills
    FOR EACH ROW EXECUTE FUNCTION queue_execution_history();
CREATE TRIGGER history_links AFTER INSERT OR UPDATE OR DELETE ON execution_order_links
    FOR EACH ROW EXECUTE FUNCTION queue_execution_history();
