import os
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
INTERNAL_TOKEN = "test-internal-token"

# app.config builds Settings() at import time, so the environment must be set first.
os.environ.update(RABBITMQ_URL="amqp://unused", INTERNAL_TOKEN=INTERNAL_TOKEN, DB_PATH="unused.db")
