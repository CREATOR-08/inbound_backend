
import logging
import os
from contextvars import ContextVar, Token
from time import perf_counter
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.pool import QueuePool

load_dotenv()

logger = logging.getLogger(__name__)
_request_context: ContextVar[tuple[str, str] | None] = ContextVar(
    "database_request_context",
    default=None,
)
_physical_connect_duration_ms: ContextVar[float] = ContextVar(
    "physical_connect_duration_ms",
    default=0.0,
)


def set_database_request_context(endpoint: str, request_id: str) -> Token:
    return _request_context.set((endpoint, request_id))


def reset_database_request_context(token: Token) -> None:
    _request_context.reset(token)


def log_database_timing(event_name: str, **fields: Any) -> None:
    context = _request_context.get()
    endpoint, request_id = context if context is not None else ("unknown", "unknown")
    details = " ".join(f"{name}={value}" for name, value in fields.items())
    logger.info(
        "database_performance event=%s endpoint=%s request_id=%s %s",
        event_name,
        endpoint,
        request_id,
        details,
    )


def _pool_metrics(pool: QueuePool) -> dict[str, int]:
    return {
        "pool_checked_out": pool.checkedout(),
        "pool_overflow": max(pool.overflow(), 0),
        "pool_available": pool._pool.qsize(),
    }


class InstrumentedQueuePool(QueuePool):
    def _do_get(self):
        started = perf_counter()
        connect_token = _physical_connect_duration_ms.set(0.0)
        succeeded = False
        try:
            connection = super()._do_get()
            succeeded = True
            return connection
        finally:
            elapsed_ms = (perf_counter() - started) * 1000
            physical_connect_ms = _physical_connect_duration_ms.get()
            _physical_connect_duration_ms.reset(connect_token)
            log_database_timing(
                "pool_checkout",
                checkout_duration_ms=f"{elapsed_ms:.1f}",
                pool_wait_duration_ms=f"{max(elapsed_ms - physical_connect_ms, 0):.1f}",
                checkout_succeeded=succeeded,
                **_pool_metrics(self),
            )


class TimedCursor:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def _fetch(self, method_name: str, *args):
        started = perf_counter()
        try:
            return getattr(self._cursor, method_name)(*args)
        finally:
            log_database_timing(
                "result_fetch",
                fetch_method=method_name,
                fetch_duration_ms=f"{(perf_counter() - started) * 1000:.1f}",
            )

    def fetchone(self):
        return self._fetch("fetchone")

    def fetchmany(self, size=None):
        if size is None:
            return self._fetch("fetchmany")
        return self._fetch("fetchmany", size)

    def fetchall(self):
        return self._fetch("fetchall")


def _sql_operation(statement: str) -> str:
    operation = statement.lstrip().split(maxsplit=1)
    if not operation:
        return "UNKNOWN"
    first_word = operation[0].upper()
    return first_word if first_word.isalpha() else "OTHER"


def instrument_engine(database_engine) -> None:
    @event.listens_for(database_engine, "do_connect", retval=True)
    def measure_physical_connect(dialect, connection_record, cargs, cparams):
        started = perf_counter()
        succeeded = False
        try:
            connection = dialect.connect(*cargs, **cparams)
            succeeded = True
            return connection
        finally:
            elapsed_ms = (perf_counter() - started) * 1000
            _physical_connect_duration_ms.set(
                _physical_connect_duration_ms.get() + elapsed_ms
            )
            log_database_timing(
                "physical_connection",
                connection_duration_ms=f"{elapsed_ms:.1f}",
                connection_succeeded=succeeded,
            )

    @event.listens_for(database_engine, "before_cursor_execute")
    def start_sql_timing(connection, cursor, statement, parameters, context, executemany):
        context._database_operation = _sql_operation(statement)
        context._database_execute_started = perf_counter()

    @event.listens_for(database_engine, "after_cursor_execute")
    def finish_sql_timing(connection, cursor, statement, parameters, context, executemany):
        started = getattr(context, "_database_execute_started", None)
        if started is not None:
            log_database_timing(
                "sql_execution",
                sql_operation=getattr(context, "_database_operation", "UNKNOWN"),
                execution_duration_ms=f"{(perf_counter() - started) * 1000:.1f}",
                executemany=executemany,
            )
        context.cursor = TimedCursor(cursor)

    @event.listens_for(database_engine, "handle_error")
    def log_sql_error(exception_context):
        context = exception_context.execution_context
        started = getattr(context, "_database_execute_started", None)
        if started is not None:
            log_database_timing(
                "sql_execution",
                sql_operation=getattr(context, "_database_operation", "UNKNOWN"),
                execution_duration_ms=f"{(perf_counter() - started) * 1000:.1f}",
                succeeded=False,
            )


class InstrumentedSession(Session):
    def commit(self) -> None:
        started = perf_counter()
        succeeded = False
        try:
            super().commit()
            succeeded = True
        finally:
            log_database_timing(
                "transaction_commit",
                duration_ms=f"{(perf_counter() - started) * 1000:.1f}",
                succeeded=succeeded,
            )

    def rollback(self) -> None:
        started = perf_counter()
        succeeded = False
        try:
            super().rollback()
            succeeded = True
        finally:
            log_database_timing(
                "transaction_rollback",
                duration_ms=f"{(perf_counter() - started) * 1000:.1f}",
                succeeded=succeeded,
            )


DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set in environment or .env file")

# Normalize PostgreSQL connection URLs for psycopg.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://", "postgresql+psycopg://", 1
    )
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgresql://", "postgresql+psycopg://", 1
    )

engine = create_engine(DATABASE_URL, poolclass=InstrumentedQueuePool)
instrument_engine(engine)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    class_=InstrumentedSession,
)

Base = declarative_base()


def get_db():
    db: Session = SessionLocal()

    try:
        yield db
    finally:
        db.close()
