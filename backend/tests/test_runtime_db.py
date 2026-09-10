"""Database checks for the query-run persistence migration.

Each test owns an administrator transaction and rolls it back so the
independent local ``*_test`` database remains unchanged.  The same guardrails
as ``test_db.py`` prevent this module from connecting to a production host.
"""

import os
import traceback
import unittest
from pathlib import Path
from uuid import uuid4

import psycopg

from bi_agent.runtime import PostgresQueryRunStore
from bi_agent.runtime.models import (
    ArtifactPersistenceError,
    NewArtifact,
    NewQueryRun,
    RunCompletion,
    RunContextNotFound,
    RunEventType,
    RunNotFound,
    RunStatus,
    RunTransition,
    StaleRunRevision,
)


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
MIGRATION = Path(__file__).parents[1] / "sql" / "004_query_runtime.sql"


class RuntimeStoreValidationTests(unittest.TestCase):
    """Ingestion checks that run without a configured PostgreSQL instance."""

    def setUp(self):
        self.store = PostgresQueryRunStore(None, forbidden_values={"S1", "ERP-P-9"})

    def test_create_run_revalidates_constructed_record_before_database_access(self):
        record = NewQueryRun.model_construct(
            chat_id=uuid4(),
            user_message_id=uuid4(),
            subject_id="u1",
            tool_call_id="call_1",
            domain="business_query",
            attempt_no=1,
            normalized_request={},
            state={"message": "请查询店铺 S1 的销售额"},
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            self.store.create_run(record)

    def test_transition_revalidates_constructed_command_before_database_access(self):
        transition = RunTransition.model_construct(
            expected_revision=0,
            node="resolve_parameters",
            event_type=RunEventType.TRANSITIONED,
            status=RunStatus.RUNNING,
            state={"node": "resolve_parameters", "revision": 1},
            payload={"reasoning_content": "opaque"},
            error_code=None,
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            self.store.transition(uuid4(), transition)

    def test_transition_revalidates_constructed_normalized_request_before_database_access(self):
        transition = RunTransition.model_construct(
            expected_revision=0,
            node="resolve_parameters",
            event_type=RunEventType.TRANSITIONED,
            status=RunStatus.RUNNING,
            state={"node": "resolve_parameters", "revision": 1},
            normalized_request={"shop_aliases": ["S1"]},
            payload={},
            error_code=None,
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            self.store.transition(uuid4(), transition)

    def test_save_artifact_revalidates_constructed_command_before_database_access(self):
        artifact = NewArtifact.model_construct(
            artifact_type="metric_result",
            payload={"status": "ok", "data": [{"product_id": "ERP-P-9"}]},
            data_as_of=None,
            coverage=None,
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            self.store.save_artifact(uuid4(), artifact)

    def test_finish_revalidates_constructed_running_status_before_database_access(self):
        completion = RunCompletion.model_construct(
            expected_revision=0,
            node="finalize",
            status="running",
            state={"node": "finalize", "status": "running", "revision": 1},
            payload={},
            error_code=None,
        )

        with self.assertRaisesRegex(ValueError, "^finish_requires_terminal_status$"):
            self.store.finish(uuid4(), completion)

    def test_store_requires_forbidden_values(self):
        with self.assertRaisesRegex(ValueError, "^forbidden_values_required$"):
            PostgresQueryRunStore(None, forbidden_values=set())

    def test_artifact_foreign_key_failure_is_a_safe_missing_run_error(self):
        marker = "repository-secret-marker"

        class MissingRunConnection:
            def execute(self, _statement, _parameters):
                raise psycopg.errors.ForeignKeyViolation(marker)

        store = PostgresQueryRunStore(MissingRunConnection(), forbidden_values={"S1"})

        with self.assertRaises(RunNotFound) as context:
            store.save_artifact(uuid4(), NewArtifact(payload={"status": "ok"}))

        self.assertEqual(str(context.exception), "run_not_found")
        self.assertIsNone(context.exception.__cause__)
        self.assertTrue(context.exception.__suppress_context__)
        self.assertNotIn(marker, "".join(traceback.format_exception(context.exception)))

    def test_artifact_database_failure_is_sanitized(self):
        marker = "repository-secret-marker"

        class FailingArtifactConnection:
            def execute(self, _statement, _parameters):
                raise psycopg.errors.SyntaxError(marker)

        store = PostgresQueryRunStore(FailingArtifactConnection(), forbidden_values={"S1"})

        with self.assertRaises(ArtifactPersistenceError) as context:
            store.save_artifact(uuid4(), NewArtifact(payload={"status": "ok"}))

        self.assertEqual(str(context.exception), "artifact_persistence_error")
        self.assertIsNone(context.exception.__cause__)
        self.assertTrue(context.exception.__suppress_context__)
        self.assertNotIn(marker, "".join(traceback.format_exception(context.exception)))


class RuntimeDatabaseFixture:
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


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class RuntimeDatabaseTests(RuntimeDatabaseFixture, unittest.TestCase):
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


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class RuntimeStoreDatabaseTests(RuntimeDatabaseFixture, unittest.TestCase):
    def test_store_creates_run_only_for_matching_user_message(self):
        chat_id, message_id = self._seed_user_message(subject="u1")
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u1",
            tool_call_id="call_1", attempt_no=1,
            normalized_request={"shop_aliases": ["shop_1"]},
            state={"node": "received"},
        ))
        row = self.conn.execute(
            "SELECT subject_id, revision, status FROM bi.query_runs WHERE id=%s",
            (run_id,),
        ).fetchone()
        self.assertEqual(row, ("u1", 0, "running"))
        with self.assertRaises(RunContextNotFound):
            store.create_run(NewQueryRun(
                chat_id=chat_id, user_message_id=message_id, subject_id="u2",
                tool_call_id="call_2", attempt_no=2,
            ))

    def test_store_transitions_once_and_rejects_stale_revision(self):
        chat_id, message_id = self._seed_user_message()
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u1",
            tool_call_id="call_1", attempt_no=1, state={"node": "received"},
        ))
        normalized_request = {"shop_aliases": ["shop_1"]}
        transition = RunTransition(
            expected_revision=0,
            node="resolve_parameters",
            status=RunStatus.RUNNING,
            normalized_request=normalized_request,
            state={
                "node": "resolve_parameters",
                "revision": 1,
                "normalized_request": normalized_request,
            },
        )

        store.transition(run_id, transition)

        self.assertEqual(self.conn.execute(
            "SELECT revision FROM bi.query_runs WHERE id=%s", (run_id,)
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT revision FROM bi.query_run_events WHERE run_id=%s", (run_id,)
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT normalized_request FROM bi.query_runs WHERE id=%s", (run_id,)
        ).fetchone()[0], normalized_request)
        with self.assertRaises(StaleRunRevision):
            store.transition(run_id, transition)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_run_events WHERE run_id=%s", (run_id,)
        ).fetchone()[0], 1)

    def test_store_finishes_with_terminal_event(self):
        chat_id, message_id = self._seed_user_message()
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u1",
            tool_call_id="call_1", attempt_no=1, state={"node": "received"},
        ))

        store.finish(run_id, RunCompletion(
            expected_revision=0,
            node="finalize",
            status=RunStatus.SUCCEEDED,
            state={"node": "finalize", "status": "succeeded", "revision": 1},
            payload={"result_count": 1},
        ))

        self.assertEqual(self.conn.execute(
            "SELECT revision, status, completed_at IS NOT NULL FROM bi.query_runs WHERE id=%s",
            (run_id,),
        ).fetchone(), (1, "succeeded", True))
        self.assertEqual(self.conn.execute(
            "SELECT revision, event_type FROM bi.query_run_events WHERE run_id=%s",
            (run_id,),
        ).fetchone(), (1, "completed"))

    def test_store_persists_only_public_artifact_projection(self):
        chat_id, message_id = self._seed_user_message()
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u1",
            tool_call_id="call_1", attempt_no=1, state={"node": "received"},
        ))
        payload = {
            "status": "ok",
            "data": [{"shop_id": "店铺1", "product_id": "商品A"}],
        }

        artifact = store.save_artifact(run_id, NewArtifact(payload=payload))

        self.assertEqual(artifact.type, "metric_result")
        self.assertEqual(self.conn.execute(
            "SELECT payload FROM bi.query_artifacts WHERE id=%s", (artifact.id,)
        ).fetchone()[0], payload)

    def test_store_revalidates_constructed_commands_before_writing(self):
        chat_id, message_id = self._seed_user_message()
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        unsafe_record = NewQueryRun.model_construct(
            chat_id=chat_id,
            user_message_id=message_id,
            subject_id="u1",
            tool_call_id="call_1",
            domain="business_query",
            attempt_no=1,
            normalized_request={},
            state={"message": "请查询店铺 S1 的销售额"},
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            store.create_run(unsafe_record)

        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_runs WHERE user_message_id=%s", (message_id,)
        ).fetchone()[0], 0)
