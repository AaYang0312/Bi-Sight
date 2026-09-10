"""In-memory implementation of the query-run persistence contract for tests."""

from __future__ import annotations

from collections.abc import Collection
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import ValidationError

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
    validate_artifact_payload,
    validate_coverage_payload,
    validate_event_payload,
    validate_normalized_request,
    validate_persisted_state,
    transition_normalized_request,
)


class MemoryQueryRunStore:
    """Store query-run records in dictionaries with repository-equivalent semantics."""

    def __init__(self, *, forbidden_values: Collection[str]) -> None:
        self._forbidden_values = frozenset(value for value in forbidden_values if value)
        if not self._forbidden_values:
            raise ValueError("forbidden_values_required")
        self.runs: dict[UUID, dict[str, object]] = {}
        self.events: dict[UUID, list[dict[str, object]]] = {}
        self.artifacts: dict[UUID, dict[str, object]] = {}

    def create_run(self, record: NewQueryRun) -> UUID:
        record = self._revalidate_new_run(record)
        self._validate_normalized_request(record.normalized_request)
        self._validate_state(record.state)
        if self._has_run_context(record):
            raise ValueError("duplicate_query_run")
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
        transition = self._revalidate_transition(transition)
        self._validate_state(transition.state)
        normalized_request = transition_normalized_request(transition)
        if normalized_request is not None:
            self._validate_normalized_request(normalized_request)
        self._validate_event(transition.payload)
        run = self._require_current_revision(run_id, transition.expected_revision)
        revision = transition.expected_revision + 1
        now = _now()
        update = {
            "status": transition.status.value,
            "current_node": transition.node,
            "revision": revision,
            "state": deepcopy(transition.state),
            "error_code": transition.error_code,
            "updated_at": now,
        }
        if normalized_request is not None:
            update["normalized_request"] = deepcopy(normalized_request)
        run.update(update)
        self.events[run_id].append(_event(
            run_id=run_id,
            revision=revision,
            node=transition.node,
            event_type=transition.event_type,
            status=transition.status,
            payload=deepcopy(transition.payload),
            created_at=now,
        ))

    def save_artifact(self, run_id: UUID, artifact: NewArtifact) -> ArtifactRef:
        self._require_run(run_id)
        artifact = self._revalidate_artifact(artifact)
        self._validate_artifact(artifact.payload)
        if artifact.coverage is not None:
            self._validate_coverage(artifact.coverage)
        artifact_id = uuid4()
        self.artifacts[artifact_id] = {
            "id": artifact_id,
            "run_id": run_id,
            "artifact_type": artifact.artifact_type,
            "payload": deepcopy(artifact.payload),
            "data_as_of": artifact.data_as_of,
            "coverage": deepcopy(artifact.coverage) if artifact.coverage is not None else None,
            "created_at": _now(),
        }
        return ArtifactRef(id=artifact_id, type=artifact.artifact_type)

    def finish(self, run_id: UUID, completion: RunCompletion) -> None:
        completion = self._revalidate_completion(completion)
        if completion.status is RunStatus.RUNNING:
            raise ValueError("finish_requires_terminal_status")
        self._validate_state(completion.state)
        self._validate_event(completion.payload)
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
            payload=deepcopy(completion.payload),
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

    def _has_run_context(self, record: NewQueryRun) -> bool:
        return any(
            run["user_message_id"] == record.user_message_id
            and run["domain"] == record.domain
            and run["attempt_no"] == record.attempt_no
            for run in self.runs.values()
        )

    def _validate_normalized_request(self, value: object) -> None:
        validate_normalized_request(value)
        self._reject_forbidden_values(value)

    def _validate_state(self, value: object) -> None:
        validate_persisted_state(value)
        self._reject_forbidden_values(value)

    def _validate_event(self, value: object) -> None:
        validate_event_payload(value)
        self._reject_forbidden_values(value)

    def _validate_artifact(self, value: object) -> None:
        validate_artifact_payload(value)
        self._reject_forbidden_values(value)

    def _validate_coverage(self, value: object) -> None:
        validate_coverage_payload(value)
        self._reject_forbidden_values(value)

    def _reject_forbidden_values(self, value: object) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                self._reject_forbidden_values(nested)
        elif isinstance(value, list):
            for nested in value:
                self._reject_forbidden_values(nested)
        elif isinstance(value, str) and value in self._forbidden_values:
            raise ValueError("unsafe_persistence_payload")

    @staticmethod
    def _revalidate_new_run(record: NewQueryRun) -> NewQueryRun:
        try:
            return NewQueryRun.model_validate(record.model_dump(warnings=False))
        except ValidationError as error:
            raise ValueError("unsafe_persistence_payload") from error

    @staticmethod
    def _revalidate_transition(transition: RunTransition) -> RunTransition:
        try:
            return RunTransition.model_validate(transition.model_dump(warnings=False))
        except ValidationError as error:
            if "normalized_request_mismatch" in str(error):
                raise ValueError("normalized_request_mismatch") from error
            raise ValueError("unsafe_persistence_payload") from error

    @staticmethod
    def _revalidate_artifact(artifact: NewArtifact) -> NewArtifact:
        try:
            return NewArtifact.model_validate(artifact.model_dump(warnings=False))
        except ValidationError as error:
            raise ValueError("unsafe_persistence_payload") from error

    @staticmethod
    def _revalidate_completion(completion: RunCompletion) -> RunCompletion:
        try:
            return RunCompletion.model_validate(completion.model_dump(warnings=False))
        except ValidationError as error:
            raise ValueError("unsafe_persistence_payload") from error


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
