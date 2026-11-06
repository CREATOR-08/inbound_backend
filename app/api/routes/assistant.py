import json
import logging
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.agent.Assistant.graph import assistant_graph
from app.api.dependencies import AuthenticatedUser, get_current_user
from app.api.schemas.assistant import AssistantCommandRequest, AssistantResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"])


def _assistant_input(
    request: AssistantCommandRequest,
    current_user: AuthenticatedUser,
) -> dict[str, Any]:
    return {
        "user_id": current_user.id,
        "session_id": request.session_id,
        "message": request.message,
        "page_context": request.page_context,
        "conversation_history": request.conversation_history,
        "query_context": (
            request.query_context.model_dump(mode="json")
            if request.query_context is not None
            else None
        ),
        "approved_workflow": (
            [step.model_dump(mode="json") for step in request.approved_workflow]
            if request.approved_workflow is not None
            else None
        ),
        "confirm_workflow": request.confirm_workflow,
    }


def _sse_event(event: str, payload: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _stream_assistant_response(input_state: dict[str, Any]) -> Iterator[str]:
    input_state["stream_response"] = True
    graph_stream = None
    latest_state: dict[str, Any] = {}
    sent_tokens = False
    try:
        graph_stream = assistant_graph.stream(
            input_state,
            stream_mode=["messages", "values"],
        )
        for mode, value in graph_stream:
            if mode == "messages":
                chunk, metadata = value
                if metadata.get("langgraph_node") != "answer_question":
                    continue
                content = getattr(chunk, "content", "")
                if isinstance(content, str) and content:
                    sent_tokens = True
                    yield _sse_event("token", content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            text = block["text"]
                            if text:
                                sent_tokens = True
                                yield _sse_event("token", text)
            elif mode == "values":
                latest_state = value

        response = latest_state.get("response")
        if not isinstance(response, dict) or not isinstance(response.get("message"), str):
            raise RuntimeError("Assistant graph returned no response.")
        if response.get("status") in {"error", "rate_limited"}:
            yield _sse_event("error", "The assistant could not process your request.")
            return
        if not sent_tokens:
            yield _sse_event("token", response["message"])
        yield _sse_event("done", response)
    except GeneratorExit:
        raise
    except Exception:
        logger.exception("Assistant streaming command failed")
        yield _sse_event("error", "The assistant could not process your request.")
    finally:
        close = getattr(graph_stream, "close", None)
        if close is not None:
            close()


@router.post("/command", response_model=AssistantResponse)
def assistant_command(
    request: AssistantCommandRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AssistantResponse:
    """Run one user-scoped command through the compiled assistant graph."""
    try:
        result = assistant_graph.invoke(_assistant_input(request, current_user))
        return AssistantResponse.model_validate(result.get("response"))
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("Assistant command failed for user_id=%s", current_user.id)
        raise HTTPException(
            status_code=500,
            detail="The assistant could not process your request.",
        ) from error


@router.post("/command/stream")
def assistant_command_stream(
    request: AssistantCommandRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """Run the same user-scoped assistant flow, streaming answer text as SSE."""
    return StreamingResponse(
        _stream_assistant_response(_assistant_input(request, current_user)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
