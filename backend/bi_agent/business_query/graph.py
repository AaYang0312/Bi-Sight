"""Deterministic transitions and execution for the business-query graph."""

from __future__ import annotations

import contextlib

from bi_agent.runtime.models import (
    DomainArtifact,
    DomainResult,
    DomainStatus,
    ErrorEnvelope,
    NewQueryRun,
    RecoveryAction,
    RunCompletion,
    RunStatus,
    RunTransition,
)

from .state import (
    BusinessQueryContext,
    BusinessQueryExecution,
    BusinessQueryInput,
    BusinessQueryNode,
    BusinessQueryRuntime,
    BusinessQueryState,
)


_NEXT_NODE = {
    BusinessQueryNode.RECEIVED: BusinessQueryNode.RESOLVE_PARAMETERS,
    BusinessQueryNode.RESOLVE_PARAMETERS: BusinessQueryNode.VALIDATE_PARAMETERS,
    BusinessQueryNode.VALIDATE_PARAMETERS: BusinessQueryNode.AUTHORIZE_SCOPE,
    BusinessQueryNode.AUTHORIZE_SCOPE: BusinessQueryNode.EXECUTE_FIXED_QUERY,
    BusinessQueryNode.EXECUTE_FIXED_QUERY: BusinessQueryNode.CLASSIFY_RESULT,
    BusinessQueryNode.CLASSIFY_RESULT: BusinessQueryNode.PERSIST_ARTIFACT,
    BusinessQueryNode.PERSIST_ARTIFACT: BusinessQueryNode.FINALIZE,
}


class InvalidBusinessQueryTransition(Exception):
    def __init__(self) -> None:
        super().__init__("invalid_transition")


def transition_state(
    state: BusinessQueryState, next_node: BusinessQueryNode
) -> BusinessQueryState:
    """Advance exactly one allowlisted step without mutating the source state."""
    if _NEXT_NODE.get(state.node) != next_node:
        raise InvalidBusinessQueryTransition()
    return state.model_copy(update={"node": next_node})


def _execute_business_query_graph(
    conn: object,
    store: object,
    tool_input: BusinessQueryInput,
    context: BusinessQueryContext,
) -> BusinessQueryExecution:
    """Run the fixed, allowlisted business-query graph once.

    This internal runner intentionally has no public adapter yet; Task 7 owns
    routing it into the Agent and chat persistence boundary.

    Any unexpected failure is terminated best-effort before it is re-raised:
    a half-dead run must never stay ``running`` forever, because the unique
    ``(user_message_id, domain, attempt_no)`` context blocks a same-attempt
    replay and the session has no reaper.
    """
    run_id = store.create_run(  # type: ignore[attr-defined]
        NewQueryRun(
            chat_id=context.chat_id,
            user_message_id=context.user_message_id,
            subject_id=context.subject_id,
            tool_call_id=tool_input.tool_call_id,
            attempt_no=context.attempt_no,
            normalized_request={},
            state={
                "node": BusinessQueryNode.RECEIVED.value,
                "status": RunStatus.RUNNING.value,
                "revision": 0,
            },
        )
    )
    runtime = BusinessQueryRuntime(
        state=BusinessQueryState(run_id=run_id),
        context=context,
        resolved_args=dict(tool_input.arguments or {}),
    )
    try:
        return _run_graph_nodes(runtime, store, conn, tool_input)
    except Exception:  # noqa: BLE001 - 收尾后原样上抛，由外层做脱敏
        _finish_run_as_failed(runtime, store)
        raise


def _run_graph_nodes(
    runtime: BusinessQueryRuntime,
    store: object,
    conn: object,
    tool_input: BusinessQueryInput,
) -> BusinessQueryExecution:
    """Advance the fixed node sequence for an already-created run."""
    from .nodes import (
        _event_payload,
        authorize_scope,
        classify_result,
        execute_fixed_query,
        finalize_run,
        persist_artifact,
        resolve_parameters,
        validate_parameters,
    )

    if tool_input.arguments_error is not None:
        runtime.state = transition_state(
            runtime.state, BusinessQueryNode.RESOLVE_PARAMETERS
        )
        runtime.state = runtime.state.model_copy(
            update={
                "status": RunStatus.NEEDS_INPUT,
                "target_status": DomainStatus.NEEDS_INPUT,
                "problems": ["invalid_parameters"],
                "error": ErrorEnvelope(
                    code="invalid_parameters",
                    stage="resolve_parameters",
                    retryable=False,
                    recovery=RecoveryAction.CORRECT_PARAMETERS,
                    public_message="查询参数无效，请调整后重试。",
                    problems=["invalid_parameters"],
                ),
            }
        )
        _persist_transition(runtime, store, _event_payload(runtime))
        _finish_early(runtime, store, _event_payload(runtime))
        return _execution_result(runtime)

    for node in (resolve_parameters, validate_parameters):
        node(runtime)
        _persist_transition(runtime, store, _event_payload(runtime))
        if runtime.state.status is not RunStatus.RUNNING:
            _finish_early(runtime, store, _event_payload(runtime))
            return _execution_result(runtime)

    authorize_scope(runtime, advance_to_execution=False)
    _persist_transition(runtime, store, _event_payload(runtime))
    if runtime.state.status is not RunStatus.RUNNING:
        _finish_early(runtime, store, _event_payload(runtime))
        return _execution_result(runtime)

    execute_fixed_query(runtime, conn)
    _persist_transition(runtime, store, _event_payload(runtime))
    if runtime.state.status is not RunStatus.RUNNING:
        _finish_early(runtime, store, _event_payload(runtime))
        return _execution_result(runtime)

    classify_result(runtime)
    _persist_transition(runtime, store, _event_payload(runtime))
    if runtime.state.status is not RunStatus.RUNNING:
        _finish_early(runtime, store, _event_payload(runtime))
        return _execution_result(runtime)

    persist_artifact(runtime, store)
    _persist_transition(runtime, store, _event_payload(runtime))
    finalize_run(runtime, store)
    return _execution_result(runtime)


