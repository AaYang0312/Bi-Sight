"""Contracts for the deterministic business-query state graph."""

import unittest
from dataclasses import is_dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pydantic import ValidationError

from bi_agent.business_query import (
    BusinessQueryContext,
    BusinessQueryInput,
    BusinessQueryNode,
    BusinessQueryRuntime,
    BusinessQueryState,
    InvalidBusinessQueryTransition,
    transition_state,
)


class BusinessQueryTransitionTests(unittest.TestCase):
    def test_normal_path_is_fixed(self):
        state = BusinessQueryState(run_id=uuid4())
        for node in (
            BusinessQueryNode.RESOLVE_PARAMETERS,
            BusinessQueryNode.VALIDATE_PARAMETERS,
            BusinessQueryNode.AUTHORIZE_SCOPE,
            BusinessQueryNode.EXECUTE_FIXED_QUERY,
            BusinessQueryNode.CLASSIFY_RESULT,
            BusinessQueryNode.PERSIST_ARTIFACT,
            BusinessQueryNode.FINALIZE,
        ):
            state = transition_state(state, node)
        self.assertEqual(state.node, BusinessQueryNode.FINALIZE)

    def test_execute_cannot_skip_validation(self):
        state = BusinessQueryState(run_id=uuid4())
        with self.assertRaisesRegex(InvalidBusinessQueryTransition, "^invalid_transition$"):
            transition_state(state, BusinessQueryNode.EXECUTE_FIXED_QUERY)


class BusinessQueryStateContractTests(unittest.TestCase):
    def test_state_rejects_ephemeral_context_fields(self):
        with self.assertRaises(ValidationError):
            BusinessQueryState(run_id=uuid4(), question="查询店铺 S1 的销售额")

    def test_state_dump_revalidates_persisted_allowlist(self):
        state = BusinessQueryState(
            run_id=uuid4(),
            normalized_request={"question": "查询店铺 S1 的销售额"},
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            state.model_dump(mode="json")

    def test_ephemeral_runtime_data_is_not_in_persisted_state(self):
        now = datetime.now(timezone.utc)
        context = BusinessQueryContext(
            chat_id=uuid4(),
            user_message_id=uuid4(),
            subject_id="user-1",
            question="查询真实店铺 S1 的销售额",
            previous_filters={"shop_id": "S1"},
            shop_aliases={"shop_1": "S1"},
            allowed_shop_ids={"S1"},
            now=now,
            deadline=now + timedelta(seconds=30),
            attempt_no=1,
        )
        runtime = BusinessQueryRuntime(
            state=BusinessQueryState(run_id=uuid4()),
            context=context,
        )

        self.assertTrue(is_dataclass(runtime))
        self.assertNotIn("S1", str(runtime.state.model_dump(mode="json")))
        self.assertNotIn("question", runtime.state.model_dump(mode="json"))


class BusinessQueryInputTests(unittest.TestCase):
    def test_input_keeps_parse_failure_auditable(self):
        payload = BusinessQueryInput(
            tool_call_id="call_1",
            arguments=None,
            arguments_error="invalid_json",
        )

        self.assertEqual(payload.tool_call_id, "call_1")
        self.assertIsNone(payload.arguments)
        self.assertEqual(payload.arguments_error, "invalid_json")
