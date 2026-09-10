"""核心离线检查：配置、快麦接口边界、指标输入、模型回合、预算假设与对话。"""

import hashlib
import hmac
import json
import logging
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import httpx

from bi_agent.llm import ToolCall
from bi_agent.metrics import Coverage, ToolResult


class ConfigTests(unittest.TestCase):
    def test_app_settings_use_the_chat_role_and_trusted_origin(self):
        from bi_agent.config import load_app_settings

        env = {
            "APP_ENV": "production",
            "APP_ALLOWED_SUBJECTS": "subject-a",
            "APP_PUBLIC_ORIGIN": "https://bi.example.com",
            "AUTH_SUBJECT_HEADER": "X-Auth-Request-Sub",
            "BI_SHOP_IDS": "S1",
            "BI_APP_DSN": "postgresql://bi_app:password@localhost/bi_agent",
        }
        settings = load_app_settings(env)
        self.assertEqual(settings.app_dsn.get_secret_value(), env["BI_APP_DSN"])
        self.assertEqual(settings.public_origin, "https://bi.example.com")
        self.assertEqual(settings.auth_subject_header, "X-Auth-Request-Sub")
        with self.assertRaises(ValueError):
            load_app_settings({**env, "APP_PUBLIC_ORIGIN": "http://bi.example.com"})

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
        live_empty = parse_page({"success": True}, allow_omitted_list=True)
        self.assertEqual(live_empty.rows, [])
        self.assertTrue(live_empty.verified_empty)
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


class SyncNormalisationTests(unittest.TestCase):
    def test_trade_uses_documented_status_split_and_line_fields(self):
        """A1/A3/A4/A6：API字段应产生可复核的规范化结果。"""
        from bi_agent.sync import normalise_trade

        trade = normalise_trade({
            "sid": "E1", "userId": "S1", "updTime": 1788537600000,
            "unifiedStatus": "CLOSED", "sysStatus": "FINISHED",
            "splitType": 1, "splitSid": "E_PARENT",
            "orders": [{"id": "L1", "oid": "PLATFORM-L1", "type": 2,
                        "giftNum": "0"}],
        })

        self.assertFalse(trade["active"])
        self.assertEqual(trade["unified_status"], "CLOSED")
        self.assertEqual(trade["system_status"], "FINISHED")
        self.assertEqual(trade["split_parent_id"], "E_PARENT")
        self.assertEqual(trade["items"][0]["platform_line_id"], "PLATFORM-L1")
        self.assertEqual(trade["items"][0]["source_type"], 2)
        self.assertEqual(trade["items"][0]["line_kind"], "suite")
        self.assertFalse(trade["items"][0]["active"])

    def test_trade_uses_system_status_only_when_unified_status_is_missing(self):
        from bi_agent.sync import normalise_trade

        base = {"sid": "E1", "userId": "S1", "updTime": 1788537600000}
        self.assertFalse(normalise_trade({**base, "sysStatus": "CLOSED"})["active"])
        self.assertTrue(normalise_trade({
            **base, "unifiedStatus": "FINISHED", "sysStatus": "CLOSED",
        })["active"])

    def test_mixed_gift_line_keeps_sale_kind_and_reports_gift_quantity(self):
        """回归：`num>0` 且 `giftNum>0` 的混合行不得整行判为赠品。否则
        `line_kind <> 'gift'` 过滤会连同销售数量与该行分摊金额一起从商品排行消失。"""
        from bi_agent.sync import normalise_trade

        trade = normalise_trade({
            "sid": "E1", "userId": "S1", "updTime": 1788537600000,
            "orders": [
                {"oid": "L_MIX", "type": 0, "num": "2", "giftNum": "1",
                 "payAmount": "30"},
                {"oid": "L_PURE_GIFT", "type": 0, "num": "0", "giftNum": "3",
                 "payAmount": "0"},
            ],
        })

        mixed, pure = trade["items"]
        self.assertEqual(mixed["line_kind"], "sale")
        self.assertEqual(mixed["quantity"], Decimal("2"))
        self.assertEqual(mixed["gift_quantity"], Decimal("1"))
        self.assertEqual(mixed["allocated_paid_amount"], Decimal("30"))
        self.assertEqual(pure["line_kind"], "gift")
        self.assertEqual(pure["gift_quantity"], Decimal("3"))
        self.assertEqual(pure["quantity"], Decimal("0"))

    def test_aftersale_uses_finished_and_excludes_multi_value_void_status(self):
        from bi_agent.sync import normalise_aftersale

        record = normalise_aftersale({
            "aftersaleId": "A1", "userId": "S1", "modified": 1788537600000,
            "onlineStatus": 7, "status": "2,10", "finished": 1788624000000,
            "platformCompleteTime": 1788624000000,
        })

        self.assertEqual(record["work_status"], 2)
        self.assertEqual(record["system_completed_at"],
                         datetime.fromtimestamp(1788624000, tz=ZoneInfo("Asia/Shanghai")))
        self.assertFalse(record["platform_success"])

    def test_shop_sync_uses_active_flag(self):
        from bi_agent.sync import sync_shops

        class Transaction:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        class Connection:
            def __init__(self):
                self.parameters = []

            def transaction(self):
                return Transaction()

            def execute(self, sql, parameters):
                self.parameters.append(parameters)

        class Client:
            def call(self, method, parameters):
                return {"success": True, "total": 2, "hasNext": False, "list": [
                    {"userId": "S_DISABLED", "state": 1, "active": 0},
                    {"userId": "S_ACTIVE", "state": 4, "active": 1},
                ]}

        conn = Connection()
        self.assertEqual(sync_shops(conn, Client()), 2)
        self.assertFalse(conn.parameters[0][-1])
        self.assertTrue(conn.parameters[1][-1])


