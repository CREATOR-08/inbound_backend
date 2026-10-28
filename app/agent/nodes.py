import os
from typing import Iterator
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from app.agent.state import (
    EmailAgentState,
    TriageOutput,
    CalendarEventDraft,
    ReplyDraftOutput,
)
from app.services.gmail import clean_text_for_llm
load_dotenv()
api_key = os.getenv("GOOGLE_API_KEY")
if not api_key:
    raise ValueError("GOOGLE_API_KEY not found in environment variables. Check your .env file.")
# Initialize Gemini Model
# Ensure GOOGLE_API_KEY is present in your environment / .env
llm = ChatGoogleGenerativeAI(
    # model="gemini-2.5-flash",
    model="gemini-3.5-flash-lite",
    google_api_key=api_key,
    temperature=0.1,
)
llm2=ChatGoogleGenerativeAI(
    model="gemini-3.5-flash-lite",
        google_api_key=api_key,
        temperature=0.1,

)

# Node 1: Triage & Classify
def triage_node(state: EmailAgentState) -> dict:
    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "You are an executive email triage assistant. Classify the email category and urgency accurately.",
        ),
        ("human", "Subject: {subject}\nSender: {sender}\nBody:\n{body}"),
    ])

    structured_llm = llm.with_structured_output(TriageOutput)
    chain = prompt | structured_llm

    result: TriageOutput = chain.invoke({
        "subject": state["subject"],
        "sender": state["sender"],
        "body": state["body"],
    })

    # Direct spam emails to be archived immediately
    is_archived = result.category == "SPAM"

    return {
        "category": result.category,
        "priority": result.priority,
        "is_archived": is_archived,
    }

# # Node 2: Reply Drafter
# def drafter_node(state: EmailAgentState) -> dict:
#     prompt = ChatPromptTemplate.from_messages([
#         (
#             "system",
#             "You are a email assistant. "
#             "Draft a email response for the email received in a positive manner ",
#         ),
#         ("human", "Sender: {sender}\nSubject: {subject}\nEmail Body:\n{body_html}"),
#     ])

#     structured_llm = llm.with_structured_output(ReplyDraftOutput)
#     chain = prompt | structured_llm

#     result: ReplyDraftOutput = chain.invoke({
#         "sender": state["sender"],
#         "subject": state["subject"],
#         "body": state["body"],
#     })

#     return {
#         "reply_outline": result.reply_outline,
#         "suggested_reply": result.suggested_reply,
#     }


def stream_draft_reply(state: EmailAgentState) -> Iterator[str]:
    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "Write a concise, professional email reply. Output only the reply text.",
        ),
        ("human", "Sender: {sender}\nSubject: {subject}\nEmail Body:\n{body}"),
    ])
    chain = prompt | llm2

    for chunk in chain.stream({
        "sender": state["sender"],
        "subject": state["subject"],
        "body": state["body"],
    }):
        content = chunk.content
        if isinstance(content, str):
            if content:
                yield content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    yield block["text"]


# Node 3: Calendar Extractor
def scheduler_node(state: EmailAgentState) -> dict:
    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "Extract meeting schedule details from this email. "
            "Convert relative dates (e.g., 'this Monday at 4 PM') to valid ISO 8601 strings with timezone +05:30. "
            "Base reference year is 2026. If the email doesn't contain a specific time proposal, return confidence 0.0.",
        ),
        ("human", "Sender: {sender}\nSubject: {subject}\nBody:\n{body}"),
    ])

    structured_llm = llm.with_structured_output(CalendarEventDraft)
    chain = prompt | structured_llm

    result: CalendarEventDraft = chain.invoke({
        "sender": state["sender"],
        "subject": state["subject"],
        "body": state["body"],
    })

    return {"calendar_event": result.model_dump()}