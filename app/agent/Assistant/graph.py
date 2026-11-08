"""LangGraph orchestration for Inbound AI's conversational assistant."""

from langgraph.graph import END, START, StateGraph

from app.agent.Assistant.nodes import (
    answer_question,
    classify_intent,
    clarify_request,
    execute_tools,
    fetch_context,
    load_session_context,
    plan_actions,
    plan_data_query,
    plan_workflow,
    receive_input,
    validate_response,
)
from app.agent.Assistant.state import AssistantState


def route_after_input(state: AssistantState) -> str:
    if state.get("error"):
        return "validate_response"
    if state.get("approved_workflow") and state.get("confirm_workflow"):
        return "execute_tools"
    return "load_session_context"


def route_after_classification(state: AssistantState) -> str:
    if state.get("error"):
        return "validate_response"
    if state.get("multi_step"):
        return "plan_workflow"
    task_intent = state.get("task_intent")
    if task_intent == "SUMMARIZE_EMAIL":
        return "fetch_context"
    if task_intent == "GENERATE_REPLY":
        return "fetch_context"
    if task_intent == "SCHEDULE_EVENT":
        return "plan_workflow"
    return {
        "UI_ACTION": "plan_actions",
        "DATA_QUERY": "plan_data_query",
        "QUESTION": "fetch_context",
        "WORKFLOW": "plan_workflow",
        "CLARIFICATION": "clarify_request",
    }.get(state.get("intent", "CLARIFICATION"), "clarify_request")


def build_assistant_graph():
    graph = StateGraph(AssistantState)

    graph.add_node("receive_input", receive_input)
    graph.add_node("load_session_context", load_session_context)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("plan_data_query", plan_data_query)
    graph.add_node("plan_actions", plan_actions)
    graph.add_node("fetch_context", fetch_context)
    graph.add_node("answer_question", answer_question)
    graph.add_node("plan_workflow", plan_workflow)
    graph.add_node("execute_tools", execute_tools)
    graph.add_node("clarify_request", clarify_request)
    graph.add_node("validate_response", validate_response)

    graph.add_edge(START, "receive_input")
    graph.add_conditional_edges(
        "receive_input",
        route_after_input,
        {
            "load_session_context": "load_session_context",
            "execute_tools": "execute_tools",
            "validate_response": "validate_response",
        },
    )
    graph.add_edge("load_session_context", "classify_intent")
    graph.add_conditional_edges(
        "classify_intent",
        route_after_classification,
        {
            "plan_actions": "plan_actions",
            "plan_data_query": "plan_data_query",
            "fetch_context": "fetch_context",
            "plan_workflow": "plan_workflow",
            "clarify_request": "clarify_request",
            "validate_response": "validate_response",
        },
    )

    graph.add_edge("plan_actions", "validate_response")
    graph.add_edge("plan_data_query", "validate_response")
    graph.add_edge("fetch_context", "answer_question")
    graph.add_edge("answer_question", "validate_response")
    graph.add_conditional_edges(
        "plan_workflow",
        lambda state: "execute_tools" if state.get("execute_workflow") else "validate_response",
        {"execute_tools": "execute_tools", "validate_response": "validate_response"},
    )
    graph.add_edge("execute_tools", "validate_response")
    graph.add_edge("clarify_request", "validate_response")
    graph.add_edge("validate_response", END)

    return graph.compile()


assistant_graph = build_assistant_graph()
