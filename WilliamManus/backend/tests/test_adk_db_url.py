import unittest
import sys
from pathlib import Path

# Allow running tests from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.db_url import to_sqlalchemy_async_url, sanitize_db_url


class TestDbUrlHelpers(unittest.TestCase):
    def test_postgresql_raw_to_async(self):
        raw = "postgresql://user:pass@localhost:5432/mydb"
        expected = "postgresql+asyncpg://user:pass@localhost:5432/mydb"
        self.assertEqual(to_sqlalchemy_async_url(raw), expected)

    def test_postgres_raw_to_async(self):
        raw = "postgres://user:pass@localhost/mydb"
        expected = "postgresql+asyncpg://user:pass@localhost/mydb"
        self.assertEqual(to_sqlalchemy_async_url(raw), expected)

    def test_already_async_is_unchanged(self):
        raw = "postgresql+asyncpg://user:pass@localhost:5432/mydb"
        self.assertEqual(to_sqlalchemy_async_url(raw), raw)

    def test_sqlite_raw_to_async(self):
        raw = "sqlite:///tmp/test.db"
        expected = "sqlite+aiosqlite:///tmp/test.db"
        self.assertEqual(to_sqlalchemy_async_url(raw), expected)

    def test_unknown_scheme_passthrough(self):
        raw = "mysql://user:pass@localhost/db"
        self.assertEqual(to_sqlalchemy_async_url(raw), raw)

    def test_sanitize_hides_password(self):
        raw = "postgresql://user:secret@localhost:5432/mydb"
        safe = sanitize_db_url(raw)
        self.assertNotIn("secret", safe)
        self.assertIn("********", safe)


if __name__ == "__main__":
    unittest.main()
