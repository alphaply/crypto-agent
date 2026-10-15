"""First PostgreSQL deployment and restart contracts, using disposable databases."""
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ID = "postgres-cutover"
PROMPT = "CUTOVER_CUSTOM_PROMPT for {symbol}: 保留导入的自定义内容。"
ANSWER = "PostgreSQL 中的聊天答复已经持久化。"


@pytest.mark.parametrize("explicit_legacy_path", [False, True])
def test_import_does_not_connect_or_create_sqlite(tmp_path, explicit_legacy_path):
    """An existing legacy file must not be silently copied during imports."""
    legacy = tmp_path / "legacy-project"
    legacy.mkdir()
    for name in ("trading_data.db", "trading_data.db-wal", "trading_data.db-shm"):
        (legacy / name).write_bytes(b"legacy SQLite input must remain untouched")
    data_dir = tmp_path / "postgres-data"
    explicit_path = tmp_path / "legacy-override" / "trading.sqlite"
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHON_DOTENV_DISABLED": "1",
        "DATA_DIR": str(data_dir),
        "DATABASE_URL": "postgresql://unused:unused@127.0.0.1:1/unreachable_test",
        "CONFIG_MASTER_KEY": "import-only-test-key",
        "SYMBOL_CONFIGS": "[]",
        "RUN_SCHEDULER_IN_WEB": "false",
        "TEST_LEGACY_ROOT": str(legacy),
        "TEST_EXPLICIT_PATH": str(explicit_path),
    }
    env.pop("TRADING_DB_PATH", None)
    env.pop("CHAT_CHECKPOINT_DB", None)
    if explicit_legacy_path:
        env["TRADING_DB_PATH"] = str(explicit_path)
    script = textwrap.dedent("""
        import os
        from pathlib import Path
        from unittest.mock import patch
        import backend.storage_paths as paths

        paths.PROJECT_ROOT = Path(os.environ['TEST_LEGACY_ROOT'])

        def forbidden(*args, **kwargs):
            raise AssertionError('Module import attempted to open a database or pool')

        with patch('psycopg.connect', forbidden), \
             patch('psycopg_pool.ConnectionPool.__init__', forbidden), \
             patch('sqlite3.connect', forbidden):
            import backend.config_store
            import backend.database
            import backend.config
            import backend.storage.checkpoints
            import backend.agent.chat_graph
            import backend.app.main
            import backend.mcp.server
            assert backend.storage._pool is None
            assert backend.storage.checkpoints._saver is None

        data = Path(os.environ['DATA_DIR'])
        assert not list(data.rglob('*.db*')), list(data.rglob('*.db*'))
        assert not list(data.rglob('*.sqlite*')), list(data.rglob('*.sqlite*'))
        assert not Path(os.environ['TEST_EXPLICIT_PATH']).parent.exists()
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (legacy / "trading_data.db").read_bytes() == b"legacy SQLite input must remain untouched"


@pytest.fixture
def empty_database_url(monkeypatch):
    """Deliberately omit migrations to test the application's startup gate."""
    address = os.getenv("TEST_POSTGRES_URL")
    if not address:
        pytest.skip("Set TEST_POSTGRES_URL to a disposable database with CREATEDB rights")
    assert "test" in urlsplit(address).path.lower()
    name = "startup_test_" + uuid4().hex
    target = urlunsplit(urlsplit(address)._replace(path="/" + name, query=""))
    from backend import storage
    from backend.storage.checkpoints import close_checkpoints

    storage.close_pool()
    close_checkpoints()
    with psycopg.connect(address, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        monkeypatch.setenv("DATABASE_URL", target)
        yield target
    finally:
        close_checkpoints()
        storage.close_pool()
        with psycopg.connect(address, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def startup_app(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "shared-data"))
    monkeypatch.setenv("CONFIG_MASTER_KEY", "target-postgres-master-key")
    monkeypatch.setenv("ADMIN_PASSWORD", "target-admin-password")
    monkeypatch.setenv("JWT_SECRET", "target-jwt-secret-for-startup-test")
    monkeypatch.setenv("SYMBOL_CONFIGS", "[]")
    monkeypatch.setenv("ENABLE_SCHEDULER", "true")
    monkeypatch.setenv("RUN_SCHEDULER_IN_WEB", "true")
    monkeypatch.setenv("DEPLOYMENT_MAINTENANCE", "true")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://before-import.invalid")
    monkeypatch.setenv("MCP_DB_WAIT_SECONDS", "5")
    monkeypatch.delenv("PROMPT_DIR", raising=False)

    from backend import config_store
    for key in set(config_store.GLOBAL_SECRET_ENV_MAP.values()) | {
        "LANGCHAIN_API_KEY", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "OPENAI_API_KEY",
    }:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config_store, "BASE_DIR", tmp_path)
    from backend.config import config
    old_config = deepcopy(config.__dict__)
    from backend.app.main import app
    from backend.app.core.deps import get_current_user
    from backend.app.core import runtime
    from backend.mcp import paper, server
    from backend.utils import resource_diagnostics

    scheduler = Mock(side_effect=AssertionError("Maintenance startup must not launch scheduler"))
    paper_worker = AsyncMock(side_effect=AssertionError("Maintenance startup must not launch Paper worker"))
    monkeypatch.setattr(runtime, "_run_scheduler_once", scheduler)
    monkeypatch.setattr(paper, "maintenance", paper_worker)
    monkeypatch.setattr(resource_diagnostics, "start_resource_collector", lambda *_: None)
    monkeypatch.setattr(resource_diagnostics, "stop_resource_collector", lambda: None)
    monkeypatch.setattr(server, "_server", None)
    monkeypatch.setattr(server, "_public_url", None)
    old_routes = list(app.router.routes)
    old_overrides = dict(app.dependency_overrides)
    old_mcp_routes = getattr(app.state, "mcp_routes", ())
    old_openapi = app.openapi_schema
    app.dependency_overrides[get_current_user] = lambda: {"sub": "startup-test-admin"}
    try:
        yield SimpleNamespace(app=app, scheduler=scheduler, paper_worker=paper_worker)
    finally:
        app.router.routes[:] = old_routes
        app.state.mcp_routes = old_mcp_routes
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
        app.openapi_schema = old_openapi
        config.__dict__.clear()
        config.__dict__.update(old_config)


def test_startup_refuses_missing_schema_without_creating_tables(empty_database_url, startup_app, monkeypatch):
    from backend.mcp import server
    from backend.storage.migrate import MigrationError

    install = Mock(side_effect=AssertionError("MCP installation must follow the schema check"))
    monkeypatch.setattr(server, "install_mcp", install)
    with pytest.raises(MigrationError, match="schema is missing.*backend.storage.migrate"):
        with TestClient(startup_app.app):
            pytest.fail("The app must not accept requests without migrated schemas")
    install.assert_not_called()
    startup_app.scheduler.assert_not_called()
    startup_app.paper_worker.assert_not_called()
    with psycopg.connect(empty_database_url) as conn:
        tables = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname IN ('public','checkpoints')"
        ).fetchall()
    assert tables == []


