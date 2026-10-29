from app.agent.Assistant.graph import assistant_graph


def test_ui_action():
    result = assistant_graph.invoke({
        "user_id": "test-user",
        "session_id": "test-session",
        "message": "Show my urgent emails",
        "page_context": {
            "page": "inbox",
            "active_filter": "ALL",
        },
    })

    print("\nUI ACTION RESULT")
    print(result["response"])


def test_question():
    result = assistant_graph.invoke({
        "user_id": "test-user",
        "session_id": "test-session",
        "message": "Summarize this email",
        "page_context": {
            "page": "email_detail",
            "selected_email_id": "email-123",
        },
    })

    print("\nQUESTION RESULT")
    print(result["response"])


def test_clarification():
    result = assistant_graph.invoke({
        "user_id": "test-user",
        "session_id": "test-session",
        "message": "Do something useful",
        "page_context": {},
    })

    print("\nCLARIFICATION RESULT")
    print(result["response"])


if __name__ == "__main__":
    test_ui_action()
    test_question()
    test_clarification()