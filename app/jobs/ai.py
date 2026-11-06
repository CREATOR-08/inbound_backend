import logging

from app.jobs.reliability import log_job_failure

logger = logging.getLogger(__name__)


def tag_recent_untagged_emails_job(user_id: str, limit: int = 10) -> None:
    from app.api.routes.emails import tag_recent_untagged_emails

    try:
        tag_recent_untagged_emails(user_id, limit)
    except Exception as error:
        log_job_failure(
            logger,
            "tag_recent_untagged_emails",
            error,
            user_id=user_id,
        )
        raise
