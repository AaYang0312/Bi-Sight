"""核心离线检查：配置、快麦接口边界、指标输入、模型回合、预算假设与对话。"""

import hashlib
import hmac
import json
import logging
import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import httpx


class ConfigTests(unittest.TestCase):
    def test_selected_provider_uses_its_own_key(self):
        from bi_agent.config import load_model_settings

        env = {
            "LLM_PROVIDER": "deepseek",
            "LLM_MODEL": "demo-model",
            "DEEPSEEK_API_KEY": "fake-deepseek-key",
            "QWEN_API_KEY": "fake-qwen-key",
        }
        settings = load_model_settings(env)
        self.assertEqual(settings.api_key.get_secret_value(), "fake-deepseek-key")
        with self.assertRaises(ValueError):
            load_model_settings({**env, "LLM_PROVIDER": "unknown"})
        self.assertNotIn("fake-deepseek-key", repr(settings))

    def test_missing_model_id_rejected(self):
        from bi_agent.config import load_model_settings

        env = {"LLM_PROVIDER": "qwen", "LLM_MODEL": "", "QWEN_API_KEY": "k"}
        with self.assertRaises(ValueError):
            load_model_settings(env)

    def test_base_url_must_be_https(self):
        from bi_agent.config import load_model_settings

        env = {
            "LLM_PROVIDER": "qwen",
            "LLM_MODEL": "m",
            "QWEN_API_KEY": "k",
            "LLM_BASE_URL": "http://insecure.example.com/v1",
        }
        with self.assertRaises(ValueError):
            load_model_settings(env)


