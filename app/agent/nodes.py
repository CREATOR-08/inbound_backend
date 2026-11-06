import os
from typing import Iterator, Optional
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
from app.services.calendar_extraction import (
    email_received_at_in_ist,
    resolve_calendar_event_draft,
)
from app.agent.prompt import SYSTEM_PROMPT
from langsmith import traceable
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
@traceable(name='triage_node',run_type='chain')
# Node 1: Triage & Classify
def triage_node(state: EmailAgentState) -> dict:
    from langchain_core.prompts import ChatPromptTemplate

    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            SYSTEM_PROMPT,  # Store the complete prompt above here
        ),
        (
            "human",
            """Classify the following email.

    <email>
    Subject: {subject}
    Sender: {sender}

    Body:
    {body}
    </email>

    Return the classification in the required JSON format."""
        ),
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

@traceable(name='draft_reply',run_type='chain')
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


@traceable(name='scheduler',run_type='chain')
def scheduler_node(
    state: EmailAgentState, email_received_at: Optional[str] = None
) -> dict:
    email_body = clean_text_for_llm(state["body"])
    if not email_body:
        raise ValueError("The email has no readable body to extract a meeting from.")

    received_at_ist = email_received_at_in_ist(email_received_at)
    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "Extract only meeting details explicitly supported by the email. "
            "Return the exact date phrase, start-time phrase, optional end-time "
            "phrase, optional duration phrase, and optional explicit timezone "
            "phrase as written; do not calculate dates or convert timezones. "
            "Do not infer AM/PM, a duration, an end time, a date, or a year. "
            "Leave a field empty if it is missing or unclear. The received-at "
            "timestamp below is already converted to Asia/Kolkata and is the "
            "only reference date for interpreting relative-date wording later.",
        ),
        (
            "human",
            "Email received at (Asia/Kolkata): {email_received_at}\n"
            "Sender: {sender}\nSubject: {subject}\nBody:\n{body}",
        ),
    ])

    structured_llm = llm.with_structured_output(CalendarEventDraft)
    chain = prompt | structured_llm

    result: CalendarEventDraft = chain.invoke({
        "sender": state["sender"],
        "subject": state["subject"],
        "body": email_body,
        "email_received_at": received_at_ist or "unavailable",
    })

    return {
        "calendar_event": resolve_calendar_event_draft(
            result.model_dump(),
            source_text=f"{state['subject']}\n{email_body}",
            received_at=email_received_at,
        )
    }