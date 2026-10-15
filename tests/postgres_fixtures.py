"""Fixtures only accept an explicitly selected disposable PostgreSQL database."""
import os
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4
import psycopg
from psycopg import sql
import pytest
from backend import storage

@pytest.fixture
def financial_pg(monkeypatch, tmp_path):
    url = os.environ.get('TEST_POSTGRES_URL')
    if not url:
        pytest.skip('TEST_POSTGRES_URL must name an isolated PostgreSQL test database')
    if 'test' not in urlsplit(url).path.lower():
        pytest.fail('TEST_POSTGRES_URL database name must contain test')
    schema = 'financial_test_' + uuid4().hex
    storage.close_pool()
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        admin.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        for path in sorted((Path(__file__).parents[1] / 'backend/storage/migrations').glob('*.sql')):
            admin.execute(path.read_text(encoding='utf-8'))
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query['options'] = '-csearch_path=' + schema
    scoped_url = urlunsplit((*parts[:3], urlencode(query, quote_via=quote), parts.fragment))
    monkeypatch.setenv('DATABASE_URL', scoped_url)
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'financial-isolated-test-key')
    monkeypatch.setenv('MCP_DB_WAIT_SECONDS', '5')
    try:
        yield scoped_url
    finally:
        storage.close_pool()
        with psycopg.connect(url, autocommit=True) as admin:
            # A leaked test transaction should fail teardown visibly instead of
            # hanging the entire regression run behind DROP SCHEMA forever.
            admin.execute("SET lock_timeout = '10s'")
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture
def pg(financial_pg):
    yield storage.get_connection


@pytest.fixture
def checkpoint_url(monkeypatch):
    address = os.getenv('TEST_POSTGRES_URL')
    if not address:
        pytest.skip('Set TEST_POSTGRES_URL to an isolated test database with CREATEDB rights')
    name = 'checkpoint_test_' + uuid4().hex
    parsed = urlsplit(address)
    target = urlunsplit(parsed._replace(path='/' + name, query=''))
    with psycopg.connect(address, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    from backend.storage.checkpoints import close_checkpoints
    from backend.storage.migrate import migrate
    storage.close_pool()
    close_checkpoints()
    try:
        migrate(dsn=target)
        monkeypatch.setenv('DATABASE_URL', target)
        yield target
    finally:
        close_checkpoints()
        storage.close_pool()
        with psycopg.connect(address, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


@pytest.fixture
def pg_cli(tmp_path):
    """Use local PG clients, or the pinned PG17 Docker client on Windows."""
    import shutil
    import subprocess

    def run(program, args, dsn, *, data=None):
        executable = shutil.which(program)
        from psycopg.conninfo import conninfo_to_dict
        def variables(url):
            names = {'host':'PGHOST', 'port':'PGPORT', 'user':'PGUSER', 'password':'PGPASSWORD',
                     'dbname':'PGDATABASE', 'options':'PGOPTIONS', 'sslmode':'PGSSLMODE'}
            return {names[k]:v for k,v in conninfo_to_dict(url).items() if k in names}
        container_name = None
        if executable:
            env = dict(os.environ, **variables(dsn))
            command = [executable, *args]
        else:
            parsed = urlsplit(dsn)
            if parsed.hostname in {'127.0.0.1', 'localhost'}:
                host = 'host.docker.internal'
                authority = parsed.netloc.rsplit('@', 1)[0] + '@' + host + ':' + str(parsed.port or 5432)
                dsn = urlunsplit(parsed._replace(netloc=authority))
            environment = tmp_path / 'pg-client.env'
            environment.write_text(''.join(k+'='+v+'\n' for k,v in variables(dsn).items()) + 'PGCONNECT_TIMEOUT=5\n', encoding='utf-8')
            container_name = 'crypto-pg-client-test-' + uuid4().hex
            command = ['docker', 'run', '--rm', '--name', container_name,
                       '--label', 'crypto-agent.task=upgrade-postgresql-storage', '-i', '--env-file', str(environment),
                       'postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24',
                       program, *args]
            env = None
        try:
            result = subprocess.run(command, input=data, capture_output=True, env=env, timeout=60)
        finally:
            if container_name:
                subprocess.run(['docker', 'rm', '-f', container_name], capture_output=True, timeout=15)
        if result.returncode:
            # Do not include server SQL or credentials in pytest assertion output.
            raise RuntimeError(f'{program} failed with exit {result.returncode}')
        return result.stdout
    return run


@pytest.fixture
def backup_client(pg_cli, monkeypatch):
    from backend.app.services import database_export
    def dump(path):
        with storage.get_connection() as conn:
            schema = conn.execute('SELECT current_schema()').fetchone()[0]
        # Other test processes create/drop their own schemas in the shared test DB.
        data = pg_cli('pg_dump', ['--format=custom', '--no-owner', '--no-privileges',
                                  '--schema', schema], storage.database_url())
        Path(path).write_bytes(data)
        return Path(path)
    monkeypatch.setattr(database_export, 'dump_database', dump)
    return dump


@pytest.fixture
def restore_dump(pg_cli):
    created = []
    admin_url = os.environ['TEST_POSTGRES_URL']
    def restore(data):
        name = 'restore_test_' + uuid4().hex
        target = urlunsplit(urlsplit(storage.database_url())._replace(path='/' + name))
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created.append(name)
        pg_cli('pg_restore', ['--exit-on-error','--no-owner','--no-privileges','--dbname',name], target, data=data)
        return target
    try:
        yield restore
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            for name in created:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
