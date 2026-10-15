-- Evidence size never makes valid plan state UNKNOWN.
CREATE OR REPLACE FUNCTION project_protection_activity() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parsed JSONB;
    projected_state TEXT := 'UNKNOWN';
    projected_updated DOUBLE PRECISION;
    payload_size BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM protection_plan_activity WHERE plan_rowid = OLD.storage_id;
        RETURN OLD;
    END IF;
    payload_size := octet_length(convert_to(NEW.payload, 'UTF8'));
    parsed := storage_json(NEW.payload);
    IF jsonb_typeof(parsed) = 'object' THEN
        projected_state := COALESCE(parsed->>'state', 'UNKNOWN');
        IF jsonb_typeof(parsed->'updated_at') = 'number' THEN
            BEGIN
                projected_updated := (parsed->>'updated_at')::double precision;
            EXCEPTION WHEN numeric_value_out_of_range THEN
                projected_updated := NULL;
            END;
        END IF;
    END IF;
    INSERT INTO protection_plan_activity(plan_rowid, config_id, state, updated_at, payload_bytes)
    VALUES(NEW.storage_id, NEW.config_id, projected_state, projected_updated, payload_size)
    ON CONFLICT(plan_rowid) DO UPDATE SET config_id=EXCLUDED.config_id,
        state=EXCLUDED.state, updated_at=EXCLUDED.updated_at, payload_bytes=EXCLUDED.payload_bytes;
    RETURN NEW;
END;
$$;

UPDATE protection_plan_scan_progress SET cursor=0,complete=0 WHERE id=1;
UPDATE protection_plan_scan_mode SET limits_enabled=0 WHERE id=1;

-- Revisit parents previously hidden solely because their payload was large.
DELETE FROM paper_batch_status WHERE status='audit_only';
