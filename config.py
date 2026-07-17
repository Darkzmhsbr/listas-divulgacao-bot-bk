"""
Configurações centralizadas do projeto.
Lê variáveis de ambiente automaticamente.
"""

import os
from functools import lru_cache


class Settings:
    """Configurações do sistema lidas de variáveis de ambiente."""

    # --- Banco de dados ---
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

    # --- JWT ---
    JWT_SECRET: str = os.environ.get("JWT_SECRET", "change-me-in-production")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_MINUTES: int = int(os.environ.get("JWT_EXPIRATION_MINUTES", "120"))

    # --- Admin inicial ---
    ADMIN_USERNAME: str = os.environ.get("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "admin123")

    # --- Telegram ---
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
    ADMIN_CHAT_ID: str = os.environ.get("ADMIN_CHAT_ID", "")

    # --- Integração com site antigo (opcional) ---
    DATABASE_URL_SITE: str = os.environ.get("DATABASE_URL_SITE", "")

    # --- CORS ---
    FRONTEND_URL: str = os.environ.get("FRONTEND_URL", "http://localhost:5173")

    # --- Timezone ---
    TZ: str = os.environ.get("TZ", "America/Sao_Paulo")

    def __init__(self):
        # Railway usa postgres:// mas SQLAlchemy precisa de postgresql+asyncpg://
        if self.DATABASE_URL:
            url = self.DATABASE_URL
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql://", 1)
            # Para async, precisamos de postgresql+asyncpg://
            if url.startswith("postgresql://"):
                self.DATABASE_URL_ASYNC = url.replace(
                    "postgresql://", "postgresql+asyncpg://", 1
                )
                self.DATABASE_URL_SYNC = url
            else:
                self.DATABASE_URL_ASYNC = url
                self.DATABASE_URL_SYNC = url
        else:
            self.DATABASE_URL_ASYNC = ""
            self.DATABASE_URL_SYNC = ""


@lru_cache()
def get_settings() -> Settings:
    return Settings()
