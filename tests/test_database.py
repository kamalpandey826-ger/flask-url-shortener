"""Step 2 tests: database setup, upgrading old databases, and the access log."""
import os
import sqlite3
import tempfile
import unittest

# DATABASE is read when app.py is imported. If test_app.py already imported it,
# that one wins; every test below switches to its own temp file anyway.
_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault('DATABASE', os.path.join(_TMP.name, 'import.db'))

import app as shortener  # noqa: E402


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        original = shortener.DATABASE
        self.addCleanup(setattr, shortener, 'DATABASE', original)
        shortener.DATABASE = os.path.join(folder.name, 'test.db')
        shortener.app.config['TESTING'] = True
        self.client = shortener.app.test_client()

    def run_sql(self, sql, args=()):
        connection = sqlite3.connect(shortener.DATABASE)
        try:
            rows = connection.execute(sql, args).fetchall()
            connection.commit()
            return rows
        finally:
            connection.close()

    def columns(self, table):
        return [row[1] for row in self.run_sql(f'PRAGMA table_info({table})')]

    def test_fresh_database_has_full_schema(self):
        shortener.create_database()
        self.assertIn('batch_id', self.columns('links'))
        self.assertIn('ip_address', self.columns('access_logs'))

    def test_create_database_is_safe_to_repeat(self):
        shortener.create_database()
        shortener.create_database()
        self.assertEqual(self.run_sql('SELECT COUNT(*) FROM links'), [(0,)])

    def test_old_database_is_upgraded_and_keeps_its_data(self):
        self.run_sql(
            'CREATE TABLE links (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'code TEXT UNIQUE NOT NULL, url TEXT NOT NULL, '
            'clicks INTEGER DEFAULT 0, created_at TEXT)'
        )
        self.run_sql("INSERT INTO links (code, url) VALUES ('old001', 'https://old.example')")
        shortener.create_database()
        self.assertIn('batch_id', self.columns('links'))
        self.assertIn('ip_address', self.columns('access_logs'))
        self.assertEqual(self.run_sql('SELECT code, url FROM links'),
                         [('old001', 'https://old.example')])

    def test_long_user_agent_is_cut(self):
        shortener.create_database()
        self.client.get('/', headers={'User-Agent': 'x' * 1000})
        self.assertEqual(self.run_sql('SELECT length(user_agent) FROM access_logs'), [(200,)])

    def test_old_log_rows_are_deleted(self):
        shortener.create_database()
        # The next log row gets id 500, which triggers the clean-up.
        self.run_sql(
            'INSERT INTO access_logs (id, created_at) VALUES (499, '"'"'2000-01-01 00:00:00'"'"')'
        )
        self.client.get('/')
        self.assertEqual(self.run_sql('SELECT COUNT(*) FROM access_logs WHERE id = 499'), [(0,)])
        self.assertEqual(self.run_sql('SELECT COUNT(*) FROM access_logs WHERE id = 500'), [(1,)])

    def test_log_failure_does_not_break_the_page(self):
        # A database with no access_logs table at all.
        self.run_sql('CREATE TABLE links (id INTEGER PRIMARY KEY, code TEXT, url TEXT)')
        with self.assertLogs(shortener.app.logger, level='ERROR'):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
