"""Baseline tests: they record how the app behaves TODAY, before any hardening.

They use a throwaway database in a temp folder, so your real links.db and the
Docker volume are never touched. Later steps will update these tests as the
behaviour is deliberately changed (e.g. URL validation).
"""
import os
import sqlite3
import tempfile
import unittest

from werkzeug.security import generate_password_hash

TEST_PASSWORD_HASH_METHOD = os.getenv("TEST_PASSWORD_HASH_METHOD", "pbkdf2:sha256:1")

# DATABASE is read when app.py is imported, so set it first.
_TMP = tempfile.TemporaryDirectory()
os.environ["DATABASE"] = os.path.join(_TMP.name, "test.db")
os.environ["ADMIN_PASSWORD_HASH"] = generate_password_hash(
    "test-password",
    method=TEST_PASSWORD_HASH_METHOD,
)

import app as shortener  # noqa: E402

# The schema as the real database has it (create_database() in app.py only
# makes `links` without `batch_id`; step 2 of the plan fixes that).
SCHEMA = """
CREATE TABLE links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    url TEXT NOT NULL,
    clicks INTEGER DEFAULT 0,
    created_at TEXT,
    batch_id TEXT
);
CREATE TABLE access_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address TEXT,
    method TEXT,
    path TEXT,
    status_code INTEGER,
    user_agent TEXT,
    created_at TEXT
);
"""


def query(sql, args=()):
    connection = sqlite3.connect(os.environ["DATABASE"])
    try:
        return connection.execute(sql, args).fetchall()
    finally:
        connection.close()


