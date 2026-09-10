"""Database checks for the query-run persistence migration.

Each test owns an administrator transaction and rolls it back so the
independent local ``*_test`` database remains unchanged.  The same guardrails
as ``test_db.py`` prevent this module from connecting to a production host.
"""

import os
import unittest
from pathlib import Path
from uuid import uuid4

import psycopg


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
MIGRATION = Path(__file__).parents[1] / "sql" / "004_query_runtime.sql"


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class RuntimeDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["BI_TEST_ADMIN_DSN"])
        if not self.conn.info.dbname.endswith("_test"):
            self.fail(f"测试必须连接 *_test 数据库，实际 {self.conn.info.dbname}")
        if (self.conn.info.host or "") not in LOCAL_HOSTS:
            self.fail(f"测试必须连接本地测试实例，实际 {self.conn.info.host}")

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _seed_user_message(self, subject: str = "u1"):
        chat_id = uuid4()
        message_id = uuid4()
        self.conn.execute(
            "INSERT INTO bi.app_chats(id, subject_id, title) VALUES (%s, %s, '查询')",
            (chat_id, subject),
        )
        self.conn.execute(
            "INSERT INTO bi.app_messages(id, chat_id, role, content, status) "
            "VALUES (%s, %s, 'user', '查询销售额', 'complete')",
            (message_id, chat_id),
        )
        return chat_id, message_id

    def test_runtime_tables_have_constraints_and_cascade_from_chat(self):
        """A run and every child record disappear when its chat is deleted."""
        chat_id, message_id = self._seed_user_message()
        run_id = uuid4()
        artifact_id = uuid4()
        self.conn.execute(
            "INSERT INTO bi.query_runs "
            "(id, chat_id, user_message_id, subject_id, tool_call_id, attempt_no) "
            "VALUES (%s, %s, %s, 'u1', 'call_1', 1)",
            (run_id, chat_id, message_id),
        )
        self.conn.execute(
            "INSERT INTO bi.query_run_events "
            "(run_id, revision, node, event_type, status) "
            "VALUES (%s, 1, 'received', 'entered', 'running')", (run_id,),
        )
        self.conn.execute(
            "INSERT INTO bi.query_artifacts (id, run_id, artifact_type, payload) "
            "VALUES (%s, %s, 'metric_result', '{\"status\":\"ok\"}')",
            (artifact_id, run_id),
        )

        for statement, parameters, error in (
            (
                "INSERT INTO bi.query_runs "
                "(id, chat_id, user_message_id, subject_id, tool_call_id, attempt_no) "
                "VALUES (%s, %s, %s, 'u1', 'call_2', 0)",
                (uuid4(), chat_id, message_id),
                psycopg.errors.CheckViolation,
            ),
            (
                "INSERT INTO bi.query_run_events "
                "(run_id, revision, node, event_type, status) "
                "VALUES (%s, 2, 'received', 'unknown', 'running')",
                (run_id,),
                psycopg.errors.CheckViolation,
            ),
            (
                "INSERT INTO bi.query_artifacts (id, run_id, artifact_type, payload) "
                "VALUES (%s, %s, 'unknown', '{}')",
                (uuid4(), run_id),
                psycopg.errors.CheckViolation,
            ),
        ):
            with self.subTest(statement=statement), self.assertRaises(error):
                with self.conn.transaction():
                    self.conn.execute(statement, parameters)

        self.conn.execute("DELETE FROM bi.app_chats WHERE id=%s", (chat_id,))

        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_runs WHERE id=%s", (run_id,)
        ).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_run_events WHERE run_id=%s", (run_id,)
        ).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_artifacts WHERE id=%s", (artifact_id,)
        ).fetchone()[0], 0)

    def test_app_role_can_manage_runtime_but_not_business_facts(self):
        """The API role writes its runtime rows but never fact-table rows."""
        chat_id, message_id = self._seed_user_message()
        run_id = uuid4()
        self.conn.execute("SET LOCAL ROLE bi_app")
        self.conn.execute(
            "INSERT INTO bi.query_runs "
            "(id, chat_id, user_message_id, subject_id, tool_call_id, attempt_no) "
            "VALUES (%s, %s, %s, 'u1', 'call_1', 1)",
            (run_id, chat_id, message_id),
        )
        self.conn.execute(
            "INSERT INTO bi.query_run_events "
            "(run_id, revision, node, event_type, status) "
            "VALUES (%s, 1, 'received', 'entered', 'running')", (run_id,),
        )
        self.conn.execute(
            "INSERT INTO bi.query_artifacts (id, run_id, artifact_type, payload) "
            "VALUES (%s, %s, 'metric_result', '{\"status\":\"ok\"}')",
            (uuid4(), run_id),
        )
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.conn.transaction():
                self.conn.execute("INSERT INTO bi.shops(shop_id) VALUES ('forbidden')")

    def test_app_role_cannot_mutate_runtime_events_or_artifacts(self):
        """Event and artifact history is append-only for the API role."""
        chat_id, message_id = self._seed_user_message()
        run_id = uuid4()
        artifact_id = uuid4()
        self.conn.execute(
            "INSERT INTO bi.query_runs "
            "(id, chat_id, user_message_id, subject_id, tool_call_id, attempt_no) "
            "VALUES (%s, %s, %s, 'u1', 'call_1', 1)",
            (run_id, chat_id, message_id),
        )
        self.conn.execute(
            "INSERT INTO bi.query_run_events "
            "(run_id, revision, node, event_type, status) "
            "VALUES (%s, 1, 'received', 'entered', 'running')", (run_id,),
        )
        self.conn.execute(
            "INSERT INTO bi.query_artifacts (id, run_id, artifact_type, payload) "
            "VALUES (%s, %s, 'metric_result', '{\"status\":\"ok\"}')",
            (artifact_id, run_id),
        )

        self.conn.execute("SET LOCAL ROLE bi_app")
        for statement, parameters in (
            ("UPDATE bi.query_run_events SET node='finalize' WHERE run_id=%s", (run_id,)),
            ("DELETE FROM bi.query_run_events WHERE run_id=%s", (run_id,)),
            ("UPDATE bi.query_artifacts SET payload='{}' WHERE id=%s", (artifact_id,)),
            ("DELETE FROM bi.query_artifacts WHERE id=%s", (artifact_id,)),
        ):
            with self.subTest(statement=statement), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.conn.transaction():
                    self.conn.execute(statement, parameters)

    def test_migration_is_idempotent_in_an_administrator_transaction(self):
        """Reapplying the migration does not create duplicate database objects."""
        migration = MIGRATION.read_text(encoding="utf-8")
        self.conn.execute(migration)
        self.conn.execute(migration)
        tables = self.conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='bi' AND table_name IN "
            "('query_runs', 'query_run_events', 'query_artifacts') ORDER BY table_name"
        ).fetchall()
        self.assertEqual([row[0] for row in tables], [
            "query_artifacts", "query_run_events", "query_runs",
        ])
        indexes = self.conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='bi' "
            "AND indexname IN ('query_runs_chat_started_idx', "
            "'query_runs_message_attempt_idx', 'query_artifacts_run_idx') "
            "ORDER BY indexname"
        ).fetchall()
        self.assertEqual([row[0] for row in indexes], [
            "query_artifacts_run_idx", "query_runs_chat_started_idx",
            "query_runs_message_attempt_idx",
        ])