class KuaimaiTests(unittest.TestCase):
    def test_sign_and_empty_are_explicit(self):
        from bi_agent.kuaimai import KuaimaiError, parse_page, sign

        expected = hmac.new(b"test-secret", b"a1b2", hashlib.sha256).hexdigest().upper()
        self.assertEqual(sign({"b": "2", "a": "1", "sign": "old"}, "test-secret"), expected)
        self.assertTrue(parse_page({"success": True, "total": 0}).verified_empty)
        for body in ({"success": True}, {"success": True, "total": 2},
                     {"success": False, "code": "25"}):
            with self.assertRaises(KuaimaiError):
                parse_page(body)

    def _client(self, responses):
        from bi_agent.kuaimai import KuaimaiClient
        from bi_agent.config import SyncSettings
        from pydantic import SecretStr

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            response = responses[min(len(calls) - 1, len(responses) - 1)]
            if callable(response):
                return response(request)
            return response

        transport = httpx.MockTransport(handler)
        settings = SyncSettings(
            writer_dsn=SecretStr("postgresql://localhost/test"),
            shop_ids=frozenset({"S1"}),
            app_key=SecretStr("fake-app-key"),
            app_secret=SecretStr("fake-app-secret"),
            access_token=SecretStr("fake-session"),
            refresh_token=SecretStr("fake-refresh"),
        )
        return KuaimaiClient(settings, httpx.Client(transport=transport)), calls

    def test_call_signs_sent_params(self):
        from bi_agent.kuaimai import ROUTER_URL, sign

        def response(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"success": True, "total": 0})

        client, calls = self._client([response])
        payload = client.call("erp.trade.list.query", {"userIds": "S1"})
        self.assertEqual(payload["total"], 0)
        request = calls[0]
        self.assertEqual(str(request.url), ROUTER_URL)
        sent = dict(httpx.QueryParams(request.content.decode()))
        self.assertEqual(sent["appKey"], "fake-app-key")
        self.assertEqual(sent["session"], "fake-session")
        self.assertEqual(sent["method"], "erp.trade.list.query")
        self.assertEqual(sent["version"], "1.0")
        self.assertEqual(sent["sign_method"], "hmac-sha256")
        expected = sign(sent, "fake-app-secret")
        self.assertEqual(sent["sign"], expected)

    def test_retry_on_429_and_5xx(self):
        from bi_agent.kuaimai import KuaimaiClient

        responses = [httpx.Response(429), httpx.Response(503),
                     httpx.Response(200, json={"success": True, "total": 0})]
        client, calls = self._client(responses)
        with patch("bi_agent.kuaimai._sleep") as sleep:
            payload = client.call("m", {})
        self.assertEqual(payload["total"], 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_auth_error_no_retry(self):
        from bi_agent.kuaimai import KuaimaiError

        client, calls = self._client([
            httpx.Response(200, json={"success": False, "code": "7",
                                      "message": "session失效"})])
        with self.assertRaises(KuaimaiError) as ctx:
            client.call("m", {})
        self.assertEqual(ctx.exception.code, "authentication")
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_three_attempts(self):
        from bi_agent.kuaimai import KuaimaiError

        client, calls = self._client([httpx.Response(500)])
        with patch("bi_agent.kuaimai._sleep"):
            with self.assertRaises(KuaimaiError) as ctx:
                client.call("m", {})
        self.assertEqual(ctx.exception.code, "upstream")
        self.assertEqual(len(calls), 3)

    def test_timeout_and_network_map(self):
        from bi_agent.kuaimai import KuaimaiError

        client, calls = self._client([httpx.Response(401)])
        with self.assertRaises(KuaimaiError) as ctx:
            client.call("m", {})
        self.assertEqual(ctx.exception.code, "authentication")

        client, calls = self._client([
            httpx.Response(200, content=b"not-json")])
        with self.assertRaises(KuaimaiError) as ctx:
            client.call("m", {})
        self.assertEqual(ctx.exception.code, "invalid_response")

    def test_refresh_session_keeps_tokens(self):
        client, calls = self._client([
            httpx.Response(200, json={"success": True,
                                      "expireTime": "2026-10-07 14:00:00"})])
        expires = client.refresh_session(now=datetime(2026, 9, 7, 12, 0,
                                                     tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertEqual(expires.year, 2026)
        self.assertEqual(expires.month, 10)

    def test_refresh_session_rejects_changed_token(self):
        from bi_agent.kuaimai import KuaimaiError

        client, calls = self._client([
            httpx.Response(200, json={"success": True, "accessToken": "other-token",
                                      "expireTime": "2026-10-07 14:00:00"})])
        with self.assertRaises(KuaimaiError):
            client.refresh_session()

    def test_no_secrets_in_logs(self):
        client, calls = self._client([
            httpx.Response(500)])
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("bi_agent.kuaimai")
        logger.addHandler(handler)
        try:
            with patch("bi_agent.kuaimai._sleep"):
                with self.assertRaises(Exception):
                    client.call("m", {})
        finally:
            logger.removeHandler(handler)
        text = "".join(str(r.getMessage()) for r in records)
        self.assertNotIn("fake-app-secret", text)
        self.assertNotIn("fake-session", text)


class KuaimaiPageTests(unittest.TestCase):
    def test_row_type_check(self):
        from bi_agent.kuaimai import KuaimaiError, parse_page

        with self.assertRaises(KuaimaiError):
            parse_page({"success": True, "total": 1, "list": ["not-a-dict"]})
        page = parse_page({"success": True, "total": 1, "list": [{"sid": "E1"}],
                           "hasNext": False})
        self.assertEqual(page.rows[0]["sid"], "E1")
        self.assertFalse(page.verified_empty)
        self.assertIs(page.has_next, False)


class MetricInputTests(unittest.TestCase):
    def test_date_defaults_and_bounds(self):
        from bi_agent.metrics import QueryRequest, resolve_period

        now = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_period("最近7天", now=now),
                         (date(2026, 9, 1), date(2026, 9, 8)))
        with self.assertRaises(ValueError):
            QueryRequest(start="2025-01-01", end="2026-09-08", shop_ids=["S1"],
                         metrics=["paid_amount"])

    def test_same_bounds_rejected(self):
        from bi_agent.metrics import QueryRequest

        with self.assertRaises(ValueError):
            QueryRequest(start="2026-09-01", end="2026-09-01", shop_ids=["S1"],
                         metrics=["paid_amount"])

    def test_unknown_metric_rejected(self):
        from bi_agent.metrics import QueryRequest

        with self.assertRaises(ValueError):
            QueryRequest(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                         metrics=["widget_refund_rate"])

    def test_non_cny_rejected(self):
        from bi_agent.metrics import QueryRequest

        with self.assertRaises(ValueError):
            QueryRequest(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                         metrics=["paid_amount"], currency="USD")

    def test_product_group_rejects_non_product_metrics(self):
        from bi_agent.metrics import QueryRequest

        with self.assertRaises(ValueError):
            QueryRequest(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                         metrics=["paid_amount"], group_by="product")
        request = QueryRequest(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                               metrics=["quantity"], group_by="product")
        self.assertEqual(request.group_by, "product")

    def test_spoken_dates(self):
        from bi_agent.metrics import resolve_period

        now = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_period("9月1日至7日支付金额", now=now),
                         (date(2026, 9, 1), date(2026, 9, 8)))
        self.assertEqual(resolve_period("2026-09-01的支付额", now=now),
                         (date(2026, 9, 1), date(2026, 9, 2)))
        self.assertEqual(resolve_period("那上个月呢", now=now),
                         (date(2026, 8, 1), date(2026, 9, 1)))
        self.assertEqual(resolve_period("今天的支付额", now=now),
                         (date(2026, 9, 8), date(2026, 9, 9)))
        self.assertIsNone(resolve_period("照上次那样", now=now))


class PresentationTests(unittest.TestCase):
    def test_csv_escape_and_amounts(self):
        from app import _csv_bytes

        rows = [{"shop_id": "S1", "label": "=SUM(A1)", "cmd": "@cmd",
                 "plus": "+1", "note": "-note", "tab": "\tvalue",
                 "paid_amount": "1000.00", "negative_amount": "-50"}]
        text = _csv_bytes(rows).decode("utf-8-sig")
        self.assertIn("'=SUM(A1)", text)
        self.assertIn("'@cmd", text)
        self.assertIn("+1", text)  # 数值形式按数字输出，无注入风险
        self.assertNotIn("'+1", text)
        self.assertIn("'-note", text)
        self.assertIn("'\tvalue", text)
        self.assertIn("1000.00", text)
        self.assertIn("-50", text)
        self.assertNotIn("'-50", text)

    def test_money_format_and_missing(self):
        from app import _money

        self.assertEqual(_money("166.6666"), "166.67")
        self.assertEqual(_money(1000), "1000.00")
        self.assertEqual(_money(None), "不可计算")

    def test_exclusive_end_conversion(self):
        from app import _exclusive_end

        self.assertEqual(_exclusive_end(date(2026, 9, 7)), date(2026, 9, 8))


def _model_settings(provider: str):
    from bi_agent.config import load_model_settings

    key = f"{provider.upper()}_API_KEY"
    return load_model_settings({
        "LLM_PROVIDER": provider, "LLM_MODEL": "demo-model",
        key: f"fake-{provider}-key",
    })


class ModelTests(unittest.TestCase):
    PROVIDER_RESPONSE = {
        "choices": [{"message": {
            "role": "assistant", "content": None,
            "reasoning_content": "synthetic-private-context",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "query_business", "arguments": '{"start":"2026-09-01"}'}}]
        }}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    def _model(self, provider: str, handler):
        from bi_agent.llm import CompatibleChatModel

        return CompatibleChatModel(_model_settings(provider),
                                   transport=httpx.MockTransport(handler))

    def test_provider_round_trip_for_both(self):
        from bi_agent.llm import Message

        def provider_response(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self.PROVIDER_RESPONSE)

        for provider in ("qwen", "deepseek"):
            with self.subTest(provider=provider):
                model = self._model(provider, provider_response)
                reply = model.complete([Message(role="user", content="查看经营")],
                                       [], timeout_s=2)
                self.assertEqual(reply.tool_calls[0].id, "call_1")
                self.assertEqual(reply.tool_calls[0].arguments, {"start": "2026-09-01"})
                self.assertNotIn("synthetic-private-context", repr(reply.as_message()))
                self.assertNotIn("synthetic-private-context", repr(reply))

    def test_second_request_preserves_reasoning_and_tool_id(self):
        from bi_agent.llm import Message, ModelReply, ToolCall

        requests: list[dict] = []

        def provider_response(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(200, json=self.PROVIDER_RESPONSE)
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant",
                                          "content": "已查询"}}],
            })

        model = self._model("deepseek", provider_response)
        first = model.complete([Message(role="user", content="查看经营")],
                               [], timeout_s=2)
        tool_result = Message(role="tool", tool_call_id="call_1", content="{}")
        second = model.complete(
            [Message(role="user", content="查看经营"), first.as_message(), tool_result],
            [], timeout_s=2)
        self.assertEqual(second.tool_calls, [])
        assistant_encoded = requests[1]["messages"][1]
        self.assertEqual(assistant_encoded["reasoning_content"],
                         "synthetic-private-context")
        self.assertEqual(assistant_encoded["tool_calls"][0]["id"], "call_1")
        self.assertEqual(assistant_encoded["tool_calls"][0]["function"]["arguments"],
                         '{"start":"2026-09-01"}')
        tool_encoded = requests[1]["messages"][2]
        self.assertEqual(tool_encoded["tool_call_id"], "call_1")

    def test_invalid_json_arguments_preserve_id(self):
        from bi_agent.llm import Message

        def provider_response(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": None,
                                          "tool_calls": [{"id": "call_9",
                                                           "type": "function",
                                                           "function": {
                                                               "name": "query_business",
                                                               "arguments": '{bad json'}}]}}],
            })

        model = self._model("qwen", provider_response)
        reply = model.complete([Message(role="user", content="q")], [], timeout_s=2)
        self.assertIsNone(reply.tool_calls[0].arguments)
        self.assertEqual(reply.tool_calls[0].arguments_error, "invalid_json")
        self.assertEqual(reply.tool_calls[0].id, "call_9")

    def test_usage_missing_and_partial(self):
        from bi_agent.llm import Message

        def missing_usage(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        model = self._model("qwen", missing_usage)
        reply = model.complete([Message(role="user", content="q")], [], timeout_s=2)
        self.assertIsNone(reply.usage)

        def partial_usage(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 7}})

        model = self._model("qwen", partial_usage)
        reply = model.complete([Message(role="user", content="q")], [], timeout_s=2)
        self.assertEqual(reply.usage, {"prompt_tokens": 7, "completion_tokens": None,
                                       "total_tokens": None})

    def test_error_mapping(self):
        from bi_agent.llm import Message, ModelError

        for status, code in ((401, "authentication"), (403, "authentication"),
                             (429, "rate_limit"), (500, "unavailable"), (503, "unavailable")):
            with self.subTest(status=status):
                model = self._model("deepseek",
                                    lambda request, s=status: httpx.Response(s))
                with self.assertRaises(ModelError) as ctx:
                    model.complete([Message(role="user", content="q")], [], timeout_s=2)
                self.assertEqual(ctx.exception.code, code)

    def test_timeout_budget_exhausted(self):
        import asyncio as asyncio_module

        from bi_agent.llm import Message, ModelError

        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio_module.sleep(1)
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        model = self._model("qwen", slow)
        with self.assertRaises(ModelError) as ctx:
            model.complete([Message(role="user", content="q")], [], timeout_s=0.05)
        self.assertEqual(ctx.exception.code, "timeout")

    def test_duplicate_and_missing_tool_ids_rejected(self):
        from bi_agent.llm import Message, ModelError

        duplicate = {
            "choices": [{"message": {"role": "assistant", "content": None,
                                      "tool_calls": [
                                          {"id": "c1", "type": "function",
                                           "function": {"name": "a", "arguments": "{}"}},
                                          {"id": "c1", "type": "function",
                                           "function": {"name": "b", "arguments": "{}"}}]}}]}
        missing = {
            "choices": [{"message": {"role": "assistant", "content": None,
                                      "tool_calls": [
                                          {"type": "function",
                                           "function": {"name": "a", "arguments": "{}"}}]}}]}
        for payload in (duplicate, missing):
            with self.subTest():
                model = self._model("qwen",
                                    lambda request, p=payload: httpx.Response(200, json=p))
                with self.assertRaises(ModelError) as ctx:
                    model.complete([Message(role="user", content="q")], [], timeout_s=2)
                self.assertEqual(ctx.exception.code, "invalid_response")

    def test_tools_and_endpoint_passthrough(self):
        from bi_agent.llm import DEFAULT_BASE_URLS, Message

        bodies: list[dict] = []

        def provider_response(request: httpx.Request) -> httpx.Response:
            bodies.append({"url": str(request.url),
                           "body": json.loads(request.content)})
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        tools = [{"type": "function", "function": {"name": "query_business",
                                                    "parameters": {"type": "object"}}}]
        model = self._model("qwen", provider_response)
        model.complete([Message(role="user", content="q")], tools, timeout_s=2)
        self.assertEqual(bodies[0]["url"],
                         DEFAULT_BASE_URLS["qwen"] + "/chat/completions")
        self.assertEqual(bodies[0]["body"]["tools"], tools)
        self.assertEqual(bodies[0]["body"]["model"], "demo-model")
        self.assertNotIn("response_format", bodies[0]["body"])

    def test_create_model_mapping(self):
        from bi_agent.llm import CompatibleChatModel, create_model

        for provider in ("qwen", "deepseek"):
            model = create_model(_model_settings(provider))
            self.assertIsInstance(model, CompatibleChatModel)


if __name__ == "__main__":
    unittest.main()