class BaselineTests(unittest.TestCase):

    def setUp(self):
        super().setUp()

        # Reset the production limiters between isolated tests.
        shortener._login_failures.clear()
        shortener._link_creation_requests.clear()
        shortener._bulk_creation_requests.clear()

        path = os.environ["DATABASE"]
        if os.path.exists(path):
            os.remove(path)
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        connection.close()
        shortener.app.config["TESTING"] = True
        self.client = shortener.app.test_client()

    def add_link(self, code, url):
        connection = sqlite3.connect(os.environ["DATABASE"])
        connection.execute(
            "INSERT INTO links (code, url, clicks, created_at) VALUES (?, ?, 0, 'now')",
            (code, url),
        )
        connection.commit()
        connection.close()

    def test_home_page_loads(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_link_creation_rate_limit_blocks_after_ten_requests(self):
        for index in range(10):
            response = self.client.post(
                "/",
                data={"url": f"https://example.com/{index}"},
                follow_redirects=False
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post(
            "/",
            data={"url": "https://example.com/blocked"},
            follow_redirects=False
        )

        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)

    def test_bulk_50_urls_succeeds(self):
        self.login()

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        response = self.client.post(
            "/bulk",
            data={
                "urls": "\n".join(f"https://example.com/{i}" for i in range(50)),
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(query("SELECT COUNT(*) FROM links"), [(50,)])

    def test_bulk_over_limit_returns_429_and_retry_after(self):
        self.login()

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        for _ in range(6):
            response = self.client.post(
                "/bulk",
                data={
                    "urls": "\n".join(f"https://example.com/{i}" for i in range(50)),
                    "csrf_token": csrf_token,
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post(
            "/bulk",
            data={
                "urls": "\n".join(f"https://example.com/{i}" for i in range(50)),
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)

    def test_bulk_creation_does_not_use_public_creation_allowance(self):
        self.login()

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        response = self.client.post(
            "/bulk",
            data={
                "urls": "\n".join(f"https://example.com/{i}" for i in range(5)),
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/",
            data={"url": "https://example.com/public"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)

    def test_creation_limiter_prunes_expired_entries_and_caps_memory(self):
        for index in range(shortener.LINK_CREATE_MAX_TRACKED_ADDRESSES + 5):
            shortener.rate_limit_request(
                f"client-{index}",
                shortener._link_creation_requests,
                shortener.LINK_CREATE_MAX_REQUESTS,
                shortener.LINK_CREATE_WINDOW_SECONDS,
                count=1,
            )

        self.assertLessEqual(
            len(shortener._link_creation_requests),
            shortener.LINK_CREATE_MAX_TRACKED_ADDRESSES,
        )

    def test_get_client_ip_uses_trusted_proxy_x_real_ip(self):
        with shortener.app.test_request_context(
            "/",
            headers={"X-Real-IP": "203.0.113.42"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        ):
            self.assertEqual(shortener.get_client_ip(), "203.0.113.42")

    def test_get_client_ip_rejects_untrusted_proxy_headers(self):
        with shortener.app.test_request_context(
            "/",
            headers={"X-Real-IP": "203.0.113.42"},
            environ_overrides={"REMOTE_ADDR": "198.51.100.7"},
        ):
            self.assertEqual(shortener.get_client_ip(), "198.51.100.7")

    def test_get_client_ip_falls_back_to_remote_addr_on_invalid_header(self):
        with shortener.app.test_request_context(
            "/",
            headers={"X-Real-IP": "not-an-ip"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        ):
            self.assertEqual(shortener.get_client_ip(), "127.0.0.1")

    def test_host_header_cannot_change_generated_short_url(self):
        response = self.client.post(
            "/",
            data={"url": "https://example.com"},
            headers={"Host": "attacker.example"}
        )

        self.assertEqual(response.status_code, 200)

        body = response.get_data(as_text=True)

        self.assertIn(
            "http://127.0.0.1:5000/",
            body
        )

        self.assertNotIn(
            "http://attacker.example/",
            body
        )

    def test_create_short_link(self):
        response = self.client.post("/", data={"url": "https://example.com"})
        self.assertEqual(response.status_code, 200)
        rows = query("SELECT code, url, clicks FROM links")
        self.assertEqual(len(rows), 1)
        code, url, clicks = rows[0]
        self.assertEqual(len(code), 6)
        self.assertEqual(url, "https://example.com")
        self.assertEqual(clicks, 0)
        self.assertIn(code, response.get_data(as_text=True))

    def test_create_short_link_rejects_unsafe_scheme(self):
        response = self.client.post("/", data={"url": "javascript:alert(1)"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(query("SELECT COUNT(*) FROM links"), [(0,)])

    def test_create_short_link_rejects_missing_hostname(self):
        response = self.client.post("/", data={"url": "https:///missing-host"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(query("SELECT COUNT(*) FROM links"), [(0,)])

    def test_create_short_link_rejects_overlong_url(self):
        response = self.client.post("/", data={"url": "https://example.com/" + "x" * 2048})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(query("SELECT COUNT(*) FROM links"), [(0,)])

    def test_redirect_and_click_count(self):
        self.add_link("abc123", "https://example.com/page")
        response = self.client.get("/abc123")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "https://example.com/page")
        self.assertEqual(query("SELECT clicks FROM links WHERE code='abc123'"), [(1,)])

    def test_unknown_code_is_404(self):
        self.assertEqual(self.client.get("/nope99").status_code, 404)

    def test_dashboard_lists_links(self):
        self.add_link("abc123", "https://example.com/page")
        self.login()

        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn("abc123", response.get_data(as_text=True))

    def test_dashboard_requires_authentication(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")

    def test_bulk_requires_authentication(self):
        response = self.client.get("/bulk")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")

    def login(self, password="test-password"):
        return self.client.post(
            "/login",
            data={"password": password},
            follow_redirects=False
        )

    def test_login_rejects_wrong_password(self):
        response = self.login("wrong-password")
        self.assertEqual(response.status_code, 401)

    def test_login_rate_limit_blocks_after_five_failures(self):
        for _ in range(5):
            response = self.login("wrong-password")
            self.assertEqual(response.status_code, 401)

        response = self.login("wrong-password")

        self.assertEqual(response.status_code, 429)

    def test_successful_login_clears_rate_limit(self):
        for _ in range(4):
            response = self.login("wrong-password")
            self.assertEqual(response.status_code, 401)

        response = self.login()
        self.assertEqual(response.status_code, 302)

        # The failure counter was cleared, so another five failures
        # should still be allowed.
        self.client.post("/logout")

        for _ in range(5):
            response = self.login("wrong-password")
            self.assertEqual(response.status_code, 401)

    def test_login_accepts_correct_password(self):
        response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard")

    def test_bulk_requires_csrf_token(self):
        self.login()

        response = self.client.post(
            "/bulk",
            data={"urls": "https://example.com"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(query("SELECT COUNT(*) FROM links"), [(0,)])

    def test_authenticated_bulk_with_csrf_succeeds(self):
        self.login()

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        response = self.client.post(
            "/bulk",
            data={
                "urls": "https://example.com",
                "csrf_token": csrf_token
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            query("SELECT COUNT(*) FROM links"),
            [(1,)]
        )

    def test_logout_removes_authentication(self):
        self.login()

        response = self.client.post("/logout")
        self.assertEqual(response.status_code, 302)

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")

    def test_dashboard_escapes_html_in_urls(self):
        self.add_link("xss001", "<script>alert(1)</script>")
        self.login()

        body = self.client.get("/dashboard").get_data(as_text=True)

        self.assertNotIn("<script>alert(1)</script>", body)

    def test_bulk_creates_numbered_batches(self):
        self.login()

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        first = self.client.post(
            "/bulk",
            data={
                "urls": "https://a.com\n\nhttps://b.com\n",
                "csrf_token": csrf_token
            }
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            query("SELECT COUNT(*) FROM links WHERE batch_id='B001'"),
            [(2,)]
        )

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        self.client.post(
            "/bulk",
            data={
                "urls": "https://c.com",
                "csrf_token": csrf_token
            }
        )

        self.assertEqual(
            query("SELECT COUNT(*) FROM links WHERE batch_id='B002'"),
            [(1,)]
        )

    def test_bulk_rejects_unsafe_url(self):
        self.login()

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        response = self.client.post(
            "/bulk",
            data={
                "urls": "https://good.example\njavascript:alert(1)",
                "csrf_token": csrf_token
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(query("SELECT COUNT(*) FROM links"), [(0,)])

    def test_bulk_rejects_more_than_100_urls(self):
        self.login()

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        urls = "\n".join(
            f"https://example.com/{i}" for i in range(101)
        )

        response = self.client.post(
            "/bulk",
            data={
                "urls": urls,
                "csrf_token": csrf_token
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(query("SELECT COUNT(*) FROM links"), [(0,)])

    def test_csv_export(self):
        self.login()

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        self.client.post(
            "/bulk",
            data={
                "urls": "https://a.com\nhttps://b.com",
                "csrf_token": csrf_token
            }
        )

        response = self.client.get("/bulk/B001/csv")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")

        text = response.get_data(as_text=True)

        self.assertIn("https://a.com", text)
        self.assertIn("https://b.com", text)

    def test_pdf_export(self):
        self.login()

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        self.client.post(
            "/bulk",
            data={
                "urls": "https://a.com",
                "csrf_token": csrf_token
            }
        )

        response = self.client.get("/bulk/B001/pdf")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.get_data().startswith(b"%PDF"))

    def test_pdf_export_escapes_reportlab_markup(self):
        self.login()

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        self.client.post(
            "/bulk",
            data={
                "urls": "https://example.com/?x=%3Cb%3Eevil%3C/b%3E",
                "csrf_token": csrf_token
            }
        )

        connection = sqlite3.connect(os.environ["DATABASE"])
        connection.execute(
            "UPDATE links SET url = ? WHERE batch_id = ?",
            ("https://example.com/<b>evil</b>", "B001")
        )
        connection.commit()
        connection.close()

        response = self.client.get("/bulk/B001/pdf")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.get_data().startswith(b"%PDF"))

    def test_requests_are_logged(self):
        self.client.get("/", headers={"User-Agent": "unit-test"})
        rows = query("SELECT method, path, status_code, user_agent FROM access_logs")
        self.assertEqual(rows, [("GET", "/", 200, "unit-test")])


if __name__ == "__main__":
    unittest.main()
