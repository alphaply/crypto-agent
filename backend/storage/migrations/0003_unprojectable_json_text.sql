-- TEXT payloads remain the source of truth. JSON can contain escaped characters
-- (notably \u0000) that PostgreSQL JSONB cannot represent; projection maintenance
-- must not reject those raw records or silently replace their content.
CREATE OR REPLACE FUNCTION storage_json(value TEXT) RETURNS JSONB
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
BEGIN
    RETURN value::jsonb;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
    OR untranslatable_character THEN
    RETURN NULL;
END;
$$;
