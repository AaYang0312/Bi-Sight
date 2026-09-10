"""Contracts for the deterministic business-query state graph."""

import unittest
from dataclasses import is_dataclass
from datetime import date, datetime, timedelta, timezone
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
from bi_agent.metrics import Coverage
from bi_agent.runtime.models import (
    ArtifactRef,
    DomainStatus,
    ErrorEnvelope,
    RecoveryAction,
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

    def test_constructor_rejects_raw_question_diagnostic_and_real_id(self):
        cases = (
            {"normalized_request": {"question": "查询店铺 S1 的销售额"}},
            {"limitations": ["psycopg.errors.SyntaxError: relation missing"]},
            {"normalized_request": {"shop_aliases": ["S1"]}},
        )

        for values in cases:
            with self.subTest(values=values), self.assertRaisesRegex(
                ValidationError, "unsafe_persistence_payload"
            ):
                BusinessQueryState(run_id=uuid4(), **values)

    def test_assignment_rejects_unsafe_persisted_values(self):
        state = BusinessQueryState(run_id=uuid4())

        with self.assertRaisesRegex(ValidationError, "unsafe_persistence_payload"):
            state.limitations = ["psycopg.errors.SyntaxError: relation missing"]

        self.assertEqual(state.limitations, [])

    def test_all_dump_paths_reject_constructed_unsafe_state(self):
        state = BusinessQueryState.model_construct(
            run_id=uuid4(),
            normalized_request={"question": "查询店铺 S1 的销售额"},
        )
        dump_paths = (
            state.model_dump,
            lambda: state.model_dump(mode="json"),
            state.model_dump_json,
        )

        for dump in dump_paths:
            with self.subTest(dump=dump), self.assertRaisesRegex(
                ValueError, "^unsafe_persistence_payload$"
            ):
                dump()

    def test_model_copy_revalidates_unsafe_update(self):
        state = BusinessQueryState(run_id=uuid4())

        with self.assertRaisesRegex(ValidationError, "unsafe_persistence_payload"):
            state.model_copy(update={"normalized_request": {"shop_aliases": ["S1"]}})

    def test_safe_future_state_shape_remains_serializable(self):
        now = datetime.now(timezone.utc)
        state = BusinessQueryState(
            run_id=uuid4(),
            node=BusinessQueryNode.CLASSIFY_RESULT,
            normalized_request={
                "shop_aliases": ["shop_1"],
                "metrics": ["paid_amount"],
                "start": "2026-09-01",
                "end": "2026-09-08",
                "group_by": "total",
                "compare": "none",
                "top_n": 10,
                "currency": "CNY",
            },
            problems=["invalid_metric"],
            tool_status="ok",
            target_status=DomainStatus.SUCCESS,
            coverage=Coverage(
                status="complete",
                start=date(2026, 9, 1),
                end=date(2026, 9, 8),
            ),
            data_as_of=now,
            limitations=["coverage_incomplete"],
            artifact_refs=[ArtifactRef(id=uuid4(), type="metric_result")],
            error=ErrorEnvelope(
                code="invalid_parameters",
                stage="validate_parameters",
                retryable=False,
                recovery=RecoveryAction.CORRECT_PARAMETERS,
                public_message="查询参数无效",
                problems=["invalid_metric"],
            ),
        )

        self.assertEqual(state.model_dump(mode="json")["node"], "classify_result")
        self.assertIn("classify_result", state.model_dump_json())

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
