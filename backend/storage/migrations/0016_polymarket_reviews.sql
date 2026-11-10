-- Configuration revisions also change on manual writes (including delete/reinsert).
CREATE TABLE polymarket_watchlist_revision (
    id INTEGER PRIMARY KEY CHECK (id = 1), revision BIGINT NOT NULL DEFAULT 0
);
INSERT INTO polymarket_watchlist_revision(id) VALUES (1);
CREATE FUNCTION advance_polymarket_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'DELETE' AND OLD.key = 'polymarket') OR
       (TG_OP <> 'DELETE' AND NEW.key = 'polymarket') THEN
        UPDATE polymarket_watchlist_revision SET revision = revision + 1 WHERE id = 1;
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER polymarket_configuration_changed AFTER INSERT OR UPDATE OR DELETE ON app_settings
    FOR EACH ROW EXECUTE FUNCTION advance_polymarket_revision();
CREATE TABLE polymarket_reviews (
    run_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    before_revision BIGINT NOT NULL, before_json TEXT NOT NULL,
    after_json TEXT, decisions_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL, checked_at TEXT, published_at TEXT, error TEXT,
    snapshot_id BIGINT REFERENCES global_news_snapshots(id) ON DELETE SET NULL
);
CREATE INDEX polymarket_reviews_time ON polymarket_reviews(started_at DESC);
