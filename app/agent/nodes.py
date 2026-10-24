import os
from langchain_groq import ChatGroq  # Swap to ChatOpenAI if using OpenAI
from langchain_core.prompts import ChatPromptTemplate
from app.agent.state import EmailAgentState, TriageOutput, CalendarEventDraft, ReplyDraftOutput

# Initialize LLM (Ensure GROQ_API_KEY or OPENAI_API_KEY is in your environment)
llm = ChatGroq(model_name="llama-3.3-70b-versatile", temperature=0.1)

# Node 1: Triage & Classify
def triage_node(state: EmailAgentState) -> dict:
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are an executive email triage assistant. Classify the email category and urgency."),
        ("human", "Subject: {subject}\nSender: {sender}\nBody:\n{body}")
    ])
    
    structured_llm = llm.with_structured_output(TriageOutput)
    chain = prompt | structured_llm
    
    result: TriageOutput = chain.invoke({
        "subject": state["subject"],
        "sender": state["sender"],
        "body": state["body"]
    })
    
    # If it's spam, mark it as archived immediately
    is_archived = result.category == "SPAM"
    
    return {
        "category": result.category,
        "priority": result.priority,
        "is_archived": is_archived
    }

# Node 2: Reply Drafter
def drafter_node(state: EmailAgentState) -> dict:
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a professional assistant drafting email replies. Provide an outline and a full email draft."),
        ("human", "Sender: {sender}\nSubject: {subject}\nEmail Body:\n{body}")
    ])
    
    structured_llm = llm.with_structured_output(ReplyDraftOutput)
    chain = prompt | structured_llm
    
    result: ReplyDraftOutput = chain.invoke({
        "sender": state["sender"],
        "subject": state["subject"],
        "body": state["body"]
    })
    
    return {
        "reply_outline": result.reply_outline,
        "suggested_reply": result.suggested_reply
    }

# Node 3: Calendar Extractor
def scheduler_node(state: EmailAgentState) -> dict:
    prompt = ChatPromptTemplate.from_messages([
        ("system", "Extract meeting details from this email. Convert relative dates to concrete ISO 8601 timestamps using timezone +05:30. Current time reference: October 2026."),
        ("human", "Sender: {sender}\nSubject: {subject}\nBody:\n{body}")
    ])
    
    structured_llm = llm.with_structured_output(CalendarEventDraft)
    chain = prompt | structured_llm
    
    try:
        result: CalendarEventDraft = chain.invoke({
            "sender": state["sender"],
            "subject": state["subject"],
            "body": state["body"]
        })
        calendar_dict = result.model_dump()
    except Exception:
        calendar_dict = None
    
    return {"calendar_event": calendar_dict}