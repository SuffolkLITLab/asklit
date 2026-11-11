"""Idempotent protection for hosted tables, including newly migrated ones."""

PRIVACY_SQL = """
    DO $block$
    DECLARE item record; role_name text;
    BEGIN
      FOR item IN SELECT schemaname, tablename FROM pg_tables
        WHERE schemaname = current_schema()
          AND (tablename LIKE 'hosted\\_%' ESCAPE '\\' OR tablename = 'asklit_schema_version')
      LOOP
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', item.schemaname, item.tablename);
        EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM PUBLIC', item.schemaname, item.tablename);
        FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM %I', item.schemaname, item.tablename, role_name);
          END IF;
        END LOOP;
      END LOOP;
    END $block$;
    """
