"""
Root conftest.py — sets DATABASE_URL before any app module is imported so
Pydantic Settings finds it even in environments where .env isn't present.
"""

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/ghlconnector_test",
)
