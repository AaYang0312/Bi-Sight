"""Chat API contract checks that do not require an ERP connection."""

import unittest
import warnings
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch, sentinel
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg

from starlette.exceptions import StarletteDeprecationWarning

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning, module="fastapi.testclient")
from fastapi.testclient import TestClient
from pydantic import SecretStr


class ApiTests(unittest.TestCase):
    def test_runtime_factory_loads_app_and_selected_model(self):
        from bi_agent.api import create_runtime_app

        env = {
            "APP_ENV": "development",
            "APP_PUBLIC_ORIGIN": "http://localhost:5173",
            "BI_APP_DSN": "postgresql://bi_app:password@localhost/bi_agent_test",
            "BI_SHOP_IDS": "S1",
            "LLM_PROVIDER": "qwen",
            "LLM_MODEL": "qwen-test",
            "QWEN_API_KEY": "test-key",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "bi_agent.llm.create_model", return_value=sentinel.model
        ) as create_model:
            app = create_runtime_app()

        self.assertEqual(app.state.settings.environment, "development")
        create_model.assert_called_once()

    def _app(self):
        from bi_agent.api import create_app
        from bi_agent.config import AppSettings

        return create_app(AppSettings(
            app_dsn=SecretStr(os.environ["BI_TEST_ADMIN_DSN"]),
            shop_ids=frozenset({"S1"}),
            environment="production",
            allowed_subjects=frozenset({"user-a", "user-b"}),
            public_origin="https://bi.test",
            auth_subject_header="X-Auth-Request-Sub",
        ))

    @unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
    def test_chat_crud_is_owned_by_the_authenticated_subject(self):
        client = TestClient(self._app())
        write_headers = {
            "Content-Type": "application/json",
            "X-BI-Agent": "web",
            "Origin": "https://bi.test",
            "X-Auth-Request-Sub": "user-a",
        }
        created = client.post("/api/chats", headers=write_headers, json={})
        self.assertEqual(created.status_code, 201, created.text)
        chat_id = created.json()["id"]
        try:
            other = client.get(
                f"/api/chats/{chat_id}/messages",
                headers={"X-Auth-Request-Sub": "user-b"},
            )
            self.assertEqual(other.status_code, 404)
            renamed = client.patch(
                f"/api/chats/{chat_id}", headers=write_headers,
                json={"title": "九月复盘"},
            )
            self.assertEqual(renamed.status_code, 200, renamed.text)
            self.assertEqual(renamed.json()["title"], "九月复盘")
            denied = client.post(
                "/api/chats",
                headers={"X-Auth-Request-Sub": "user-a"}, json={},
            )
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.json()["code"], "forbidden")
        finally:
            client.delete(f"/api/chats/{chat_id}", headers=write_headers)

    @unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
    def test_message_stream_persists_a_completed_turn(self):
        from bi_agent.llm import Message, ModelReply

        class PlainModel:
            def complete(self, messages, tools, *, timeout_s):
                reply = ModelReply(text="已记录你的问题")
                reply._message = Message(role="assistant", content=reply.text)
                return reply

        client = TestClient(create_app := self._app_with_model(PlainModel()))
        headers = {
            "Content-Type": "application/json",
            "X-BI-Agent": "web",
            "Origin": "https://bi.test",
            "X-Auth-Request-Sub": "user-a",
        }
        created = client.post("/api/chats", headers=headers, json={})
        self.assertEqual(created.status_code, 201, created.text)
        chat_id = created.json()["id"]
        try:
            response = client.post(
                f"/api/chats/{chat_id}/messages", headers=headers,
                json={"content": "帮我看最近7天"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("event: status", response.text)
            self.assertIn("event: message", response.text)
            self.assertIn("event: done", response.text)
            messages = client.get(
                f"/api/chats/{chat_id}/messages",
                headers={"X-Auth-Request-Sub": "user-a"},
            ).json()
            self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
            self.assertEqual(messages[-1]["content"], "已记录你的问题")
        finally:
            client.delete(f"/api/chats/{chat_id}", headers=headers)

    @unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
    def test_message_stream_reports_a_model_failure(self):
        from bi_agent.llm import ModelError

        class FailingModel:
            def complete(self, messages, tools, *, timeout_s):
                raise ModelError("timeout")

        client = TestClient(self._app_with_model(FailingModel()))
        headers = {
            "Content-Type": "application/json",
            "X-BI-Agent": "web",
            "Origin": "https://bi.test",
            "X-Auth-Request-Sub": "user-a",
        }
        created = client.post("/api/chats", headers=headers, json={})
        self.assertEqual(created.status_code, 201, created.text)
        chat_id = created.json()["id"]
        try:
            response = client.post(
                f"/api/chats/{chat_id}/messages", headers=headers,
                json={"content": "帮我看最近7天"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("event: error", response.text)
            self.assertIn('"status":"error"', response.text)
            messages = client.get(
                f"/api/chats/{chat_id}/messages",
                headers={"X-Auth-Request-Sub": "user-a"},
            ).json()
            self.assertEqual(messages[-1]["status"], "error")
        finally:
            client.delete(f"/api/chats/{chat_id}", headers=headers)

    def test_runtime_create_run_failure_ends_with_a_sanitized_sse_error(self):
        """A run-store failure must not leak database diagnostics to the browser."""
        from bi_agent.agent import run_chat_turn
        from bi_agent.llm import Message, ModelReply, ToolCall

        class QueryingModel:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, tools, *, timeout_s):
                self.calls += 1
                call = ToolCall(
                    id="call_1", name="query_business", arguments={
                        "start": "2026-09-01", "end": "2026-09-08",
                        "shop_ids": ["shop_1"], "metrics": ["paid_amount"],
                    },
                )
                reply = ModelReply(tool_calls=[call])
                reply._message = Message(role="assistant", content=None, tool_calls=[call])
                return reply

        class FailingRunStore:
            def __init__(self, _conn, *, forbidden_values):
                self.forbidden_values = forbidden_values

            def create_run(self, _record):
                raise RuntimeError(
                    "psycopg.OperationalError dsn=postgresql://secret "
                    "SELECT * FROM bi.query_runs"
                )

        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [("S1", "店铺A")]
        model = QueryingModel()
        with patch("bi_agent.agent.PostgresQueryRunStore", FailingRunStore), patch(
            "bi_agent.chats.load_chat_context", return_value=({}, [])
        ), patch(
            "bi_agent.chats.save_user_message",
            return_value=SimpleNamespace(id=uuid4()),
        ), patch("bi_agent.chats.save_assistant_message"), patch(
            "bi_agent.chats.update_chat_filters"
        ):
            events = list(run_chat_turn(
                conn, uuid4(), "user-a", "最近7天店铺A支付金额", model=model,
                allowed_shop_ids=frozenset({"S1"}),
                now=datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
            ))

        self.assertEqual(model.calls, 1)
        self.assertEqual([event.event for event in events], ["status", "error", "done"])
        self.assertEqual(events[-2].data["code"], "unavailable")
        self.assertEqual(events[-1].data, {"status": "error"})
        browser_text = "\n".join(
            f"{event.event}:{event.data}" for event in events
        )
        self.assertNotIn("psycopg", browser_text)
        self.assertNotIn("SELECT", browser_text)
        self.assertNotIn("dsn=", browser_text)

    @unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
    def test_message_stream_audits_a_completed_business_query_without_sensitive_json(self):
        """A completed business query links its run, events, and public artifact."""
        from bi_agent.llm import Message, ModelReply, ToolCall
        from tests.test_db import seed_business_case

        class BusinessQueryModel:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, tools, *, timeout_s):
                self.calls += 1
                if self.calls == 1:
                    call = ToolCall(
                        id="call_1", name="query_business", arguments={
                            "start": "2026-09-01", "end": "2026-09-08",
                            "shop_ids": ["shop_1"], "metrics": ["paid_amount"],
                        },
                    )
                    reply = ModelReply(tool_calls=[call])
                    reply._message = Message(
                        role="assistant", content=None, tool_calls=[call],
                        provider_context={"reasoning_content": "private reasoning"},
                    )
                    return reply
                reply = ModelReply(text="最近7天店铺A支付金额为1000元")
                reply._message = Message(role="assistant", content=reply.text)
                return reply

        class TestTransactionConnection:
            """Keep API requests inside this test's rollback-only transaction."""

            def __init__(self, connection):
                self._connection = connection

            def __enter__(self):
                return self

            def __exit__(self, *_details):
                return False

            def close(self):
                pass

            def __getattr__(self, name):
                return getattr(self._connection, name)

        admin_conn = psycopg.connect(os.environ["BI_TEST_ADMIN_DSN"])
        try:
            if not admin_conn.info.dbname.endswith("_test"):
                self.fail(f"测试必须连接 *_test 数据库，实际 {admin_conn.info.dbname}")
            if (admin_conn.info.host or "") not in {"localhost", "127.0.0.1", "::1"}:
                self.fail(f"测试必须连接本地测试实例，实际 {admin_conn.info.host}")
            seed_business_case(admin_conn)
            connection = TestTransactionConnection(admin_conn)
            client = TestClient(self._app_with_model(BusinessQueryModel()))
            headers = {
                "Content-Type": "application/json",
                "X-BI-Agent": "web",
                "Origin": "https://bi.test",
                "X-Auth-Request-Sub": "user-a",
            }
            with patch("bi_agent.api.psycopg.connect", return_value=connection):
                created = client.post("/api/chats", headers=headers, json={})
                self.assertEqual(created.status_code, 201, created.text)
                chat_id = created.json()["id"]
                try:
                    response = client.post(
                        f"/api/chats/{chat_id}/messages", headers=headers,
                        json={"content": "最近7天店铺A支付金额"},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("event: artifact", response.text)
                    self.assertIn("event: message", response.text)
                    self.assertLess(
                        response.text.index("event: artifact"),
                        response.text.index("event: message"),
                    )
                    self.assertLess(
                        response.text.index("event: message"),
                        response.text.rindex("event: done"),
                    )
                    self.assertTrue(response.text.rstrip().endswith(
                        'event: done\ndata: {"status":"complete"}'
                    ))
                    run = admin_conn.execute(
                        "SELECT id, status, current_node, revision FROM bi.query_runs "
                        "WHERE chat_id=%s ORDER BY started_at DESC LIMIT 1", (chat_id,),
                    ).fetchone()
                    self.assertIsNotNone(run)
                    self.assertEqual(run[1:3], ("succeeded", "finalize"))
                    self.assertGreater(run[3], 0)
                    self.assertGreater(admin_conn.execute(
                        "SELECT count(*) FROM bi.query_run_events WHERE run_id=%s", (run[0],)
                    ).fetchone()[0], 0)
                    self.assertEqual(admin_conn.execute(
                        "SELECT count(*) FROM bi.query_artifacts WHERE run_id=%s", (run[0],)
                    ).fetchone()[0], 1)
                    persisted_json = admin_conn.execute(
                        "SELECT concat(r.normalized_request::text, r.state::text, "
                        "coalesce((SELECT string_agg(e.payload::text, '') "
                        "FROM bi.query_run_events e WHERE e.run_id=r.id), ''), "
                        "coalesce((SELECT string_agg(a.payload::text, '') "
                        "FROM bi.query_artifacts a WHERE a.run_id=r.id), '')) "
                        "FROM bi.query_runs r WHERE r.id=%s", (run[0],),
                    ).fetchone()[0]
                    for forbidden in ("S1", "ERP-P-9", "reasoning_content"):
                        self.assertNotIn(forbidden, persisted_json)
                finally:
                    client.delete(f"/api/chats/{chat_id}", headers=headers)
        finally:
            admin_conn.rollback()
            admin_conn.close()

    def _app_with_model(self, model):
        from bi_agent.api import create_app
        from bi_agent.config import AppSettings

        return create_app(AppSettings(
            app_dsn=SecretStr(os.environ["BI_TEST_ADMIN_DSN"]),
            shop_ids=frozenset({"S1"}),
            environment="production",
            allowed_subjects=frozenset({"user-a", "user-b"}),
            public_origin="https://bi.test",
            auth_subject_header="X-Auth-Request-Sub",
        ), model=model)

    def test_health_and_chat_only_routes(self):
        from bi_agent.api import create_app
        from bi_agent.config import AppSettings

        app = create_app(AppSettings(
            app_dsn=SecretStr("postgresql://bi_app:password@localhost/bi_agent_test"),
            shop_ids=frozenset({"S1"}),
            environment="development",
            allowed_subjects=frozenset(),
            public_origin="http://localhost:5173",
            auth_subject_header="X-Auth-Request-Sub",
        ))
        client = TestClient(app)

        self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
        paths = set(app.openapi()["paths"])
        self.assertIn("/api/chats", paths)
        self.assertNotIn("/api/query", paths)
        self.assertNotIn("/api/metrics", paths)
        self.assertNotIn("/api/sql", paths)
