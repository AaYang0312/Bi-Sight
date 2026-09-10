"""Safe persisted state and in-process data for business-query runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from bi_agent.metrics import Coverage, QueryRequest, ToolResult
from bi_agent.runtime.models import (
    ArtifactRef,
    DomainResult,
    DomainStatus,
    ErrorEnvelope,
    ProblemCode,
    RunStatus,
    validate_persisted_state,
)


class BusinessQueryNode(StrEnum):
    RECEIVED = "received"
    RESOLVE_PARAMETERS = "resolve_parameters"
    VALIDATE_PARAMETERS = "validate_parameters"
    AUTHORIZE_SCOPE = "authorize_scope"
    EXECUTE_FIXED_QUERY = "execute_fixed_query"
    CLASSIFY_RESULT = "classify_result"
    PERSIST_ARTIFACT = "persist_artifact"
    FINALIZE = "finalize"


class BusinessQueryInput(BaseModel):
    """One model tool call, including a JSON parse failure when present."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    tool_call_id: str
    arguments: dict[str, object] | None
    arguments_error: str | None = None


class BusinessQueryState(BaseModel):
    """The complete allowlisted snapshot permitted to cross the Store boundary."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    run_id: UUID
    node: BusinessQueryNode = BusinessQueryNode.RECEIVED
    status: RunStatus = RunStatus.RUNNING
    revision: int = Field(default=0, ge=0)
    normalized_request: dict[str, object] = Field(default_factory=dict)
    problems: list[ProblemCode] = Field(default_factory=list)
    tool_status: str | None = None
    target_status: DomainStatus | None = None
    coverage: Coverage | None = None
    data_as_of: datetime | None = None
    limitations: list[str] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    error: ErrorEnvelope | None = None

    def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
        """Validate JSON snapshots with the shared persisted-state allowlist."""
        dumped = super().model_dump(*args, **kwargs)
        if kwargs.get("mode") == "json":
            return validate_persisted_state(dumped)
        return dumped


@dataclass
class BusinessQueryContext:
    """Request-scoped data that can contain real identifiers and raw text."""

    chat_id: UUID
    user_message_id: UUID
    subject_id: str
    question: str
    previous_filters: dict[str, object]
    shop_aliases: dict[str, str]
    allowed_shop_ids: frozenset[str]
    now: datetime
    deadline: datetime
    attempt_no: int


@dataclass
class BusinessQueryRuntime:
    """Mutable graph inputs and results which must remain process-local."""

    state: BusinessQueryState
    context: BusinessQueryContext
    resolved_args: dict[str, object] = field(default_factory=dict)
    request: QueryRequest | None = None
    result: ToolResult | None = None


@dataclass
class BusinessQueryExecution:
    """Internal compatibility result; it is never persisted directly."""

    domain_result: DomainResult
    tool_result: ToolResult | None = None
    session_filters: dict[str, object] = field(default_factory=dict)
