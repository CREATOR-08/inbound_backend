import os
import redis
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = "redis://default:gQAAAAAAAvIOAAIgcDJlNTI2MzA0MjkyNjA0NzQwOGU5ZTI4ZWU0MTA3MDE3Nw@allowed-ocelot-193038.upstash.io:6379"
if not REDIS_URL:
    raise RuntimeError(
        "REDIS_URL is required. Set it to the address of the Redis server."
    )

redis_connection = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=False,
    socket_connect_timeout=5,
    socket_timeout=5,
    health_check_interval=30,
)