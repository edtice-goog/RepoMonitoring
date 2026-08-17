"""Engine + Session factory. Connection comes from DATABASE_URL (default: the
user-owned WSL2 cluster that infra/stack.sh stands up)."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "postgresql+psycopg://repomon@127.0.0.1:5544/repomon"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, class_=Session, future=True, expire_on_commit=False)
