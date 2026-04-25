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

    def test_protected_routes_require_auth(self):
        for path in ("/api/stats/tokens", "/api/config"):
            with self.subTest(path=path):
                response = self.client.get(path)
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


if __name__ == "__main__":
    unittest.main()
