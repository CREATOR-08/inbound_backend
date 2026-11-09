import os
import redis
from dotenv import load_dotenv

load_dotenv()

REDIS_URL =os.getenv("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError(
        "REDIS_URL is required. Set it to the address of the Redis server."
    )

redis_connection = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=False,
    socket_connect_timeout=5,
    socket_timeout=5,
)