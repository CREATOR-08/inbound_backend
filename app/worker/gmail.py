import os

from rq import SimpleWorker, Worker

from app.core.queues import gmail_queue
from app.core.redis import redis_connection


if __name__ == "__main__":
    worker_type = Worker if os.name == "posix" else SimpleWorker
    worker_type([gmail_queue], connection=redis_connection).work(with_scheduler=True)
