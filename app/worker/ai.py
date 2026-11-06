import os

from rq import SimpleWorker, Worker

from app.core.queues import ai_queue
from app.core.redis import redis_connection


if __name__ == "__main__":
    worker_type = Worker if os.name == "posix" else SimpleWorker
    worker_type([ai_queue], connection=redis_connection).work(with_scheduler=True)
