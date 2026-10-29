from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agent.Assistant.graph import assistant_graph
from app.api.dependencies import get_current_user
from app.db.models import UserAccount

router = APIRouter(
    prefix="/assistant",
    tags=["assistant"],
)


class AssistantCommandRequest(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    session_id: str = Field(min_length=1, max_length=200)
    page_context: dict[str, Any] = Field(default_factory=dict)
    conversation_history: list[dict[str, str]] = Field(
        default_factory=list
    )


@router.post("/command")
def assistant_command(
    request: AssistantCommandRequest,
    current_user: UserAccount = Depends(get_current_user),
):
    message = request.message.strip()

    if not message:
        raise HTTPException(
            status_code=422,
            detail="Message cannot be empty.",
        )

    try:
        result = assistant_graph.invoke({
            "user_id": current_user.id,
            "session_id": request.session_id,
            "message": message,
            "page_context": request.page_context,
            "conversation_history": request.conversation_history,
        })

        response = result.get("response")

        if not isinstance(response, dict):
            raise HTTPException(
                status_code=500,
                detail="The assistant returned an invalid response.",
            )

        return response

    except HTTPException:
        raise
    except Exception as exc:
        # Log the exception server-side in production.
        raise HTTPException(
            status_code=500,
            detail="The assistant could not process your request.",
        ) from exc