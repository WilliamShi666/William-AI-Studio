from pydantic import BaseSettings

class Settings(BaseSettings):
    project_name: str = "Roys Legion Knowledge Base API"
    project_version: str = "0.1.0"
    database_url: str = "sqlite:///./test.db"
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection_name: str = "public_tutoring_demo"
    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    @property
    def sqlalchemy_database_uri(self) -> str:
        return self.database_url

    class Config:
        env_file = ".env"
        case_sensitive = False

settings = Settings()

DATABASE_URL = settings.database_url
SQLALCHEMY_DATABASE_URI = settings.sqlalchemy_database_uri
MILVUS_HOST = settings.milvus_host
MILVUS_PORT = settings.milvus_port
