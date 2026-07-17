"""
Listas de Divulgação v2 — Backend
FastAPI + Bot Telegram + Scheduler (tudo em um processo)
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import pytz
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import get_settings
from models import Base, Admin, BotConfig, DispatchConfig
from auth import hash_password
from bot import TelegramBot
from routes import router, get_db as _get_db_placeholder, get_bot as _get_bot_placeholder

# ===================== LOGGING =====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ===================== GLOBALS =====================

settings = get_settings()
engine = None
SessionLocal = None
telegram_bot = None
scheduler = AsyncIOScheduler(timezone=pytz.timezone(settings.TZ))


# ===================== DB SESSION =====================

async def get_db():
    """Dependency que fornece uma sessão do banco."""
    async with SessionLocal() as session:
        yield session


def get_bot_instance():
    """Dependency que fornece a instância do bot."""
    return telegram_bot


# ===================== SETUP INICIAL =====================

async def create_initial_data():
    """Cria admin padrão e config de disparo se não existirem."""
    async with SessionLocal() as session:
        # Admin
        admin = await session.scalar(select(Admin).limit(1))
        if not admin:
            admin = Admin(
                username=settings.ADMIN_USERNAME,
                password_hash=hash_password(settings.ADMIN_PASSWORD),
            )
            session.add(admin)
            logger.info(f"✅ Admin criado: {settings.ADMIN_USERNAME}")

        # DispatchConfig padrão
        config = await session.scalar(select(DispatchConfig).limit(1))
        if not config:
            session.add(DispatchConfig())
            logger.info("✅ Config de disparo padrão criada")

        # BotConfig (se token fornecido via env)
        if settings.BOT_TOKEN:
            bot_config = await session.scalar(select(BotConfig).limit(1))
            if not bot_config:
                session.add(BotConfig(bot_token=settings.BOT_TOKEN))
                logger.info("✅ Bot config criada a partir da env BOT_TOKEN")

        await session.commit()


# ===================== SCHEDULER =====================

def setup_scheduler():
    """Configura os jobs do scheduler lendo a config do banco."""

    async def scheduled_dispatch():
        """Job de disparo automático."""
        if telegram_bot and telegram_bot.is_running:
            await telegram_bot.dispatch("auto")

    async def scheduled_cleanup():
        """Job de limpeza automática."""
        if telegram_bot and telegram_bot.is_running:
            await telegram_bot.cleanup()

    async def scheduled_verify():
        """Job de verificação de canais ativos."""
        if telegram_bot and telegram_bot.is_running:
            await telegram_bot.verify_active_channels()

    async def scheduled_metrics():
        """Job de coleta de métricas de membros."""
        if telegram_bot and telegram_bot.is_running:
            await telegram_bot.collect_member_counts()

    async def load_schedule_from_db():
        """Carrega os horários do banco e configura os jobs."""
        async with SessionLocal() as session:
            config = await session.scalar(select(DispatchConfig).limit(1))
            if not config or not config.is_active:
                logger.info("⏸️ Scheduler: config inativa ou não encontrada")
                return

            tz = pytz.timezone(settings.TZ)

            # Disparo automático
            scheduler.add_job(
                scheduled_dispatch,
                CronTrigger(
                    day_of_week=config.schedule_days,
                    hour=config.schedule_hour,
                    minute=config.schedule_minute,
                    timezone=tz,
                ),
                id="disparo_automatico",
                replace_existing=True,
            )
            logger.info(f"📅 Disparo: {config.schedule_days} às {config.schedule_hour}:{config.schedule_minute:02d}")

            # Limpeza automática
            if config.auto_delete:
                scheduler.add_job(
                    scheduled_cleanup,
                    CronTrigger(
                        day_of_week=config.cleanup_days,
                        hour=config.cleanup_hour,
                        minute=config.cleanup_minute,
                        timezone=tz,
                    ),
                    id="limpeza_automatica",
                    replace_existing=True,
                )
                logger.info(f"🗑️ Limpeza: {config.cleanup_days} às {config.cleanup_hour}:{config.cleanup_minute:02d}")

            # Verificação de canais: a cada 2 horas
            scheduler.add_job(
                scheduled_verify,
                "interval",
                hours=2,
                id="verificacao_canais",
                replace_existing=True,
            )

            # Coleta de métricas: a cada 6 horas
            scheduler.add_job(
                scheduled_metrics,
                "interval",
                hours=6,
                id="coleta_metricas",
                replace_existing=True,
            )

    asyncio.get_event_loop().create_task(load_schedule_from_db())
    scheduler.start()
    logger.info("📅 Scheduler iniciado")


# ===================== LIFESPAN =====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup e shutdown do app."""
    global engine, SessionLocal, telegram_bot

    logger.info("🚀 Iniciando backend Listas de Divulgação v2...")

    # Banco de dados
    engine = create_async_engine(settings.DATABASE_URL_ASYNC, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Criar tabelas
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("✅ Banco de dados conectado e tabelas criadas")

    # Dados iniciais
    await create_initial_data()

    # Bot Telegram
    telegram_bot = TelegramBot(SessionLocal)
    async with SessionLocal() as session:
        bot_config = await session.scalar(select(BotConfig).limit(1))
        if bot_config and bot_config.bot_token:
            try:
                await telegram_bot.start(bot_config.bot_token)
            except Exception as e:
                logger.error(f"❌ Erro ao iniciar bot: {e}")

    # Scheduler
    setup_scheduler()

    logger.info("✅ Backend pronto!")

    yield

    # Shutdown
    logger.info("🔴 Desligando backend...")
    scheduler.shutdown(wait=False)
    if telegram_bot:
        await telegram_bot.stop()
    if engine:
        await engine.dispose()
    logger.info("🔴 Backend desligado")


# ===================== APP =====================

app = FastAPI(
    title="Listas de Divulgação v2",
    description="API do sistema de divulgação cruzada no Telegram",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.FRONTEND_URL,
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Injetar dependências reais nos routes
import routes as routes_module
routes_module.get_db = get_db
routes_module.get_bot = get_bot_instance

# Registrar rotas com prefixo /api
app.include_router(router, prefix="/api")


# ===================== HEALTH CHECK =====================

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Listas de Divulgação v2",
        "bot_connected": telegram_bot.is_running if telegram_bot else False,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# ===================== ENTRYPOINT =====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
