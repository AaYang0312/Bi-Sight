"""PostgreSQL implementation of the safe query-run persistence contract."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any
from uuid import UUID, uuid4

from psycopg import errors
from pydantic import ValidationError
from psycopg.types.json import Jsonb

from .models import (
    ArtifactPersistenceError,
    ArtifactRef,
    NewArtifact,
    NewQueryRun,
    RunCompletion,
    RunContextNotFound,
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


class PostgresQueryRunStore:
    """Persist the public, validated projection of a query run."""

    def __init__(self, conn: Any, *, forbidden_values: Collection[str]) -> None:
        self.conn = conn
        self._forbidden_values = frozenset(value for value in forbidden_values if value)
        if not self._forbidden_values:
            raise ValueError("forbidden_values_required")

    def create_run(self, record: NewQueryRun) -> UUID:
        record = self._revalidate_new_run(record)
        self._validate_normalized_request(record.normalized_request)
        self._validate_state(record.state)
        run_id = uuid4()
        try:
            row = self.conn.execute(
                """INSERT INTO bi.query_runs (
                       id, chat_id, user_message_id, subject_id, tool_call_id, domain,
                       attempt_no, normalized_request, state
                   )
                   SELECT %s, c.id, m.id, c.subject_id, %s, %s, %s, %s, %s
                   FROM bi.app_messages AS m
                   JOIN bi.app_chats AS c ON c.id = m.chat_id
                   WHERE m.id = %s AND c.id = %s AND m.role = 'user' AND c.subject_id = %s
                   RETURNING id""",
                (
                    run_id,
                    record.tool_call_id,
                    record.domain,
                    record.attempt_no,
                    Jsonb(record.normalized_request),
                    Jsonb(record.state),
                    record.user_message_id,
                    record.chat_id,
                    record.subject_id,
                ),
            ).fetchone()
        except errors.UniqueViolation as error:
            raise ValueError("duplicate_query_run") from error
        if row is None:
            raise RunContextNotFound()
        return row[0]

    def transition(self, run_id: UUID, transition: RunTransition) -> None:
        transition = self._revalidate_transition(transition)
        self._validate_state(transition.state)
        normalized_request = transition_normalized_request(transition)
        if normalized_request is not None:
            self._validate_normalized_request(normalized_request)
        self._validate_event(transition.payload)
        with self.conn.transaction():
            row = self.conn.execute(
                """UPDATE bi.query_runs
                   SET status = %s, current_node = %s, revision = revision + 1,
                       normalized_request = COALESCE(%s, normalized_request), state = %s,
                       error_code = %s, updated_at = now()
                   WHERE id = %s AND revision = %s
                   RETURNING revision""",
                (
                    transition.status.value,
                    transition.node,
                    Jsonb(normalized_request) if normalized_request is not None else None,
                    Jsonb(transition.state),
                    transition.error_code,
                    run_id,
                    transition.expected_revision,
                ),
            ).fetchone()
            if row is None:
                self._raise_missing_or_stale(run_id)
            self._insert_event(
                run_id=run_id,
                revision=row[0],
                node=transition.node,
                event_type=transition.event_type,
                status=transition.status,
                payload=transition.payload,
            )

    def save_artifact(self, run_id: UUID, artifact: NewArtifact) -> ArtifactRef:
        artifact = self._revalidate_artifact(artifact)
        self._validate_artifact(artifact.payload)
        if artifact.coverage is not None:
            self._validate_coverage(artifact.coverage)
        artifact_id = uuid4()
        try:
            self.conn.execute(
                """INSERT INTO bi.query_artifacts (
                       id, run_id, artifact_type, payload, data_as_of, coverage
                   ) VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    artifact_id,
                    run_id,
                    artifact.artifact_type,
                    Jsonb(artifact.payload),
                    artifact.data_as_of,
                    Jsonb(artifact.coverage),
                ),
            )
        except errors.ForeignKeyViolation:
            raise RunNotFound() from None
        except errors.Error:
            raise ArtifactPersistenceError() from None
        return ArtifactRef(id=artifact_id, type=artifact.artifact_type)

    def finish(self, run_id: UUID, completion: RunCompletion) -> None:
        completion = self._revalidate_completion(completion)
        if completion.status is RunStatus.RUNNING:
            raise ValueError("finish_requires_terminal_status")
        self._validate_state(completion.state)
        self._validate_event(completion.payload)
        with self.conn.transaction():
            row = self.conn.execute(
                """UPDATE bi.query_runs
                   SET status = %s, current_node = %s, revision = revision + 1,
                       state = %s, error_code = %s, updated_at = now(), completed_at = now()
                   WHERE id = %s AND revision = %s
                   RETURNING revision""",
                (
                    completion.status.value,
                    completion.node,
                    Jsonb(completion.state),
                    completion.error_code,
                    run_id,
                    completion.expected_revision,
                ),
            ).fetchone()
            if row is None:
                self._raise_missing_or_stale(run_id)
            self._insert_event(
                run_id=run_id,
                revision=row[0],
                node=completion.node,
                event_type=(RunEventType.FAILED if completion.status is RunStatus.FAILED
                            else RunEventType.COMPLETED),
                status=completion.status,
                payload=completion.payload,
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

    def _raise_missing_or_stale(self, run_id: UUID) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM bi.query_runs WHERE id = %s", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFound()
        raise StaleRunRevision()

    def _insert_event(self, *, run_id: UUID, revision: int, node: str,
                      event_type: RunEventType, status: RunStatus,
                      payload: dict[str, object]) -> None:
        self.conn.execute(
            """INSERT INTO bi.query_run_events (
                   run_id, revision, node, event_type, status, payload
               ) VALUES (%s, %s, %s, %s, %s, %s)""",
            (run_id, revision, node, event_type.value, status.value, Jsonb(payload)),
        )

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
