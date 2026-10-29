from langgraph.graph import StateGraph, START, END

from app.agent.Assistant.state import AssistantState

from app.agent.Assistant.nodes import (
    receive_input,
    load_session_context,
    classify_intent,
    plan_actions,
    fetch_context,
    answer_question,
    plan_workflow,
    execute_tools,
    clarify_request,
    validate_response,
)

def route_after_input(state: AssistantState) -> str:
    """Skip processing if the incoming message is invalid."""

    if state.get("error"):
        return "validate_response"

    return "load_session_context"


def route_by_intent(state: AssistantState) -> str:
    """Choose the execution path based on the classified intent."""

    intent = state.get("intent")

    if intent == "UI_ACTION":
        return "plan_actions"

    if intent == "QUESTION":
        return "fetch_context"

    if intent == "WORKFLOW":
        return "plan_workflow"

    return "clarify_request"


def build_assistant_graph():
    graph = StateGraph(AssistantState)

    # Register nodes
    graph.add_node("receive_input", receive_input)
    graph.add_node("load_session_context", load_session_context)
    graph.add_node("classify_intent", classify_intent)

    graph.add_node("plan_actions", plan_actions)
    graph.add_node("fetch_context", fetch_context)
    graph.add_node("answer_question", answer_question)

    graph.add_node("plan_workflow", plan_workflow)
    graph.add_node("execute_tools", execute_tools)

    graph.add_node("clarify_request", clarify_request)
    graph.add_node("validate_response", validate_response)

    # Entry point
    graph.add_edge(START, "receive_input")

    # Validate incoming message
    graph.add_conditional_edges(
        "receive_input",
        route_after_input,
        {
            "load_session_context": "load_session_context",
            "validate_response": "validate_response",
        },
    )

    # Shared processing
    graph.add_edge(
        "load_session_context",
        "classify_intent",
    )

    # Conditional routing
    graph.add_conditional_edges(
        "classify_intent",
        route_by_intent,
        {
            "plan_actions": "plan_actions",
            "fetch_context": "fetch_context",
            "plan_workflow": "plan_workflow",
            "clarify_request": "clarify_request",
        },
    )

    # UI action path
    graph.add_edge("plan_actions", "validate_response")

    # Question-answering path
    graph.add_edge("fetch_context", "answer_question")
    graph.add_edge("answer_question", "validate_response")

    # Multi-step workflow path
    graph.add_edge("plan_workflow", "execute_tools")
    graph.add_edge("execute_tools", "validate_response")

    # Clarification path
    graph.add_edge("clarify_request", "validate_response")

    # Exit
    graph.add_edge("validate_response", END)

    return graph.compile()


assistant_graph = build_assistant_graph()