def _finish_run_as_failed(
    runtime: BusinessQueryRuntime, store: object
) -> None:
    """Best-effort FAILED completion for a graph that died mid-flight.

    The in-memory revision mirror only advances after a store write succeeds, so
    it normally still matches the store; if it does not, the rejected write is
    suppressed rather than replacing the exception that is already in flight.
    """
    state = runtime.state
    error = state.error or ErrorEnvelope(
        code="unavailable",
        stage=state.node.value,
        retryable=True,
        recovery=RecoveryAction.RETRY_LATER,
        public_message="查询暂不可用，请稍后重试。",
    )
    with contextlib.suppress(Exception):
        store.finish(  # type: ignore[attr-defined]
            state.run_id,
            RunCompletion(
                expected_revision=state.revision,
                node=state.node.value,
                status=RunStatus.FAILED,
                state={
                    **state.model_dump(mode="json"),
                    "status": RunStatus.FAILED.value,
                    "target_status": DomainStatus.FAILED.value,
                    "error": error.model_dump(mode="json"),
                },
                payload={},
                error_code="unavailable",
            ),
        )


def _persist_transition(
    runtime: BusinessQueryRuntime, store: object, payload: dict[str, object]
) -> None:
    """Persist one completed node and mirror the Store revision in memory."""
    previous = runtime.state
    persisted = previous.model_copy(update={"revision": previous.revision + 1})
    store.transition(  # type: ignore[attr-defined]
        persisted.run_id,
        RunTransition(
            expected_revision=previous.revision,
            node=persisted.node.value,
            status=persisted.status,
            state=persisted.model_dump(mode="json"),
            payload=payload,
            error_code=persisted.error.code if persisted.error else None,
        ),
    )
    runtime.state = persisted


def _finish_early(
    runtime: BusinessQueryRuntime, store: object, payload: dict[str, object]
) -> None:
    """Finish a terminal pre-query path without running later graph nodes."""
    previous = runtime.state
    finished = previous.model_copy(update={"revision": previous.revision + 1})
    store.finish(  # type: ignore[attr-defined]
        finished.run_id,
        RunCompletion(
            expected_revision=previous.revision,
            node=finished.node.value,
            status=finished.status,
            state=finished.model_dump(mode="json"),
            payload=payload,
            error_code=finished.error.code if finished.error else None,
        ),
    )
    runtime.state = finished


def _execution_result(runtime: BusinessQueryRuntime) -> BusinessQueryExecution:
    from .tool import to_model_result, to_public_artifact

    state = runtime.state
    status = state.target_status or DomainStatus.FAILED
    output_is_safe = not (
        state.error is not None
        and state.error.code in {"artifact_persistence_failed", "result_contract_violation"}
    )
    model_payload: dict[str, object]
    artifacts: list[DomainArtifact] = []
    if runtime.result is not None and output_is_safe:
        try:
            if runtime.catalog is None:
                # 没建立目录就无法把主键换成引用：与投影失败同样关闭三个出口。
                raise ValueError("catalog_not_built")
            model_payload = to_model_result(runtime.result, runtime.catalog)
            public_payload = to_public_artifact(runtime.result, runtime.catalog)
            artifacts = [
                DomainArtifact(ref=ref, public_payload=public_payload)
                for ref in state.artifact_refs
            ]
        except ValueError:
            # 投影失败说明结果不符合安全契约：三个出口一律关闭，被拒数字不得回流。
            status = DomainStatus.FAILED
            model_payload = {"status": "failed"}
            artifacts = []
            output_is_safe = False
    else:
        model_payload = {
            "status": (
                "needs_input" if status is DomainStatus.NEEDS_INPUT else "failed"
            )
        }

    return BusinessQueryExecution(
        domain_result=DomainResult(
            run_id=state.run_id,
            status=status,
            model_payload=model_payload,
            artifacts=artifacts,
            data_as_of=state.data_as_of,
            coverage=state.coverage,
            error=state.error,
        ),
        tool_result=runtime.result if output_is_safe else None,
        session_filters=_session_filters(runtime) if output_is_safe else {},
    )


def _session_filters(runtime: BusinessQueryRuntime) -> dict[str, object]:
    request = runtime.request
    if request is None:
        return {}
    return {
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "shop_ids": list(request.shop_ids),
        "metrics": list(request.metrics),
        "group_by": request.group_by,
        "compare": request.compare,
        "top_n": request.top_n,
        "currency": request.currency,
    }
