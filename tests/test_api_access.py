import os
import unittest

os.environ.setdefault("RUN_SCHEDULER_IN_WEB", "false")

from fastapi.testclient import TestClient

from backend.app.core.security import get_expected_password
from backend.app.main import app


class ApiAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_public_usage_is_open(self):
        response = self.client.get("/api/public/usage")
        self.assertEqual(response.status_code, 200)
        self.assertIn("summary", response.json())

    def test_public_history_is_open(self):
        response = self.client.get("/api/public/history", params={"symbol": "BTC/USDT"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("summaries", response.json())

    def test_public_daily_summaries_is_open(self):
        response = self.client.get("/api/public/daily-summaries", params={"symbol": "BTC/USDT"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("daily_summaries", response.json())

    def test_protected_routes_require_auth(self):
        for path in ("/api/stats/tokens", "/api/config", "/api/history/daily-summaries/generate"):
            with self.subTest(path=path):
                if path.endswith("/generate"):
                    response = self.client.post(path, json={"config_id": "missing", "date": "2026-05-06"})
                else:
                    response = self.client.get(path)
                self.assertEqual(response.status_code, 401)

    def test_daily_summary_writes_require_auth(self):
        checks = [
            ("put", "/api/history/daily-summaries", {"date": "2026-05-06", "config_id": "missing", "summary": ""}),
            ("delete", "/api/history/daily-summaries", {"date": "2026-05-06", "config_id": "missing"}),
            ("post", "/api/history/clean", {"symbol": "BTC/USDT"}),
            ("post", "/api/stats/position/protection", {"config_id": "missing", "symbol": "BTC/USDT", "side": "LONG"}),
        ]
        for method, path, body in checks:
            with self.subTest(path=path):
                if method == "delete":
                    response = self.client.request("DELETE", path, json=body)
                else:
                    response = getattr(self.client, method)(path, json=body)
                self.assertEqual(response.status_code, 401)

    def test_login_unlocks_config(self):
        password = get_expected_password()
        self.assertTrue(password)

        login = self.client.post("/api/auth/login", json={"password": password})
        self.assertEqual(login.status_code, 200)
        token = login.json()["token"]

        response = self.client.get("/api/config", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("globals", data)
        self.assertIn("agents", data)

    def test_database_export(self):
        res = self.client.get("/api/config/database/export")
        self.assertEqual(res.status_code, 401)

        password = get_expected_password()
        login = self.client.post("/api/auth/login", json={"password": password})
        token = login.json()["token"]

        res = self.client.get("/api/config/database/export", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("application/x-sqlite3", res.headers.get("content-type", ""))
        self.assertTrue(res.headers.get("content-disposition", "").startswith('attachment; filename="trading_data_'))
        self.assertTrue(res.content.startswith(b"SQLite format 3\x00"))


if __name__ == "__main__":
    unittest.main()
