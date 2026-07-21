"""
Listas de Divulgação v2 — Backend
FastAPI + Bot Telegram + Scheduler (tudo em um processo)
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytz
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import get_settings
from models import Base, Admin, BotConfig, DispatchConfig, BotCommand
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

# Flag global de saúde do backend: True somente se DB e tabelas subiram OK.
# Usada pelo endpoint /health para reportar o status real em vez de sempre "ok".
backend_ready = False
startup_error = None


# ===================== DB SESSION =====================

async def get_db():
    """Dependency que fornece uma sessão do banco."""
    async with SessionLocal() as session:
        yield session


def get_bot_instance():
    """Dependency que fornece a instância do bot."""
    return telegram_bot


# ===================== COMANDOS PADRÃO DO BOT =====================
#
# Textos originais fornecidos pelo cliente. Ficam como seeds tanto na
# criação inicial (create_initial_data) quanto na rota de migração
# pública (/migrate-bot-commands). is_default=True impede que sejam
# deletados pelo painel — o texto ainda pode ser editado normalmente.
#
# ATENÇÃO: usar aspas triplas simples nos textos porque eles contêm
# HTML compatível com o Telegram (b, i, code, blockquote) e ficam mais
# fáceis de manter.

DEFAULT_BOT_COMMANDS = [
    {
        "command": "como_funciona",
        "description": "Explicação curta de como o projeto funciona",
        "response_text": (
            "🔩 <b>Como funciona</b> ❓\n\n"
            "O processo é extremamente simples.\n\n"
            "1. Entre em contato com a administração do projeto. @SuporteFLIXz\n"
            "2. Adicione o Bot Oficial @AcessoXbrsbot como administrador do canal "
            "ou grupo que participa da rede, concedendo todas as permissões necessárias.\n"
            "3. Você poderá cadastrar até 5 canais ou grupos por participante.\n"
            "4. Os canais e grupos devem estar abertos (públicos) para que sejam "
            "adicionados à pasta oficial utilizada pelo sistema de divulgação cruzada."
        ),
        "has_webapp_button": False,
        "sort_order": 10,
    },
    {
        "command": "admin",
        "description": "Contato do administrador do projeto",
        "response_text": (
            "Para participar ou tratar de outros assuntos entre em contato com o "
            "admin do projeto @SuporteFLIXz"
        ),
        "has_webapp_button": False,
        "sort_order": 20,
    },
    {
        "command": "instrucoes",
        "description": "Abre o Mini App com instruções completas do Elite PRIME",
        "response_text": (
            "Para saber mais sobre o nosso projeto, sobre como funciona de forma "
            "mais completa e etc.. clique no botão abaixo!!"
        ),
        "has_webapp_button": True,
        "button_text": "📖 Ver instruções completas",
        # URL padrão apontando pro frontend do projeto Listas em produção
        # (Vercel). O admin pode editar pelo painel se o domínio mudar.
        "webapp_url": "https://listas-divulgacao-bot-fd.vercel.app/mini-app/instrucoes",
        "sort_order": 30,
    },
]


async def _seed_default_commands(session):
    """Insere os comandos padrão se ainda não existirem.

    Idempotente: se o comando já existe (pelo nome), NÃO sobrescreve — o
    admin pode ter editado o texto pelo painel e não queremos reverter.
    Só cria os que estão faltando. Usado tanto no startup quanto na rota
    de migração pública.
    """
    created = 0
    for cmd_data in DEFAULT_BOT_COMMANDS:
        existing = await session.scalar(
            select(BotCommand).where(BotCommand.command == cmd_data["command"])
        )
        if existing:
            continue
        session.add(BotCommand(
            command=cmd_data["command"],
            description=cmd_data.get("description"),
            response_text=cmd_data["response_text"],
            has_webapp_button=cmd_data.get("has_webapp_button", False),
            button_text=cmd_data.get("button_text"),
            webapp_url=cmd_data.get("webapp_url"),
            is_active=True,
            is_default=True,
            sort_order=cmd_data.get("sort_order", 0),
        ))
        created += 1
    return created


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

        # Comandos padrão do bot (idempotente — só cria os que faltam)
        created_cmds = await _seed_default_commands(session)
        if created_cmds > 0:
            logger.info(f"✅ {created_cmds} comando(s) padrão do bot seedado(s)")

        await session.commit()


# ===================== SCHEDULER =====================

def setup_scheduler():
    """Configura os jobs do scheduler lendo a config do banco."""

    async def scheduled_dispatch():
        """Job de disparo automático."""
        if telegram_bot and telegram_bot.is_running:
            # Verificar se os disparos estão ativos antes de executar.
            # Isso permite pausar disparos pelo painel sem precisar
            # remover os jobs do scheduler.
            async with SessionLocal() as session:
                config = await session.scalar(select(DispatchConfig).limit(1))
                if not config or not config.is_active:
                    logger.info("⏸️ Disparo automático ignorado: is_active está desativado no painel")
                    return
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

    asyncio.ensure_future(load_schedule_from_db())
    scheduler.start()
    logger.info("📅 Scheduler iniciado")


# ===================== LIFESPAN =====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup e shutdown do app.

    IMPORTANTE: nada aqui pode deixar uma exceção "vazar" para fora do
    bloco try, senão o FastAPI/Uvicorn derruba o processo inteiro ANTES
    de conseguir aceitar qualquer requisição - inclusive o /health.
    Isso fazia o Railway mostrar "Application failed to respond" mesmo
    para rotas que não dependem do banco.
    """
    global engine, SessionLocal, telegram_bot, backend_ready, startup_error

    logger.info("🚀 Iniciando backend Listas de Divulgação v2...")

    try:
        # Banco de dados
        if not settings.DATABASE_URL_ASYNC:
            raise RuntimeError(
                "DATABASE_URL não configurada (variável de ambiente ausente ou vazia)."
            )

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

        backend_ready = True
        logger.info("✅ Backend pronto!")

    except Exception as e:
        # Não deixamos a exceção subir: o processo continua de pé,
        # servindo /health (que reporta o erro) em vez de morrer em
        # silêncio e virar "Application failed to respond" no Railway.
        startup_error = str(e)
        logger.error(f"❌ ERRO FATAL NO STARTUP: {e}", exc_info=True)

    yield

    # Shutdown
    logger.info("🔴 Desligando backend...")
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass
    if telegram_bot:
        await telegram_bot.stop()
    if engine:
        await engine.dispose()
    logger.info("🔴 Backend desligado")


