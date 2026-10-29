
from pprint import pprint

from app.agent.Assistant.graph import assistant_graph


def test_assistant(message: str) -> None:
    print("\n" + "=" * 60)
    print(f"USER: {message}")
    print("=" * 60)

    result = assistant_graph.invoke({
        "user_id": "test-user",
        "session_id": "test-session-001",
        "message": message,
        "page_context": {
            "page": "inbox",
            "active_filter": "ALL",
            "selected_email_id": None,
        },
        "conversation_history": [],
    })

    print("\nASSISTANT RESPONSE:")
    pprint(result.get("response"))

    print("\nCLASSIFIED INTENT:")
    print(result.get("intent"))

    print("\nACTION PLAN:")
    pprint(result.get("action_plan"))


if __name__ == "__main__":


    test_assistant("Find emails from indeed")
