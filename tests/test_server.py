"""Smoke test for the real stdlib HTTP server over a loopback socket.

Confirms the :mod:`api.server` adapter (request reading, status/body writing,
headers) works against an actual ``http.client`` connection, not just
``Application.dispatch``.

The Track A SQLite connection is bound to the thread that opens it, and the
server here runs in a background thread, so the database, app, and server are
all constructed *inside* that thread (which is exactly how a single-threaded
production deployment behaves). An in-memory DB keeps it isolated.
"""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import HTTPServer

from api.app import build_app
from api.server import _make_handler
from config.settings import load_settings
from db.connection import Database


class ServerSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._ready = threading.Event()
        self.httpd: HTTPServer | None = None
        self.port = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.assertTrue(self._ready.wait(timeout=5), "server did not start")

    def _serve(self) -> None:
        db = Database(":memory:")
        db.init_schema()
        app = build_app(db, settings=load_settings())
        httpd = HTTPServer(("127.0.0.1", 0), _make_handler(app))
        self.httpd = httpd
        self.port = httpd.server_address[1]
        self._ready.set()
        try:
            httpd.serve_forever()
        finally:
            db.close()

    def tearDown(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.thread.join(timeout=5)

    def _conn(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_health_over_socket(self) -> None:
        conn = self._conn()
        conn.request("GET", "/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.read())["status"], "ok")
        conn.close()

    def test_create_and_run_over_socket(self) -> None:
        conn = self._conn()
        payload = json.dumps(
            {
                "name": "payroll",
                "source_language": "cobol",
                "target_language": "python",
                "source_files": [{"path": "p.cob", "content": "MOVE 1 TO X\n"}],
            }
        )
        conn.request(
            "POST", "/jobs", body=payload,
            headers={"Content-Type": "application/json"},
        )
        create_resp = conn.getresponse()
        self.assertEqual(create_resp.status, 201)
        created = json.loads(create_resp.read())
        conn.close()

        conn = self._conn()
        conn.request("POST", f"/jobs/{created['id']}/run")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.read())["report"]["succeeded"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