# ===================== MIDDLEWARE: NORMALIZAÇÃO DE PATH =====================

class NormalizePathMiddleware:
    """Middleware ASGI puro que colapsa barras duplicadas no path da URL.

    Motivo: o frontend, se configurado com VITE_API_URL terminando em "/"
    (ex: ".../api/"), gera chamadas como "//api/auth/login" em vez de
    "/api/auth/login". O FastAPI trata isso como uma rota diferente e
    devolve 404, mesmo com a rota "/api/auth/login" corretamente
    registrada em routes.py. Esta camada corrige isso no backend,
    funcionando como segurança extra independente de qualquer bug de
    configuração que volte a acontecer no frontend.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if "//" in path:
                normalized = re.sub(r"/{2,}", "/", path)
                scope["path"] = normalized
                logger.info(f"🔧 Path normalizado: '{path}' → '{normalized}'")
        await self.app(scope, receive, send)


# ===================== APP =====================

app = FastAPI(
    title="Listas de Divulgação v2",
    description="API do sistema de divulgação cruzada no Telegram",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS
frontend_origins = [
    settings.FRONTEND_URL,
    "http://localhost:5173",
    "http://localhost:3000",
]
# Limpar origens vazias
frontend_origins = [o for o in frontend_origins if o]

app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Normalização de path (ver classe acima) — protege contra "//api/..."
app.add_middleware(NormalizePathMiddleware)

# Injetar dependências reais nos routes
#
# CORREÇÃO CRÍTICA: a abordagem anterior (routes_module.get_db = get_db)
# NÃO funciona com FastAPI. Cada rota que usa Depends(get_db) já capturou
# a referência da função original (o placeholder que sempre lança
# NotImplementedError) no momento em que routes.py foi importado e os
# decorators @router.get/post/etc rodaram. Reatribuir o atributo do
# módulo depois disso não tem efeito nenhum sobre rotas já registradas.
#
# O mecanismo correto do FastAPI para isso é app.dependency_overrides,
# que mapeia pela IDENTIDADE do callable original (_get_db_placeholder /
# _get_bot_placeholder, importados acima antes de qualquer reatribuição)
# para a implementação real. É resolvido a cada requisição, então sempre
# pega o valor mais atual de SessionLocal / telegram_bot.
import routes as routes_module
app.dependency_overrides[_get_db_placeholder] = get_db
app.dependency_overrides[_get_bot_placeholder] = get_bot_instance

# Registrar rotas com prefixo /api
app.include_router(router, prefix="/api")


# ===================== HEALTH CHECK =====================

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Listas de Divulgação v2",
        "backend_ready": backend_ready,
        "startup_error": startup_error,
        "bot_connected": telegram_bot.is_running if telegram_bot else False,
    }


@app.get("/health")
async def health():
    # Sempre responde 200 (para o Railway healthcheck não achar que o
    # container morreu), mas agora reporta o status real do startup em
    # vez de mentir dizendo "ok" mesmo quando o banco/bot falharam.
    return {
        "status": "ok" if backend_ready else "degraded",
        "backend_ready": backend_ready,
        "startup_error": startup_error,
    }


# ===================== MIGRAÇÕES =====================
#
# Padrão do projeto Zenyx VIPs: rotas públicas (sem auth) no root do
# app, prefixadas com "/migrate-", que criam tabelas/colunas novas e
# semeiam dados iniciais. São idempotentes — podem ser chamadas
# quantas vezes forem necessárias após cada deploy sem risco.
#
# Como acessar após deploy no Railway:
# https://listas-divulgacao-bot-bk.up.railway.app/migrate-bot-commands

@app.get("/migrate-bot-commands")
async def migrate_bot_commands():
    """Cria a tabela bot_commands e semeia os 3 comandos padrão.

    Executa `CREATE TABLE IF NOT EXISTS` via SQLAlchemy metadata (a
    própria create_all já é idempotente) e depois chama
    _seed_default_commands, que só insere os comandos que ainda não
    existem — nunca sobrescreve texto que o admin tenha editado.
    """
    result = {
        "success": False,
        "table_created": False,
        "commands_seeded": 0,
        "existing_commands": [],
        "error": None,
    }

    if not backend_ready or engine is None or SessionLocal is None:
        result["error"] = (
            "Backend não está pronto — verifique /health. "
            "Startup pode ter falhado na conexão com o banco."
        )
        return result

    try:
        # 1) Garante a tabela — a metadata.create_all só cria o que falta,
        # então é seguro rodar em produção sem afetar tabelas existentes.
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: BotCommand.__table__.create(sync_conn, checkfirst=True)
            )
        result["table_created"] = True

        # 2) Semeia os 3 comandos padrão (idempotente — só cria os que faltam).
        async with SessionLocal() as session:
            created = await _seed_default_commands(session)
            await session.commit()

            # Lista os comandos que existem agora — pro admin conferir.
            existing_result = await session.execute(
                select(BotCommand).order_by(BotCommand.sort_order.asc(), BotCommand.id.asc())
            )
            existing = existing_result.scalars().all()
            result["existing_commands"] = [
                {
                    "id": c.id,
                    "command": c.command,
                    "is_active": c.is_active,
                    "is_default": c.is_default,
                    "has_webapp_button": c.has_webapp_button,
                }
                for c in existing
            ]

        result["commands_seeded"] = created

        # 3) Se o bot está rodando, aproveita e recarrega os handlers
        # pra que os comandos recém-criados já respondam sem redeploy.
        if telegram_bot and telegram_bot.is_running:
            try:
                await telegram_bot.reload_commands()
                result["bot_reloaded"] = True
            except Exception as e:
                result["bot_reloaded"] = False
                result["bot_reload_error"] = str(e)

        result["success"] = True
        return result

    except Exception as e:
        logger.error(f"❌ Migração /migrate-bot-commands falhou: {e}", exc_info=True)
        result["error"] = str(e)
        return result


@app.get("/migrate-fix-instrucoes-url")
async def migrate_fix_instrucoes_url():
    """Corrige a webapp_url do comando /instrucoes se ainda estiver com
    o valor incorreto do primeiro deploy (apontava pra zenyxvips.com).

    Idempotente e SEGURA: só atualiza o registro se o valor atual for
    exatamente a URL antiga errada. Se o admin já editou pelo painel
    (URL customizada) ou se já está correta, NÃO mexe — preserva
    qualquer customização feita manualmente.
    """
    OLD_WRONG_URL = "https://zenyxvips.com/mini-app/instrucoes"
    NEW_CORRECT_URL = "https://listas-divulgacao-bot-fd.vercel.app/mini-app/instrucoes"

    result = {
        "success": False,
        "action": "none",
        "old_url": None,
        "new_url": None,
        "error": None,
    }

    if not backend_ready or SessionLocal is None:
        result["error"] = (
            "Backend não está pronto — verifique /health. "
            "Startup pode ter falhado na conexão com o banco."
        )
        return result

    try:
        async with SessionLocal() as session:
            cmd = await session.scalar(
                select(BotCommand).where(BotCommand.command == "instrucoes")
            )
            if not cmd:
                result["action"] = "skipped_not_found"
                result["error"] = (
                    "Comando /instrucoes não existe no banco — "
                    "rode /migrate-bot-commands primeiro."
                )
                return result

            result["old_url"] = cmd.webapp_url

            if cmd.webapp_url == NEW_CORRECT_URL:
                # Já está correto — nada a fazer.
                result["action"] = "already_correct"
                result["new_url"] = cmd.webapp_url
                result["success"] = True
                return result

            if cmd.webapp_url != OLD_WRONG_URL:
                # Foi customizado pelo admin — respeita e não sobrescreve.
                result["action"] = "skipped_customized"
                result["new_url"] = cmd.webapp_url
                result["success"] = True
                return result

            # Atualiza só o valor antigo errado.
            cmd.webapp_url = NEW_CORRECT_URL
            cmd.updated_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(cmd)

            result["action"] = "updated"
            result["new_url"] = cmd.webapp_url

        # Recarrega o bot pra refletir imediatamente
        if telegram_bot and telegram_bot.is_running:
            try:
                await telegram_bot.reload_commands()
                result["bot_reloaded"] = True
            except Exception as e:
                result["bot_reloaded"] = False
                result["bot_reload_error"] = str(e)

        result["success"] = True
        return result

    except Exception as e:
        logger.error(f"❌ Migração /migrate-fix-instrucoes-url falhou: {e}", exc_info=True)
        result["error"] = str(e)
        return result


# ===================== ENTRYPOINT =====================

if __name__ == "__main__":
    import os
    import uvicorn

    # CRÍTICO: o Railway atribui a porta dinamicamente via variável de
    # ambiente PORT. Rodar fixo em 8000 pode não bater com a porta que
    # o domínio público está de fato roteando, resultando em
    # "Application failed to respond" mesmo com o processo no ar.
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)