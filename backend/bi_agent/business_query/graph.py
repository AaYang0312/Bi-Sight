"""Deterministic transitions for the business-query graph."""

from .state import BusinessQueryNode, BusinessQueryState


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
