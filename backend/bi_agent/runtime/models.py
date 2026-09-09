"""Public models and store protocol for deterministic query runs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import math
import re
from collections.abc import Collection
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from bi_agent.metrics import Coverage


_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "analysis",
    "chainofthought",
    "databaseerror",
    "dberror",
    "diagnostic",
    "diagnostics",
    "error_detail",
    "error_hint",
    "errordetail",
    "errorhint",
    "errormessage",
    "exception",
    "exceptionmessage",
    "hiddenreasoning",
    "modelreasoning",
    "originalquestion",
    "prompt",
    "question",
    "rawquestion",
    "reasoning",
    "scratchpad",
    "sql",
    "sqlstate",
    "stacktrace",
    "thoughts",
    "traceback",
    "userquestion",
})
_FORBIDDEN_KEY_FRAGMENTS = (
    "apikey",
    "authorization",
    "connectionstring",
    "credential",
    "cookie",
    "dsn",
    "password",
    "privatekey",
    "secret",
    "token",
)
_UNSAFE_TEXT_PATTERNS = (
    re.compile(r"\b(?:psycopg|postgres(?:ql)?|sqlstate|traceback)\b", re.IGNORECASE),
    re.compile(r"\b(?:password|passwd|secret|token|credential|authorization|bearer|cookie)\b", re.IGNORECASE),
    re.compile(r"(?:postgres(?:ql)?|[a-z][a-z0-9+.-]*)://[^\s]+@", re.IGNORECASE),
    re.compile(r"\b(?:connection refused|permission denied|undefinedtable)\b", re.IGNORECASE),
    re.compile(r"\b(?:select|insert|update|delete)\s+", re.IGNORECASE),
)


def validate_safe_payload(
    value: object, *, forbidden_values: Collection[str] = (),
) -> dict[str, object]:
    """Validate that a JSON payload is safe to persist without leaking its value."""
    if not isinstance(value, dict):
        raise ValueError("unsafe_persistence_payload")
    _validate_safe_value(value, frozenset(item for item in forbidden_values if item))
    return value


def _validate_safe_value(value: object, forbidden_values: frozenset[str]) -> None:
    if isinstance(value, dict):
        for key, nested_value in value.items():
            if not isinstance(key, str) or _is_forbidden_key(key):
                raise ValueError("unsafe_persistence_payload")
            _validate_safe_value(nested_value, forbidden_values)
        return
    if isinstance(value, list):
        for item in value:
            _validate_safe_value(item, forbidden_values)
        return
    if isinstance(value, str):
        if value in forbidden_values or _contains_unsafe_text(value):
            raise ValueError("unsafe_persistence_payload")
        return
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    raise ValueError("unsafe_persistence_payload")


def _is_forbidden_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return (
        normalized in _FORBIDDEN_PAYLOAD_KEYS
        or any(fragment in normalized for fragment in _FORBIDDEN_KEY_FRAGMENTS)
    )


def _contains_unsafe_text(value: str) -> bool:
    return any(pattern.search(value) for pattern in _UNSAFE_TEXT_PATTERNS)


def _validate_safe_identifier(value: str) -> str:
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError("unsafe_persistence_identifier")
    return value


def _validate_public_text(value: str) -> str:
    if _contains_unsafe_text(value):
        raise ValueError("unsafe_public_error_text")
    return value


SafePayload = Annotated[dict[str, object], BeforeValidator(validate_safe_payload)]


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
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    code: str
    stage: str
    retryable: bool
    recovery: RecoveryAction
    public_message: str
    problems: list[str] = Field(default_factory=list)

    @field_validator("code", "stage")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _validate_safe_identifier(value)

    @field_validator("public_message")
    @classmethod
    def validate_public_message(cls, value: str) -> str:
        return _validate_public_text(value)

    @field_validator("problems")
    @classmethod
    def validate_problems(cls, value: list[str]) -> list[str]:
        return [_validate_public_text(problem) for problem in value]


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: UUID
    type: Literal["metric_result"]


class DomainArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    ref: ArtifactRef
    public_payload: SafePayload


class DomainResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    run_id: UUID
    status: DomainStatus
    model_payload: SafePayload
    artifacts: list[DomainArtifact] = Field(default_factory=list)
    data_as_of: datetime | None = None
    coverage: Coverage | None = None
    error: ErrorEnvelope | None = None


class NewQueryRun(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    chat_id: UUID
    user_message_id: UUID
    subject_id: str
    tool_call_id: str
    domain: Literal["business_query"] = "business_query"
    attempt_no: int = Field(ge=1)
    normalized_request: SafePayload = Field(default_factory=dict)
    state: SafePayload = Field(default_factory=dict)


class RunTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_revision: int = Field(ge=0)
    node: str
    event_type: RunEventType = RunEventType.TRANSITIONED
    status: RunStatus
    state: SafePayload
    payload: SafePayload = Field(default_factory=dict)
    error_code: str | None = None

    @field_validator("node", "error_code")
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        return _validate_safe_identifier(value) if value is not None else value


class NewArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    artifact_type: Literal["metric_result"] = "metric_result"
    payload: SafePayload
    data_as_of: datetime | None = None
    coverage: SafePayload | None = None


class RunCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_revision: int = Field(ge=0)
    node: str
    status: RunStatus
    state: SafePayload
    payload: SafePayload = Field(default_factory=dict)
    error_code: str | None = None

    @field_validator("node", "error_code")
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        return _validate_safe_identifier(value) if value is not None else value


class TurnContext(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

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
    def __init__(self, _reason: str | None = None) -> None:
        super().__init__("artifact_persistence_error")


class QueryRunStore(Protocol):
    def create_run(self, record: NewQueryRun) -> UUID: ...

    def transition(self, run_id: UUID, transition: RunTransition) -> None: ...

    def save_artifact(self, run_id: UUID, artifact: NewArtifact) -> ArtifactRef: ...

    def finish(self, run_id: UUID, completion: RunCompletion) -> None: ...