def _route_counts(app):
    return Counter(
        (route.path, method)
        for route in app.router.routes
        for method in (getattr(route, "methods", None) or {"MOUNT"})
    )


def _assert_discovery(client, origin):
    authorization = client.get("/.well-known/oauth-authorization-server")
    assert authorization.status_code == 200
    assert authorization.json()["issuer"] == origin + "/oauth"
    assert authorization.json()["token_endpoint"] == origin + "/oauth/token"
    resource = client.get("/.well-known/oauth-protected-resource/mcp")
    assert resource.status_code == 200
    assert resource.json()["resource"] == origin + "/mcp"
    assert resource.json()["authorization_servers"] == [origin + "/oauth"]
    assert resource.headers["cache-control"] == "no-store"
    challenge = client.get("/mcp")
    assert challenge.status_code == 401
    assert f'resource_metadata="{origin}/oauth/resource-metadata"' in challenge.headers["www-authenticate"]


def _assert_no_automatic_trading(startup_app):
    startup_app.scheduler.assert_not_called()
    startup_app.paper_worker.assert_not_called()
    from backend.storage import get_connection
    with get_connection() as conn:
        for table in ("paper_accounts", "paper_orders", "paper_operations", "paper_events", "trade_history"):
            assert conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0] == 0


def test_config_import_chat_and_simulation_survive_lifespan_restarts(checkpoint_url, startup_app, tmp_path, monkeypatch):
    """Exercise the real API, encryption, stores and PostgresSaver across three startups."""
    from backend import database, storage
    from backend.config import Config, config
    from backend.mcp import store
    from backend.agent import chat_graph
    from backend.storage import checkpoints
    from backend.utils.prompt_utils import render_prompt, resolve_prompt_template
    from langchain_core.messages import AIMessageChunk, SystemMessage

    captured_models = []
    captured_messages = []

    class FakeModel:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, config=None):
            captured_messages.append(messages)
            yield AIMessageChunk(
                content=ANSWER, id="postgres-cutover-answer",
                response_metadata={"finish_reason": "stop"},
                usage_metadata={"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
            )

    def fake_model(**kwargs):
        captured_models.append(kwargs)
        return FakeModel()

    def fake_market_context(state, config):
        agent = config["configurable"]["agent_config"]
        template = resolve_prompt_template(agent, agent["mode"], PROJECT_ROOT, chat_graph.logger)
        state.messages = [SystemMessage(content=render_prompt(template, symbol=agent["symbol"]))]
        return state

    monkeypatch.setattr(chat_graph, "build_chat_model", fake_model)
    monkeypatch.setattr(chat_graph, "scheduler_start_node", fake_market_context)
    stream_failures = Mock(wraps=chat_graph.logger.exception)
    monkeypatch.setattr(chat_graph.logger, "exception", stream_failures)
    env_before = {key: os.environ[key] for key in ("DATABASE_URL", "DATA_DIR", "CONFIG_MASTER_KEY", "ADMIN_PASSWORD", "JWT_SECRET")}
    env_file = tmp_path / ".env"
    env_file.write_text("# Target deployment bootstrap remains unchanged\n", encoding="utf-8")
    env_bytes = env_file.read_bytes()
    package = {
        "version": 1,
        "app_settings": {"enable_scheduler": True, "langchain_tracing": False},
        "agents": [{
            "config_id": CONFIG_ID, "symbol": "BTC/USDT", "mode": "STRATEGY", "enabled": True,
            "llm_provider_id": "cutover-model", "exchange_profile_id": "cutover-exchange",
            "prompt_file": "cutover-custom.txt",
        }],
        "llm_providers": [{
            "provider_id": "cutover-model", "name": "Imported model", "model": "fake-cutover-model",
            "api_base": "https://model.invalid/v1", "_secrets": {"api_key": "imported-model-key"},
        }],
        "exchange_profiles": [{
            "profile_id": "cutover-exchange", "name": "Imported account", "exchange": "binance", "market_type": "swap",
            "_secrets": {"api_key": "imported-exchange-key", "secret": "imported-exchange-secret"},
        }],
        "prompts": {"cutover-custom.txt": PROMPT},
        "model_pricing": [{"model": "fake-cutover-model", "input_price_per_m": 1.5, "output_price_per_m": 4.5, "currency": "USD"}],
        "mcp": {"settings": {"enabled": True, "public_url": "https://after-import.invalid"}, "profiles": [], "keys": [{
            "id": "cutover-paper-key", "secret": "ca_mcp_" + "test" * 12,
            "key_type": "paper", "paper_namespace": "cutover-paper", "scopes": ["read", "trade"], "profile_ids": [],
        }]},
        "env": {
            "DATABASE_URL": "postgresql://old:old@old.invalid/retired", "DATA_DIR": "/retired/data",
            "CONFIG_MASTER_KEY": "retired-master-key", "ADMIN_PASSWORD": "retired-password", "JWT_SECRET": "retired-jwt",
        },
    }

    with TestClient(startup_app.app, base_url="https://before-import.invalid") as client:
        _assert_discovery(client, "https://before-import.invalid")
        routes = _route_counts(startup_app.app)
        assert all(count == 1 for count in routes.values()), routes
        imported = client.post("/api/config/full-import", json={"data": package, "write_env": False})
        assert imported.status_code == 200, imported.text
        assert imported.json()["agents_imported"] == 1
        assert imported.json()["prompts_restored"] == ["cutover-custom.txt"]
        assert imported.json()["pricing_restored"] == 1
        assert imported.json()["env_written"] == []
        _assert_no_automatic_trading(startup_app)

    assert storage._pool is None
    assert checkpoints._saver is None
    prompt_file = Path(env_before["DATA_DIR"]) / "prompts" / "cutover-custom.txt"
    assert prompt_file.read_text(encoding="utf-8") == PROMPT
    assert env_file.read_bytes() == env_bytes
    assert {key: os.environ[key] for key in env_before} == env_before

    # Discard in-memory state so the next startup must recover the imported DB snapshot.
    config.__dict__.clear()
    config.__dict__.update(Config(defer_load=True).__dict__)
    with TestClient(startup_app.app, base_url="https://after-import.invalid") as client:
        _assert_discovery(client, "https://after-import.invalid")
        assert _route_counts(startup_app.app) == routes
        agent = config.get_config_by_id(CONFIG_ID)
        assert agent["api_key"] == "imported-model-key"
        assert config.get_exchange_credentials(config_id=CONFIG_ID)[1:3] == ("imported-exchange-key", "imported-exchange-secret")
        with storage.get_connection() as conn:
            encrypted = [row[0] for row in conn.execute("SELECT encrypted_value FROM secret_store")]
        assert encrypted
        assert all("imported-" not in value for value in encrypted)
        created = client.post("/api/chat/sessions", json={"config_id": CONFIG_ID, "title": "Cutover proof"})
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        response = client.post(f"/api/chat/sessions/{session_id}/stream", json={"message": "重启后保留这段对话"})
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert not [event for event in events if event["type"] == "error"], events
        done = next(event for event in events if event["type"] == "done")
        assert done["persisted"] is True, done
        assert done["persistence_error"] is None
        assert done["messages"][-1]["content"] == ANSWER
        stream_failures.assert_not_called()
        assert len(captured_models) == 1
        assert captured_models[0]["api_key"] == "imported-model-key"
        assert captured_models[0]["base_url"] == "https://model.invalid/v1"
        assert captured_models[0]["model"] == "fake-cutover-model"
        assert any(PROMPT.format(symbol="BTC/USDT") in str(message.content) for message in captured_messages[0])
        assert database.get_mock_account(CONFIG_ID, "BTC/USDT")["balance"] == 10000
        assert database.update_mock_account_balance(CONFIG_ID, "BTC/USDT", 123)[0] == 10123
        database.create_mock_order("BTC/USDT", "BUY", 50000, 0.01, 49000, 52000, CONFIG_ID,
                                   config_id=CONFIG_ID, order_id="cutover-open-order")
        orders = database.get_mock_orders(config_id=CONFIG_ID)
        assert [order["order_id"] for order in orders] == ["cutover-open-order"]
        assert "storage_id" not in orders[0]
        _assert_no_automatic_trading(startup_app)
        store.put("settings", "global", {"enabled": True, "public_url": "https://after-restart.invalid"})

    assert storage._pool is None
    assert checkpoints._saver is None
    config.__dict__.clear()
    config.__dict__.update(Config(defer_load=True).__dict__)
    with TestClient(startup_app.app, base_url="https://after-restart.invalid") as client:
        _assert_discovery(client, "https://after-restart.invalid")
        assert _route_counts(startup_app.app) == routes
        history = client.get(f"/api/chat/sessions/{session_id}/messages")
        assert history.status_code == 200, history.text
        assert [(item["role"], item["content"]) for item in history.json()["messages"]] == [
            ("user", "重启后保留这段对话"), ("assistant", ANSWER),
        ]
        assert config.get_config_by_id(CONFIG_ID)["api_key"] == "imported-model-key"
        assert prompt_file.read_text(encoding="utf-8") == PROMPT
        assert database.get_mock_account(CONFIG_ID, "BTC/USDT")["balance"] == 10123
        assert database.get_mock_orders(config_id=CONFIG_ID) == orders
        assert database.get_all_pricing()["fake-cutover-model"]["input_price_per_m"] == 1.5
        assert database.get_all_pricing()["fake-cutover-model"]["output_price_per_m"] == 4.5
        with storage.get_connection() as conn:
            assert conn.execute("SELECT total_tokens FROM token_usage WHERE config_id=%s", (CONFIG_ID,)).fetchone()[0] == 19
        _assert_no_automatic_trading(startup_app)
        assert len(captured_models) == 1

    assert not list(Path(env_before["DATA_DIR"]).rglob("*.db*"))
    assert not list(Path(env_before["DATA_DIR"]).rglob("*.sqlite*"))
    assert env_file.read_bytes() == env_bytes
    assert {key: os.environ[key] for key in env_before} == env_before
