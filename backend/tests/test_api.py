"""Chat API contract checks that do not require an ERP connection."""

import unittest
import warnings
import os
from unittest.mock import patch, sentinel

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
