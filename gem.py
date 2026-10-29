from app.agent.Assistant.config import llm

try:
    print("Testing Gemini connection...")

    response = llm.invoke(
        "Reply with exactly these two words: Gemini works"
    )

    print("\nGemini response:")
    print(response.content)

except Exception as error:
    print("\nGemini test failed:")
    print(f"Error type: {type(error).__name__}")
    print(f"Error: {error}")