import unittest
from uuid import uuid4

from pydantic import ValidationError

from bi_agent.runtime.models import (
    ArtifactRef,
    DomainArtifact,
    DomainResult,
    DomainStatus,
    ErrorEnvelope,
    NewArtifact,
    NewQueryRun,
    RecoveryAction,
    RunCompletion,
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
