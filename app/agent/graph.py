from langgraph.graph import StateGraph, END
from app.agent.state import EmailAgentState
from app.agent.nodes import triage_node, drafter_node, scheduler_node

def route_triage(state: EmailAgentState) -> str:
    if state.get("is_archived"):
        return END
    if state.get("category") == "MEETING_REQUEST":
        return "scheduler_node"
    return "drafter_node"

# 1. Initialize StateGraph
builder = StateGraph(EmailAgentState)

# 2. Add Nodes
builder.add_node("triage_node", triage_node)
builder.add_node("scheduler_node", scheduler_node)
builder.add_node("drafter_node", drafter_node)

# 3. Define Entrypoint and Edges
builder.set_entry_point("triage_node")

# Conditional routing out of triage
builder.add_conditional_edges(
    "triage_node",
    route_triage,
    {
        END: END,
        "scheduler_node": "scheduler_node",
        "drafter_node": "drafter_node"
    }
)

# If meeting was extracted, move on to drafting the response
builder.add_edge("scheduler_node", "drafter_node")
builder.add_edge("drafter_node", END)

# 4. Compile workflow
email_graph = builder.compile()




if __name__ == "__main__":
    mock_email: EmailAgentState = {
        "email_id": "test_101",
        "sender": "hr@google.com",
        "subject": "Interview Availability: SDE Intern Round 1",
        "body": "Hi, are you available for a 45-minute technical round on Monday at 3 PM IST?",
        "category": None,
        "priority": None,
        "reply_outline": None,
        "suggested_reply": None,
        "calendar_event": None,
        "is_archived": False
    }

    result = email_graph.invoke(mock_email)
    import json
    print(json.dumps(result, indent=2))