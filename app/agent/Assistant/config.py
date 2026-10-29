import os

from langchain_google_genai import ChatGoogleGenerativeAI


api_key = "AQ.Ab8RN6K9Z5s-P_pD48l1SYEi3YN5hbJb8_dAt690AmpSmhDTWA"

if not api_key:
    raise RuntimeError(
        "GOOGLE_API_KEY is missing. "
        "Add it to your backend environment."
    )


llm = ChatGoogleGenerativeAI(
    model="gemini-3.8-flash",
    google_api_key=api_key,
    temperature=0,
)