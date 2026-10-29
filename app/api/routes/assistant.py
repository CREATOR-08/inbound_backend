import logging

from fastapi import APIRouter, Depends, HTTPException

from app.agent.Assistant.graph import assistant_graph
from app.api.dependencies import AuthenticatedUser, get_current_user
from app.api.schemas.assistant import AssistantCommandRequest, AssistantResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"])


@router.post("/command", response_model=AssistantResponse)
def assistant_command(
    request: AssistantCommandRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AssistantResponse:
    """Run one user-scoped command through the compiled assistant graph."""
    try:
        result = assistant_graph.invoke(
            {
                "user_id": current_user.id,
                "user_account": {
                    "access_token": current_user.access_token,
                    "refresh_token": current_user.refresh_token,
                },
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
        )
        return AssistantResponse.model_validate(result.get("response"))
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("Assistant command failed for user_id=%s", current_user.id)
        raise HTTPException(
            status_code=500,
            detail="The assistant could not process your request.",
        ) from error
