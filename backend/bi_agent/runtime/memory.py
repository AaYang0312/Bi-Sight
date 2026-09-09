"""In-memory implementation of the query-run persistence contract for tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4

from .models import (
    ArtifactRef,
    NewArtifact,
    NewQueryRun,
    RunCompletion,
    RunEventType,
    RunNotFound,
    RunStatus,
    RunTransition,
    StaleRunRevision,
)


class MemoryQueryRunStore:
    """Store query-run records in dictionaries with repository-equivalent semantics."""

    def __init__(self) -> None:
        self.runs: dict[UUID, dict[str, object]] = {}
        self.events: dict[UUID, list[dict[str, object]]] = {}
        self.artifacts: dict[UUID, dict[str, object]] = {}

    def create_run(self, record: NewQueryRun) -> UUID:
        run_id = uuid4()
        now = _now()
        self.runs[run_id] = {
            "id": run_id,
            "chat_id": record.chat_id,
            "user_message_id": record.user_message_id,
            "subject_id": record.subject_id,
            "tool_call_id": record.tool_call_id,
            "domain": record.domain,
            "attempt_no": record.attempt_no,
            "status": RunStatus.RUNNING.value,
            "current_node": record.state.get("node"),
            "revision": 0,
            "normalized_request": deepcopy(record.normalized_request),
            "state": deepcopy(record.state),
            "error_code": None,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        self.events[run_id] = []
        return run_id

    def transition(self, run_id: UUID, transition: RunTransition) -> None:
        run = self._require_current_revision(run_id, transition.expected_revision)
        revision = transition.expected_revision + 1
        now = _now()
        run.update({
            "status": transition.status.value,
            "current_node": transition.node,
            "revision": revision,
            "state": deepcopy(transition.state),
            "error_code": transition.error_code,
            "updated_at": now,
        })
        self.events[run_id].append(_event(
            run_id=run_id,
            revision=revision,
            node=transition.node,
            event_type=transition.event_type,
            status=transition.status,
            payload=transition.payload,
            created_at=now,
        ))

    def save_artifact(self, run_id: UUID, artifact: NewArtifact) -> ArtifactRef:
        self._require_run(run_id)
        artifact_id = uuid4()
        self.artifacts[artifact_id] = {
            "id": artifact_id,
            "run_id": run_id,
            "artifact_type": artifact.artifact_type,
            "payload": deepcopy(artifact.payload),
            "data_as_of": artifact.data_as_of,
            "coverage": deepcopy(artifact.coverage),
            "created_at": _now(),
        }
        return ArtifactRef(id=artifact_id, type=artifact.artifact_type)

    def finish(self, run_id: UUID, completion: RunCompletion) -> None:
        if completion.status is RunStatus.RUNNING:
            raise ValueError("finish_requires_terminal_status")
        run = self._require_current_revision(run_id, completion.expected_revision)
        revision = completion.expected_revision + 1
        now = _now()
        run.update({
            "status": completion.status.value,
            "current_node": completion.node,
            "revision": revision,
            "state": deepcopy(completion.state),
            "error_code": completion.error_code,
            "updated_at": now,
            "completed_at": now,
        })
        self.events[run_id].append(_event(
            run_id=run_id,
            revision=revision,
            node=completion.node,
            event_type=(RunEventType.FAILED if completion.status is RunStatus.FAILED
                        else RunEventType.COMPLETED),
            status=completion.status,
            payload=completion.payload,
            created_at=now,
        ))

    def _require_run(self, run_id: UUID) -> dict[str, object]:
        try:
            return self.runs[run_id]
        except KeyError as error:
            raise RunNotFound() from error

    def _require_current_revision(
        self, run_id: UUID, expected_revision: int,
    ) -> dict[str, object]:
        run = self._require_run(run_id)
        if run["revision"] != expected_revision:
            raise StaleRunRevision()
        return run


def _event(*, run_id: UUID, revision: int, node: str, event_type: RunEventType,
           status: RunStatus, payload: dict[str, object],
           created_at: datetime) -> dict[str, object]:
    return {
        "run_id": run_id,
        "revision": revision,
        "node": node,
        "event_type": event_type.value,
        "status": status.value,
        "payload": deepcopy(payload),
        "created_at": created_at,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)
