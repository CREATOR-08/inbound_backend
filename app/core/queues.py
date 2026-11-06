from collections.abc import Callable
from typing import Any

from redis.exceptions import RedisError
from rq import Queue, Retry
from rq.exceptions import DuplicateJobError
from rq.job import JobStatus

from app.core.redis import redis_connection

gmail_queue = Queue(
    "gmail",
    connection=redis_connection,
    default_timeout=1800,
    result_ttl=3600,
    failure_ttl=86400,
)

ai_queue = Queue(
    "ai",
    connection=redis_connection,
    default_timeout=600,
    result_ttl=3600,
    failure_ttl=86400,
)

default_queue = Queue(
    "default",
    connection=redis_connection,
    default_timeout=300,
)

JOB_RETRY = Retry(max=2, interval=[15, 60])
ACTIVE_JOB_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.STARTED,
    JobStatus.SCHEDULED,
    JobStatus.DEFERRED,
}


class QueueUnavailableError(RuntimeError):
    """Raised when a job cannot be persisted to Redis."""


def enqueue_job(
    queue: Queue,
    function: Callable[..., Any],
    *args: Any,
    job_id: str | None = None,
    **kwargs: Any,
) -> Any:
    kwargs.setdefault("retry", JOB_RETRY)
    if job_id is not None:
        kwargs["job_id"] = job_id
        kwargs["unique"] = True
    try:
        return queue.enqueue(function, *args, **kwargs)
    except DuplicateJobError:
        if job_id is None:
            raise
        try:
            existing = queue.fetch_job(job_id)
            if existing is not None and existing.get_status() in ACTIVE_JOB_STATUSES:
                return existing
            if existing is not None:
                existing.delete()
            return queue.enqueue(function, *args, **kwargs)
        except RedisError as error:
            raise QueueUnavailableError(
                "Redis is unavailable. Check REDIS_URL and confirm the Redis server is running."
            ) from error
    except RedisError as error:
        raise QueueUnavailableError(
            "Redis is unavailable. Check REDIS_URL and confirm the Redis server is running."
        ) from error


def has_active_job(queue: Queue, job_id: str) -> bool:
    try:
        job = queue.fetch_job(job_id)
        return job is not None and job.get_status() in ACTIVE_JOB_STATUSES
    except RedisError as error:
        raise QueueUnavailableError(
            "Redis is unavailable. Check REDIS_URL and confirm the Redis server is running."
        ) from error