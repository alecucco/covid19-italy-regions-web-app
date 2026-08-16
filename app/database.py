"""SQLAlchemy engine and session setup."""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    # Explicit, short connection timeout: without this, if the database
    # is unreachable, the failure follows the operating system's default
    # TCP timeout -- on Windows this can be much longer than on Linux,
    # making the app look frozen instead of just waiting. With this set,
    # every attempt fails predictably.
    connect_args={"connect_timeout": 3},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Declarative base class all ORM models inherit from."""