import hashlib
import logging
import socket
import uuid

import requests
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError
from httplib2 import ProxiesUnavailableError, ServerNotFoundError
from redis.exceptions import BusyLoadingError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from rq import get_current_job
from sqlalchemy.exc import InterfaceError, OperationalError


def initial_sync_job_id(user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return f"gmail-initial-{digest}"


def incremental_sync_job_id(execution_id: str) -> str:
    return f"gmail-incremental-{execution_id}"


def tagging_job_id(user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return f"ai-tag-{digest}"


def pubsub_execution_id(email_address: str, history_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"inbound:gmail:{email_address.strip().casefold()}:{history_id}",
        )
    )


def is_retryable_error(error: Exception) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, HttpError):
            status = getattr(getattr(current, "resp", None), "status", None)
            if status in (408, 429, 500, 502, 503, 504):
                return True
        if isinstance(
            current,
            (
                RedisConnectionError,
                RedisTimeoutError,
                BusyLoadingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                ProxiesUnavailableError,
                ServerNotFoundError,
                TransportError,
                socket.timeout,
                ConnectionError,
                TimeoutError,
            ),
        ):
            return True
        if isinstance(current, (OperationalError, InterfaceError)):
            message = str(current.orig or current).casefold()
            if current.connection_invalidated or any(
                marker in message
                for marker in (
                    "connection refused",
                    "connection reset",
                    "connection is closed",
                    "connection has been closed",
                    "connection timed out",
                    "broken pipe",
                    "could not connect",
                    "could not translate host name",
                    "network is unreachable",
                    "ssl connection has been closed",
                    "server closed the connection",
                    "timeout expired",
                    "temporarily unavailable",
                )
            ):
                return True
        current = current.__cause__ or current.__context__
    return False


def log_job_failure(
    logger: logging.Logger,
    job_name: str,
    error: Exception,
    **context: str,
) -> tuple[bool, int]:
    retryable = is_retryable_error(error)
    job = get_current_job()
    retries_left = job.retries_left if job is not None else 0
    if job is not None and not retryable:
        job.retries_left = 0
    logger.exception(
        "RQ job failed job_name=%s retryable=%s retries_left=%s context=%s",
        job_name,
        retryable,
        retries_left,
        context,
    )
    return retryable, retries_left
