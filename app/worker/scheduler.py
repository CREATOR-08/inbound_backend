import logging
import os

from rq.scheduler import RQScheduler

from app.core.queues import ai_queue, gmail_queue
from app.core.redis import redis_connection

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULER_INTERVAL_SECONDS = 90
SCHEDULER_INTERVAL_ENV = "RQ_SCHEDULER_INTERVAL_SECONDS"
MANAGED_QUEUES = (gmail_queue, ai_queue)


def _scheduler_interval_seconds() -> int:
    value = os.getenv(
        SCHEDULER_INTERVAL_ENV,
        str(DEFAULT_SCHEDULER_INTERVAL_SECONDS),
    )
    try:
        interval = int(value)
    except ValueError as error:
        raise RuntimeError(
            f"{SCHEDULER_INTERVAL_ENV} must be a positive integer."
        ) from error

    if interval < 1:
        raise RuntimeError(f"{SCHEDULER_INTERVAL_ENV} must be a positive integer.")
    return interval


def main() -> None:
    scheduler = RQScheduler(
        MANAGED_QUEUES,
        connection=redis_connection,
        interval=_scheduler_interval_seconds(),
        logging_level=logging.INFO,
    )
    queue_names = ",".join(queue.name for queue in MANAGED_QUEUES)
    logger.info(
        "Starting RQ scheduler name=%s queues=%s interval_seconds=%s",
        scheduler.name,
        queue_names,
        scheduler.interval,
    )

    try:
        scheduler.work()
    except BaseException:
        try:
            scheduler.stop()
        except Exception:
            logger.exception("Could not stop RQ scheduler cleanly.")
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