class SyncSchemaTests(unittest.TestCase):
    def test_sync_schema_rejects_missing_mapping_columns(self):
        from bi_agent.kuaimai import KuaimaiError
        from bi_agent.sync import assert_sync_schema

        class Rows:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self, rows):
                self.rows = rows

            def execute(self, sql, parameters):
                return Rows(self.rows)

        assert_sync_schema(Connection([
            ("orders", "unified_status"), ("orders", "system_status"),
            ("order_items", "source_type"),
        ]))
        with self.assertRaisesRegex(KuaimaiError, "schema_outdated"):
            assert_sync_schema(Connection([("orders", "unified_status")]))


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


class PromotionTests(unittest.TestCase):
    NOW = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))

    def test_cap_is_exact_and_missing_actual_is_not_zero(self):
        from bi_agent.metrics import ToolResult
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="sales_cap", start="2026-10-01", end="2026-11-01",
                                   sales_estimate="100000", target_ratio="0.12")
        result = evaluate_promotion(request, confirmed_inputs={
            "sales_estimate": Decimal("100000"), "target_ratio": Decimal("0.12")},
            now=self.NOW)
        self.assertEqual(Decimal(result.data[0]["spend_cap"]), Decimal("12000"))
        actual = PromotionRequest(mode="actual_budget", start="2026-09-01", end="2026-10-01")
        outcome = evaluate_promotion(actual, confirmed_inputs={}, now=self.NOW)
        self.assertEqual(outcome.status, "missing_data")

    def test_budget_scenario_overrun(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="budget_scenario", start="2026-09-01",
                                   end="2026-09-08", budget="100", assumed_spend="120",
                                   spent_through="2026-09-06")
        result = evaluate_promotion(request, confirmed_inputs={
            "budget": Decimal("100"), "assumed_spend": Decimal("120")}, now=self.NOW)
        row = result.data[0]
        self.assertEqual(Decimal(row["remaining_budget"]), Decimal("0"))
        self.assertEqual(Decimal(row["overrun"]), Decimal("20"))
        self.assertEqual(row["remaining_days"], 2)
        self.assertEqual(Decimal(row["daily_allowance"]), Decimal("0"))
        self.assertEqual(row["basis"], "用户输入假设")

    def test_zero_budget(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="budget_scenario", start="2026-09-01",
                                   end="2026-09-08", budget="0", assumed_spend="0",
                                   spent_through="2026-09-06")
        result = evaluate_promotion(request, confirmed_inputs={
            "budget": Decimal("0"), "assumed_spend": Decimal("0")}, now=self.NOW)
        row = result.data[0]
        self.assertEqual(Decimal(row["remaining_budget"]), Decimal("0"))
        self.assertEqual(Decimal(row["daily_allowance"]), Decimal("0"))

    def test_period_ended_no_division_by_zero(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="budget_scenario", start="2026-09-01",
                                   end="2026-09-08", budget="100", assumed_spend="60",
                                   spent_through="2026-09-08")
        result = evaluate_promotion(request, confirmed_inputs={
            "budget": Decimal("100"), "assumed_spend": Decimal("60")}, now=self.NOW)
        row = result.data[0]
        self.assertIsNone(row["daily_allowance"])
        self.assertTrue(any("周期已结束" in item for item in result.limitations))

    def test_ratio_over_100_percent_rejected(self):
        from bi_agent.promotion import PromotionRequest

        with self.assertRaises(ValueError):
            PromotionRequest(mode="sales_cap", start="2026-10-01", end="2026-11-01",
                             sales_estimate="100000", target_ratio="1.2")

    def test_negative_and_nan_rejected(self):
        from bi_agent.promotion import PromotionRequest

        for kwargs in ({"sales_estimate": "-1", "target_ratio": "0.1"},
                       {"sales_estimate": "NaN", "target_ratio": "0.1"},
                       {"sales_estimate": "Infinity", "target_ratio": "0.1"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    PromotionRequest(mode="sales_cap", start="2026-10-01",
                                     end="2026-11-01", **kwargs)

    def test_currency_restricted(self):
        from bi_agent.promotion import PromotionRequest

        with self.assertRaises(ValueError):
            PromotionRequest(mode="sales_cap", start="2026-10-01", end="2026-11-01",
                             sales_estimate="100000", target_ratio="0.1", currency="USD")

    def test_unconfirmed_amounts_rejected(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="sales_cap", start="2026-10-01", end="2026-11-01",
                                   sales_estimate="100000", target_ratio="0.12")
        result = evaluate_promotion(request, confirmed_inputs={}, now=self.NOW)
        self.assertEqual(result.status, "invalid_parameters")

    def test_mismatched_confirmed_inputs_rejected(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="budget_scenario", start="2026-09-01",
                                   end="2026-09-08", budget="100", assumed_spend="120",
                                   spent_through="2026-09-06")
        result = evaluate_promotion(request, confirmed_inputs={
            "budget": Decimal("999"), "assumed_spend": Decimal("120")}, now=self.NOW)
        self.assertEqual(result.status, "invalid_parameters")
        self.assertTrue(any("不一致" in item for item in result.limitations))

    def test_extra_mode_fields_rejected(self):
        from bi_agent.promotion import PromotionRequest

        with self.assertRaises(ValueError):
            PromotionRequest(mode="sales_cap", start="2026-10-01", end="2026-11-01",
                             sales_estimate="100000", target_ratio="0.1",
                             budget="500")

    def test_contribution_cap_missing_data(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="contribution_cap", start="2026-09-01",
                                   end="2026-10-01")
        result = evaluate_promotion(request, confirmed_inputs={}, now=self.NOW)
        self.assertEqual(result.status, "missing_data")
        self.assertTrue(any("推广实耗" in item for item in result.limitations))

    def test_span_limit(self):
        from bi_agent.promotion import PromotionRequest

        with self.assertRaises(ValueError):
            PromotionRequest(mode="sales_cap", start="2025-01-01", end="2026-09-08",
                             sales_estimate="1", target_ratio="0.1")


def _reply(text=None, calls=None, reasoning=None):
    from bi_agent.llm import Message, ModelReply, ToolCall

    calls = calls or []
    assistant = Message(role="assistant", content=text, tool_calls=calls,
                        provider_context=({"reasoning_content": reasoning}
                                          if reasoning else {}))
    reply = ModelReply(text=text, tool_calls=calls)
    reply._message = assistant
    return reply


class AgentTests(unittest.TestCase):
    NOW = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    KNOWN = ToolResult(status="ok", data=[{"paid_amount": "1000"}],
                       coverage=Coverage(status="complete", start=date(2026, 9, 1),
                                         end=date(2026, 9, 8)))

    def setUp(self):
        from bi_agent.runtime.memory import MemoryQueryRunStore

        self.run_store = MemoryQueryRunStore(
            forbidden_values={"S1", "ERP-P-9"},
        )

    def _conn(self, shops=(("S1", "店铺A"),)):
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [tuple(s) for s in shops]
        return conn

    def _call(self, **overrides):
        from bi_agent.llm import ToolCall

        args = {"start": "2026-09-01", "end": "2026-09-08",
                "shop_ids": ["shop_1"], "metrics": ["paid_amount"]}
        args.update(overrides)
        return ToolCall(id="call_1", name="query_business", arguments=args)

    def test_first_turn_maps_alias_and_calls_query(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call()]),
            _reply(text="最近7天支付金额1000元")]
        with patch("bi_agent.business_query.nodes.metrics.query_business",
                   return_value=self.KNOWN) as query:
            turn = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
            self.assertEqual(query.call_args.args[1].shop_ids, ["S1"])
            self.assertEqual(query.call_args.args[1].start, date(2026, 9, 1))
            self.assertEqual(turn.results[0].data, self.KNOWN.data)
        self.assertEqual(len(self.run_store.runs), 1)
        self.assertEqual(len(self.run_store.artifacts), 1)
        first_model_messages = model.complete.call_args_list[0].args[0]
        model_question = [message.content for message in first_model_messages
                          if message.role == "user"][-1]
        self.assertIn("shop_1", model_question)
        self.assertNotIn("S1", model_question)
        self.assertEqual(turn.text, "最近7天支付金额1000元")

    def test_agent_routes_business_queries_only_through_the_graph_adapter(self):
        import bi_agent.agent as agent

        self.assertFalse(hasattr(agent, "_handle_query_business"))
        self.assertFalse(hasattr(agent, "_run_query_business"))

    def test_follow_up_keeps_filters_only_dates_change(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call()]),
            _reply(text="最近7天支付金额1000元")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN):
            turn1 = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                           model=model, conn=self._conn(),
                           allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                           run_store=self.run_store)
        model.complete.side_effect = [
            _reply(calls=[ToolCall(id="call_2", name="query_business",
                                   arguments={"start": "2026-08-01",
                                              "end": "2026-09-01"})]),
            _reply(text="上个月支付500元")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            turn2 = answer("那上个月呢", turn1.state, model=model,
                           conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                           now=self.NOW, run_store=self.run_store)
            request = query.call_args.args[1]
            self.assertEqual(request.shop_ids, ["S1"])
            self.assertEqual(request.metrics, ["paid_amount"])
            self.assertEqual(request.start, date(2026, 8, 1))
            self.assertEqual(request.end, date(2026, 9, 1))
        self.assertEqual(turn2.text, "上个月支付500元")

    def test_sales_ambiguity_clarifies_without_model(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        turn = answer("我店里销售额怎么样？", SessionState(subject="u1"), model=model,
                      conn=self._conn(), allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                      run_store=self.run_store)
        self.assertIsNotNone(turn.clarification)
        self.assertIn("口径", turn.clarification)
        self.assertEqual(turn.results, [])
        model.complete.assert_not_called()

    def test_same_name_shop_clarifies(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        turn = answer("店铺A上周业绩", SessionState(subject="u1"), model=model,
                      conn=self._conn(shops=(("S1", "店铺A"), ("S3", "店铺A"))),
                      allowed_shop_ids=frozenset({"S1", "S3"}), now=self.NOW,
                      run_store=self.run_store)
        self.assertIsNotNone(turn.clarification)
        self.assertIn("同名", turn.clarification)
        model.complete.assert_not_called()

    def test_unknown_tool_rejected(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ToolCall

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(id="call_1", name="run_sql",
                                   arguments={"sql": "SELECT 1"})]),
            _reply(text="只能使用两个工具")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            turn = answer("查点什么", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW, run_store=self.run_store)
            query.assert_not_called()
        self.assertEqual(turn.results, [])
        tool_messages = [m for m in turn.state.turns if m.role == "tool"]
        self.assertTrue(any("unknown_tool" in (m.content or "") for m in tool_messages))

    def test_unauthorized_alias_and_injection_rejected(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call(shop_ids=["shop_2"])]),
            _reply(text="好的")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            answer("查询一下", SessionState(subject="u1"), model=model,
                   conn=self._conn(), allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                   run_store=self.run_store)
            query.assert_not_called()

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call(shop_ids=["S1; DROP TABLE bi.orders; --"])]),
            _reply(text="好的")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            turn = answer("查询一下", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW, run_store=self.run_store)
            query.assert_not_called()
        self.assertEqual(turn.results, [])

    def test_corrected_query_reuses_message_id_and_advances_attempt_number(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ToolCall

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(id="call_1", name="query_business",
                                   arguments=None, arguments_error="invalid_json")]),
            _reply(calls=[ToolCall(
                id="call_2",
                name="query_business",
                arguments={
                    "start": "2026-09-01",
                    "end": "2026-09-08",
                    "shop_ids": ["shop_1"],
                    "metrics": ["paid_amount"],
                },
            )]),
            _reply(text="已修正并完成查询"),
        ]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            turn = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
            query.assert_called_once()
        runs = list(self.run_store.runs.values())
        self.assertEqual(len(runs), 2)
        self.assertEqual({run["attempt_no"] for run in runs}, {1, 2})
        self.assertEqual(len({run["user_message_id"] for run in runs}), 1)
        self.assertEqual(len(turn.results), 1)
        self.assertEqual(model.complete.call_count, 3)

    def test_batch_of_five_executes_four(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ToolCall

        calls = [ToolCall(id=f"call_{i}", name="query_business",
                          arguments={"start": "2026-09-01", "end": "2026-09-08",
                                     "shop_ids": ["shop_1"],
                                     "metrics": ["paid_amount"]})
                 for i in range(1, 6)]
        model = Mock()
        model.complete.side_effect = [_reply(calls=calls)]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            turn = answer("多查询几个", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW, run_store=self.run_store)
            self.assertEqual(query.call_count, 4)
        self.assertEqual(len(turn.results), 4)
        tool_messages = [m for m in turn.state.turns if m.role == "tool"]
        self.assertTrue(any("budget_exhausted" in (m.content or "")
                            for m in tool_messages))

    def test_model_error_keeps_results(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ModelError

        model = Mock()
        model.complete.side_effect = [_reply(calls=[self._call()]),
                                      ModelError("timeout")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN):
            turn = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
        self.assertEqual(len(turn.results), 1)
        self.assertIn("固定查询入口仍可用", turn.text)

    def test_time_budget_not_rewaited(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [_reply(calls=[self._call()]),
                                      _reply(text="应该不会到达")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN):
            with patch("bi_agent.agent.time_module.monotonic",
                       side_effect=[0.0, 0.0, 100.0]), \
                 patch("bi_agent.business_query.nodes.monotonic", return_value=0.0):
                turn = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                              model=model, conn=self._conn(),
                              allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                              run_store=self.run_store)
        self.assertEqual(len(turn.results), 1)
        self.assertEqual(model.complete.call_count, 1)
        self.assertIn("预算已耗尽", turn.text)

    def test_tool_id_and_provider_context_preserved(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call()], reasoning="synthetic-private-context"),
            _reply(text="完成")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN):
            turn = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
        second_call_messages = model.complete.call_args_list[1][0][0]
        assistant = [m for m in second_call_messages if m.role == "assistant"][0]
        self.assertEqual(assistant.provider_context.get("reasoning_content"),
                         "synthetic-private-context")
        tool_messages = [m for m in second_call_messages if m.role == "tool"]
        self.assertEqual(tool_messages[0].tool_call_id, "call_1")
        # 匿名映射：发给模型的结果不含真实店铺ID
        self.assertNotIn("S1", tool_messages[0].content)

    def test_model_result_hides_erp_identifiers(self):
        from bi_agent.agent import SessionState, to_model_result

        result = ToolResult(
            status="ok",
            data=[{"shop_id": "S1", "product_id": "ERP-P-9", "paid_amount": "1000"}],
            filters={"shop_ids": ["S1"]},
            coverage=Coverage(status="complete", start=date(2026, 9, 1),
                              end=date(2026, 9, 8)),
        )
        payload = to_model_result(result, SessionState(
            subject="u1", shop_aliases={"S1": "shop_1"}))
        self.assertEqual(payload["data"][0]["shop_id"], "shop_1")
        self.assertEqual(payload["filters"]["shop_ids"], ["shop_1"])
        self.assertNotIn("S1", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("ERP-P-9", json.dumps(payload, ensure_ascii=False))

    def test_state_isolation_between_users(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [_reply(calls=[self._call()]), _reply(text="ok")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN):
            turn_a = answer("最近7天店铺A的支付金额", SessionState(subject="A"),
                            model=model, conn=self._conn(),
                            allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                            run_store=self.run_store)
        state_b = SessionState(subject="B")
        self.assertEqual(state_b.filters, {})
        self.assertNotEqual(turn_a.state.filters, {})
        self.assertNotEqual(turn_a.state.subject, state_b.subject)

    def test_pii_branch(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        phone_like_text = "138" + "1234" + "5678"
        turn = answer(f"订单{phone_like_text}退款到账了吗", SessionState(subject="u1"),
                      model=model, conn=self._conn(),
                      allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                      run_store=self.run_store)
        self.assertIsNotNone(turn.clarification)
        model.complete.assert_not_called()

    def test_explicit_assumptions_parsing(self):
        from bi_agent.agent import explicit_assumptions

        values = explicit_assumptions("假设10月销售额10万元、推广费用率12%，最多花多少？")
        self.assertEqual(values["sales_estimate"], Decimal("100000"))
        self.assertEqual(values["target_ratio"], Decimal("0.12"))
        values = explicit_assumptions(
            "假设9月1日至7日预算100元、已花120元，实耗统计到9月5日结束")
        self.assertEqual(values["budget"], Decimal("100"))
        self.assertEqual(values["assumed_spend"], Decimal("120"))
        self.assertEqual(values["_spent_through_md"], (9, 5))
        self.assertEqual(explicit_assumptions("照上次预算"), {})

    def test_promotion_tool_round(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ToolCall
        from bi_agent.metrics import Coverage, ToolResult

        promo_result = ToolResult(
            status="ok", data=[{"spend_cap": "12000", "basis": "用户输入假设"}],
            coverage=Coverage(status="missing", start=None, end=None))
        call = ToolCall(id="call_1", name="evaluate_promotion", arguments={
            "mode": "sales_cap", "start": "2026-10-01", "end": "2026-11-01",
            "sales_estimate": "100000", "target_ratio": "0.12"})
        model = Mock()
        model.complete.side_effect = [_reply(calls=[call]), _reply(text="上限12000元")]
        with patch("bi_agent.agent.evaluate_promotion", return_value=promo_result) as promo:
            turn = answer("假设10月销售额10万元、推广费用率12%，最多花多少？",
                          SessionState(subject="u1"), model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
            confirmed = promo.call_args.kwargs["confirmed_inputs"]
            self.assertEqual(confirmed["sales_estimate"], Decimal("100000"))
            self.assertEqual(confirmed["target_ratio"], Decimal("0.12"))
        self.assertEqual(turn.results[0].data[0]["spend_cap"], "12000")


if __name__ == "__main__":
    unittest.main()
