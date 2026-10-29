import os
from dotenv import load_dotenv
load_dotenv()

from langchain_google_genai import ChatGoogleGenerativeAI



api_key=os.getenv("GOOGLE_API_KEY")

if not api_key:
    raise RuntimeError(
        "GOOGLE_API_KEY is missing. "
        "Add it to your backend environment."
    )


llm = ChatGoogleGenerativeAI(
    model="gemini-3.8-flash",
    google_api_key=api_key,
    temperature=0,
    max_retries=2,
    
)