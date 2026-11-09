from rq import Worker

from app.core.queues import gmail_queue
from app.core.redis import redis_connection


def main() -> None:
    Worker([gmail_queue], connection=redis_connection).work(
        with_scheduler=False,
        logging_level="INFO",
    )


if __name__ == "__main__":
    main()
