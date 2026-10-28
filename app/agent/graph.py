from langgraph.graph import StateGraph, END
from app.agent.state import EmailAgentState
from app.agent.nodes import triage_node

# 1. Initialize StateGraph
builder = StateGraph(EmailAgentState)

# Ingestion deliberately ends after triage. Reply and calendar generation are
# separate actions initiated by the user.
builder.add_node("triage_node", triage_node)
builder.set_entry_point("triage_node")
builder.add_edge("triage_node", END)
triage_graph = builder.compile()

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

    result = triage_graph.invoke(mock_email)
    import json
    print(json.dumps(result, indent=2))