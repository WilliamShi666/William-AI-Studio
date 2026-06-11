from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from src.core.config import settings

engine = create_engine(settings.sqlalchemy_database_uri, connect_args={"check_same_thread": False})
SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
