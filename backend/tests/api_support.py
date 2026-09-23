"""Isolated authenticated HTTP fixture for the catalog integration tests."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.repositories import create_user
from app.storage import migrate


class AuthenticatedApiTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        path = str(self.folder / "service.sqlite3")
        environment = patch.dict(os.environ, {
            "APP_DB_PATH": path, "ORDER_DB_PATH": path, "APP_ENV": "development",
            "APP_ORIGIN": "", "JOB_TIMEOUT_SECONDS": "600", "WORKER_STALE_SECONDS": "30",
        })
        environment.start()
        self.addCleanup(environment.stop)
        migrate()
        self.user = create_user("catalog-manager", "catalog-test-password", "manager")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        response = self.client.post("/api/auth/login", json={
            "username": "catalog-manager", "password": "catalog-test-password",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
