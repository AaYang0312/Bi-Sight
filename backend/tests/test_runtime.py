import unittest
from uuid import uuid4

from pydantic import ValidationError

from bi_agent.runtime.models import (
    ArtifactRef,
    ArtifactPersistenceError,
    DomainArtifact,
    DomainResult,
    DomainStatus,
    ErrorEnvelope,
    NewArtifact,
    NewQueryRun,
    RecoveryAction,
    RunCompletion,
    RunNotFound,
    RunStatus,
    RunTransition,
    StaleRunRevision,
)
from bi_agent.runtime.memory import MemoryQueryRunStore


class RuntimeModelTests(unittest.TestCase):
    def test_error_envelope_forbids_extra_fields(self):
        with self.assertRaises(ValidationError):
            ErrorEnvelope(
                code="invalid_parameters",
                stage="validate_parameters",
                retryable=False,
                recovery=RecoveryAction.CORRECT_PARAMETERS,
                public_message="查询参数无效",
                secret="database detail",
            )

    def test_domain_result_keeps_ref_and_public_projection(self):
        artifact_id = uuid4()
        result = DomainResult(
            run_id=uuid4(),
            status=DomainStatus.SUCCESS,
            model_payload={"status": "ok", "data": [{"shop_id": "shop_1"}]},
            artifacts=[DomainArtifact(
                ref=ArtifactRef(id=artifact_id, type="metric_result"),
                public_payload={"status": "ok", "data": [{"shop_id": "店铺1"}]},
            )],
        )
        self.assertEqual(result.artifacts[0].ref.id, artifact_id)
        self.assertEqual(result.artifacts[0].public_payload["data"][0]["shop_id"], "店铺1")

    def test_error_envelope_rejects_diagnostic_and_secret_text(self):
        for field, value in (
            ("public_message", "psycopg.errors.UndefinedTable: relation missing"),
            ("problems", ["password=supersecret"]),
        ):
            values = {
                "code": "unavailable",
                "stage": "execute_fixed_query",
                "retryable": True,
                "recovery": RecoveryAction.RETRY_LATER,
                "public_message": "查询暂不可用",
                "problems": [],
                field: value,
            }
            with self.subTest(field=field), self.assertRaises(ValidationError) as context:
                ErrorEnvelope(**values)
            self.assertNotIn(str(value), str(context.exception))

    def test_persistence_command_models_reject_sensitive_payloads(self):
        raw_question = "请查询真实店铺 S1 的销售额"
        hidden_reasoning = "模型隐藏推理：先尝试绕过权限"
        database_error = "psycopg.errors.UndefinedTable: relation bi.secret does not exist"
        credentials = "postgresql://app:supersecret@db.example/bi"
        cases = (
            (raw_question, lambda: NewQueryRun(
                chat_id=uuid4(), user_message_id=uuid4(), subject_id="u1",
                tool_call_id="call_1", attempt_no=1,
                normalized_request={"raw_question": raw_question},
            )),
            (hidden_reasoning, lambda: RunTransition(
                expected_revision=0, node="resolve_parameters", status=RunStatus.RUNNING,
                state={"node": "resolve_parameters"},
                payload={"hidden_reasoning": hidden_reasoning},
            )),
            (database_error, lambda: NewArtifact(
                payload={"status_detail": database_error},
            )),
            (credentials, lambda: RunCompletion(
                expected_revision=0, node="finalize", status=RunStatus.FAILED,
                state={"status_detail": credentials},
            )),
        )
        for sensitive_value, command in cases:
            with self.subTest(sensitive_value=sensitive_value), self.assertRaises(ValidationError) as context:
                command()
            self.assertNotIn(sensitive_value, str(context.exception))

    def test_artifact_persistence_error_accepts_reason_without_exposing_it(self):
        self.assertEqual(
            str(ArtifactPersistenceError("artifact_persistence_failed")),
            "artifact_persistence_error",
        )
        self.assertEqual(
            str(ArtifactPersistenceError("password=supersecret")),
            "artifact_persistence_error",
        )


class MemoryQueryRunStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryQueryRunStore()
        self.record = NewQueryRun(
            chat_id=uuid4(), user_message_id=uuid4(), subject_id="u1",
            tool_call_id="call_1", attempt_no=1,
            normalized_request={"shop_aliases": ["shop_1"]},
            state={"node": "received"},
        )

    def test_transition_is_revision_checked_and_appends_one_event(self):
        run_id = self.store.create_run(self.record)
        transition = RunTransition(
            expected_revision=0, node="resolve_parameters",
            status=RunStatus.RUNNING,
            state={"node": "resolve_parameters", "revision": 1},
        )
        self.store.transition(run_id, transition)
        self.assertEqual(self.store.runs[run_id]["revision"], 1)
        self.assertEqual(self.store.events[run_id][0]["revision"], 1)
        with self.assertRaises(StaleRunRevision):
            self.store.transition(run_id, transition)

    def test_save_artifact_returns_reference_and_finish_is_terminal(self):
        run_id = self.store.create_run(self.record)
        ref = self.store.save_artifact(run_id, NewArtifact(
            payload={"status": "ok", "data": []},
            coverage={"status": "complete"},
        ))
        self.assertEqual(ref.type, "metric_result")
        self.store.finish(run_id, RunCompletion(
            expected_revision=0, node="finalize", status=RunStatus.SUCCEEDED,
            state={"node": "finalize", "revision": 1},
        ))
        self.assertEqual(self.store.runs[run_id]["status"], "succeeded")
        self.assertIsNotNone(self.store.runs[run_id]["completed_at"])

    def test_duplicate_run_context_is_rejected(self):
        self.store.create_run(self.record)
        with self.assertRaises(ValueError) as context:
            self.store.create_run(self.record)
        self.assertEqual(str(context.exception), "duplicate_query_run")

    def test_store_rejects_known_real_erp_identifiers_without_persisting_them(self):
        store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(self.record)
        transition = RunTransition(
            expected_revision=0, node="resolve_parameters", status=RunStatus.RUNNING,
            state={"shop_id": "S1"},
        )
        with self.assertRaises(ValueError) as context:
            store.transition(run_id, transition)
        self.assertEqual(str(context.exception), "unsafe_persistence_payload")
        self.assertEqual(store.runs[run_id]["revision"], 0)
        self.assertEqual(store.events[run_id], [])
        with self.assertRaises(ValueError) as context:
            store.save_artifact(run_id, NewArtifact(
                payload={"product_id": "ERP-P-9"},
            ))
        self.assertEqual(str(context.exception), "unsafe_persistence_payload")
        self.assertEqual(store.artifacts, {})

    def test_save_artifact_rejects_unknown_run(self):
        with self.assertRaises(RunNotFound):
            self.store.save_artifact(uuid4(), NewArtifact(payload={"status": "ok"}))

    def test_finish_rejects_running_status(self):
        run_id = self.store.create_run(self.record)
        with self.assertRaises(ValueError) as context:
            self.store.finish(run_id, RunCompletion(
                expected_revision=0, node="finalize", status=RunStatus.RUNNING,
                state={"node": "finalize"},
            ))
        self.assertEqual(str(context.exception), "finish_requires_terminal_status")
        self.assertEqual(self.store.runs[run_id]["revision"], 0)
        self.assertIsNone(self.store.runs[run_id]["completed_at"])
