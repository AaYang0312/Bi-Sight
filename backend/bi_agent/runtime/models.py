"""Public models and store protocol for deterministic query runs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from bi_agent.metrics import Coverage


class DomainStatus(StrEnum):
    SUCCESS = "success"
    NEEDS_INPUT = "needs_input"
    MISSING_DATA = "missing_data"
    PARTIAL = "partial"
    FAILED = "failed"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NEEDS_INPUT = "needs_input"
    MISSING_DATA = "missing_data"
    PARTIAL = "partial"
    FAILED = "failed"


class RecoveryAction(StrEnum):
    NONE = "none"
    ASK_USER = "ask_user"
    CORRECT_PARAMETERS = "correct_parameters"
    RETRY_LATER = "retry_later"


class RunEventType(StrEnum):
    ENTERED = "entered"
    COMPLETED = "completed"
    FAILED = "failed"
    TRANSITIONED = "transitioned"


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    stage: str
    retryable: bool
    recovery: RecoveryAction
    public_message: str
    problems: list[str] = Field(default_factory=list)


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    type: Literal["metric_result"]


class DomainArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: ArtifactRef
    public_payload: dict[str, object]


class DomainResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: DomainStatus
    model_payload: dict[str, object]
    artifacts: list[DomainArtifact] = Field(default_factory=list)
    data_as_of: datetime | None = None
    coverage: Coverage | None = None
    error: ErrorEnvelope | None = None


class NewQueryRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: UUID
    user_message_id: UUID
    subject_id: str
    tool_call_id: str
    domain: Literal["business_query"] = "business_query"
    attempt_no: int = Field(ge=1)
    normalized_request: dict[str, object] = Field(default_factory=dict)
    state: dict[str, object] = Field(default_factory=dict)


class RunTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    node: str
    event_type: RunEventType = RunEventType.TRANSITIONED
    status: RunStatus
    state: dict[str, object]
    payload: dict[str, object] = Field(default_factory=dict)
    error_code: str | None = None


class NewArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_type: Literal["metric_result"] = "metric_result"
    payload: dict[str, object]
    data_as_of: datetime | None = None
    coverage: dict[str, object] | None = None


class RunCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    node: str
    status: RunStatus
    state: dict[str, object]
    payload: dict[str, object] = Field(default_factory=dict)
    error_code: str | None = None


class TurnContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: UUID
    user_message_id: UUID
    subject_id: str


class RunContextNotFound(Exception):
    def __init__(self) -> None:
        super().__init__("run_context_not_found")


class RunNotFound(Exception):
    def __init__(self) -> None:
        super().__init__("run_not_found")


class StaleRunRevision(Exception):
    def __init__(self) -> None:
        super().__init__("stale_run_revision")


class ArtifactPersistenceError(Exception):
    def __init__(self) -> None:
        super().__init__("artifact_persistence_error")


class QueryRunStore(Protocol):
    def create_run(self, record: NewQueryRun) -> UUID: ...

    def transition(self, run_id: UUID, transition: RunTransition) -> None: ...

    def save_artifact(self, run_id: UUID, artifact: NewArtifact) -> ArtifactRef: ...

    def finish(self, run_id: UUID, completion: RunCompletion) -> None: ...
