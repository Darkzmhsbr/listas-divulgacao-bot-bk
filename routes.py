"""
Rotas da API REST — todas em um único arquivo.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth import get_current_admin, hash_password, verify_password, create_token
from models import (
    Admin, BotConfig, SourceChannel, Channel,
    DispatchConfig, DispatchLog, SentMessage, MemberCountHistory,
    BotCommand,
)
from schemas import (
    LoginRequest, TokenResponse, AdminResponse,
    BotConfigUpdate, BotConfigResponse, BotVerifyResponse,
    SourceChannelUpdate, SourceChannelResponse, SourceChannelVerifyResponse,
    ChannelCreate, ChannelUpdate, ChannelResponse, ChannelVerifyResponse, BulkVerifyResponse,
    DispatchConfigUpdate, DispatchConfigResponse,
    DispatchLogResponse, DispatchLogDetailResponse, SentMessageResponse,
    ChannelMetricsResponse, MemberCountEntry, DashboardSummary,
    TriggerResponse,
    TopGrowthEntry, TopGrowthResponse,
    BotCommandCreate, BotCommandUpdate, BotCommandResponse, BotCommandReloadResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# =====================================================
#  Dependência: obter sessão do banco de dados
#  (será injetada no main.py via app.dependency_overrides
#   ou via um getter global)
# =====================================================

# Placeholder — main.py vai sobrescrever isso
async def get_db() -> AsyncSession:
    raise NotImplementedError("get_db não foi configurado")

# Placeholder — main.py vai injetar a instância do bot
def get_bot():
    raise NotImplementedError("get_bot não foi configurado")


# =====================================================
#  AUTH
# =====================================================

@router.post("/auth/login", response_model=TokenResponse, tags=["Auth"])
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    admin = await db.scalar(
        select(Admin).where(Admin.username == body.username)
    )
    if not admin or not verify_password(body.password, admin.password_hash):
        raise HTTPException(status_code=401, detail="Credenciais inválidas")

    token = create_token(admin.username)
    return TokenResponse(access_token=token)


@router.get("/auth/me", response_model=AdminResponse, tags=["Auth"])
async def get_me(
    username: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    admin = await db.scalar(select(Admin).where(Admin.username == username))
    if not admin:
        raise HTTPException(status_code=404, detail="Admin não encontrado")
    return AdminResponse(id=admin.id, username=admin.username)


# =====================================================
#  BOT CONFIG
# =====================================================

@router.get("/bot", response_model=BotConfigResponse, tags=["Bot"])
async def get_bot_config(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    config = await db.scalar(select(BotConfig).limit(1))
    if not config:
        raise HTTPException(status_code=404, detail="Bot não configurado")
    return config


@router.put("/bot", response_model=BotConfigResponse, tags=["Bot"])
async def update_bot_config(
    body: BotConfigUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    config = await db.scalar(select(BotConfig).limit(1))
    if config:
        config.bot_token = body.bot_token
        config.updated_at = datetime.now(timezone.utc)
    else:
        config = BotConfig(bot_token=body.bot_token)
        db.add(config)

    await db.commit()
    await db.refresh(config)
    return config


@router.post("/bot/verify", response_model=BotVerifyResponse, tags=["Bot"])
async def verify_bot(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
    bot=Depends(get_bot),
):
    config = await db.scalar(select(BotConfig).limit(1))
    if not config:
        return BotVerifyResponse(success=False, message="Bot não configurado")

    success, username = await bot.verify_bot_token(config.bot_token)
    if success:
        config.bot_username = username
        config.is_active = True
        await db.commit()

    return BotVerifyResponse(success=success, bot_username=username, message="Token válido" if success else "Token inválido")


# =====================================================
#  BOT COMMANDS (comandos dinâmicos configuráveis)
# =====================================================
#
# CRUD dos comandos que o bot central responde no Telegram. O admin
# pode editar o texto de resposta, ativar/desativar, e opcionalmente
# adicionar um botão inline com WebApp (Mini App) que abre uma URL.
# is_default=True marca os 3 comandos padrão (/como_funciona, /admin,
# /instrucoes) e bloqueia o DELETE — o texto ainda pode ser editado.
#
# Após qualquer mudança (criar/editar/deletar/ativar/desativar) o
# frontend deve chamar POST /bot/commands/reload para o bot registrar
# os handlers novos sem precisar reiniciar o processo.

@router.get("/bot/commands", response_model=list[BotCommandResponse], tags=["Bot Commands"])
async def list_bot_commands(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    result = await db.execute(
        select(BotCommand).order_by(BotCommand.sort_order.asc(), BotCommand.id.asc())
    )
    return result.scalars().all()


@router.post("/bot/commands", response_model=BotCommandResponse, tags=["Bot Commands"])
async def create_bot_command(
    body: BotCommandCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    # Normaliza o nome do comando: remove barra inicial e espaços.
    # O Telegram só aceita [a-z0-9_], então normalizamos aqui pra evitar
    # que o handler simplesmente nunca dispare por conta de acento/espaço.
    cmd_name = (body.command or "").strip().lstrip("/").lower()
    if not cmd_name:
        raise HTTPException(status_code=400, detail="Nome do comando obrigatório")

    # Checa duplicidade
    existing = await db.scalar(select(BotCommand).where(BotCommand.command == cmd_name))
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Já existe um comando com o nome '{cmd_name}'",
        )

    # Validação básica do botão WebApp
    if body.has_webapp_button:
        if not body.button_text or not body.webapp_url:
            raise HTTPException(
                status_code=400,
                detail="Botão WebApp exige button_text e webapp_url preenchidos",
            )
        if not body.webapp_url.startswith("https://"):
            raise HTTPException(
                status_code=400,
                detail="webapp_url precisa ser HTTPS (exigência do Telegram)",
            )

    cmd = BotCommand(
        command=cmd_name,
        description=body.description,
        response_text=body.response_text,
        has_webapp_button=body.has_webapp_button,
        button_text=body.button_text,
        webapp_url=body.webapp_url,
        is_active=body.is_active,
        is_default=False,  # novos comandos NUNCA são default
        sort_order=body.sort_order,
    )
    db.add(cmd)
    await db.commit()
    await db.refresh(cmd)
    return cmd


@router.put("/bot/commands/{command_id}", response_model=BotCommandResponse, tags=["Bot Commands"])
async def update_bot_command(
    command_id: int,
    body: BotCommandUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    cmd = await db.scalar(select(BotCommand).where(BotCommand.id == command_id))
    if not cmd:
        raise HTTPException(status_code=404, detail="Comando não encontrado")

    updates = body.model_dump(exclude_unset=True)

    # Normaliza o nome caso esteja sendo alterado
    if "command" in updates and updates["command"] is not None:
        new_name = updates["command"].strip().lstrip("/").lower()
        if not new_name:
            raise HTTPException(status_code=400, detail="Nome do comando não pode ser vazio")
        # Checa duplicidade se o nome mudou
        if new_name != cmd.command:
            dup = await db.scalar(
                select(BotCommand).where(
                    BotCommand.command == new_name,
                    BotCommand.id != command_id,
                )
            )
            if dup:
                raise HTTPException(
                    status_code=400,
                    detail=f"Já existe outro comando com o nome '{new_name}'",
                )
        updates["command"] = new_name

    # Validação do botão WebApp quando ativado
    will_have_button = updates.get("has_webapp_button", cmd.has_webapp_button)
    if will_have_button:
        button_text = updates.get("button_text", cmd.button_text)
        webapp_url = updates.get("webapp_url", cmd.webapp_url)
        if not button_text or not webapp_url:
            raise HTTPException(
                status_code=400,
                detail="Botão WebApp exige button_text e webapp_url preenchidos",
            )
        if not webapp_url.startswith("https://"):
            raise HTTPException(
                status_code=400,
                detail="webapp_url precisa ser HTTPS (exigência do Telegram)",
            )

    for field, value in updates.items():
        setattr(cmd, field, value)

    cmd.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cmd)
    return cmd


@router.delete("/bot/commands/{command_id}", tags=["Bot Commands"])
async def delete_bot_command(
    command_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    cmd = await db.scalar(select(BotCommand).where(BotCommand.id == command_id))
    if not cmd:
        raise HTTPException(status_code=404, detail="Comando não encontrado")
    if cmd.is_default:
        raise HTTPException(
            status_code=400,
            detail="Comandos padrão não podem ser removidos — desative-o em vez de deletar",
        )
    await db.delete(cmd)
    await db.commit()
    return {"success": True, "message": f"Comando /{cmd.command} removido"}


@router.post("/bot/commands/reload", response_model=BotCommandReloadResponse, tags=["Bot Commands"])
async def reload_bot_commands(
    _: str = Depends(get_current_admin),
    bot=Depends(get_bot),
):
    """Força o bot a recarregar os handlers dinâmicos.

    Chamada pelo painel após qualquer alteração no CRUD acima para que
    as mudanças reflitam imediatamente no Telegram sem precisar de
    redeploy nem reiniciar o processo.
    """
    if not bot or not bot.is_running:
        return BotCommandReloadResponse(
            success=False,
            message="Bot não está conectado",
            total_loaded=0,
        )
    try:
        total = await bot.reload_commands()
        return BotCommandReloadResponse(
            success=True,
            message=f"{total} comando(s) recarregado(s) com sucesso",
            total_loaded=total,
        )
    except Exception as e:
        logger.error(f"Erro ao recarregar comandos do bot: {e}")
        return BotCommandReloadResponse(
            success=False,
            message=f"Erro: {e}",
            total_loaded=0,
        )


# =====================================================
#  SOURCE CHANNEL (Canal Central)
# =====================================================

@router.get("/source-channel", response_model=SourceChannelResponse, tags=["Canal Central"])
async def get_source_channel(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    source = await db.scalar(select(SourceChannel).limit(1))
    if not source:
        raise HTTPException(status_code=404, detail="Canal central não configurado")
    return source


@router.put("/source-channel", response_model=SourceChannelResponse, tags=["Canal Central"])
async def update_source_channel(
    body: SourceChannelUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    source = await db.scalar(select(SourceChannel).limit(1))
    if source:
        source.telegram_id = body.telegram_id
        source.updated_at = datetime.now(timezone.utc)
    else:
        source = SourceChannel(telegram_id=body.telegram_id)
        db.add(source)

    await db.commit()
    await db.refresh(source)
    return source


@router.post("/source-channel/verify", response_model=SourceChannelVerifyResponse, tags=["Canal Central"])
async def verify_source_channel(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
    bot=Depends(get_bot),
):
    source = await db.scalar(select(SourceChannel).limit(1))
    if not source:
        return SourceChannelVerifyResponse(
            success=False, bot_is_admin=False, can_post=False,
            can_delete=False, can_pin=False, message="Canal central não configurado"
        )

    result = await bot.verify_channel(source.telegram_id)

    # Atualizar no banco
    source.name = result.get("name")
    source.bot_is_admin = result["bot_is_admin"]
    await db.commit()

    return SourceChannelVerifyResponse(
        success=result["success"],
        name=result.get("name"),
        bot_is_admin=result["bot_is_admin"],
        can_post=result["can_post"],
        can_delete=result["can_delete"],
        can_pin=result["can_pin"],
        message=result["message"],
    )


@router.delete("/source-channel", tags=["Canal Central"])
async def delete_source_channel(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    source = await db.scalar(select(SourceChannel).limit(1))
    if not source:
        raise HTTPException(status_code=404, detail="Canal central não encontrado")
    await db.delete(source)
    await db.commit()
    return {"message": "Canal central removido"}


# =====================================================
#  CHANNELS (Canais/Grupos Participantes)
# =====================================================

@router.get("/channels", response_model=list[ChannelResponse], tags=["Canais"])
async def list_channels(
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    query = select(Channel).order_by(Channel.name)
    if status:
        query = query.where(Channel.status == status)
    if type:
        query = query.where(Channel.type == type)
    if search:
        query = query.where(Channel.name.ilike(f"%{search}%"))

    result = await db.execute(query)
    return result.scalars().all()


@router.post("/channels", response_model=ChannelResponse, tags=["Canais"])
async def create_channel(
    body: ChannelCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    # Verificar se já existe
    existing = await db.scalar(
        select(Channel).where(Channel.telegram_id == body.telegram_id)
    )
    if existing:
        raise HTTPException(status_code=409, detail="Canal já cadastrado")

    channel = Channel(
        telegram_id=body.telegram_id,
        name=body.name,
        link=body.link,
        type=body.type,
        added_by="manual",
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return channel


@router.put("/channels/{channel_id}", response_model=ChannelResponse, tags=["Canais"])
async def update_channel(
    channel_id: int,
    body: ChannelUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    channel = await db.get(Channel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Canal não encontrado")

    if body.name is not None:
        channel.name = body.name
    if body.link is not None:
        channel.link = body.link
    if body.type is not None:
        channel.type = body.type
    if body.status is not None:
        channel.status = body.status
        if body.status == "active":
            channel.error_reason = None

    channel.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(channel)
    return channel


@router.delete("/channels/{channel_id}", tags=["Canais"])
async def delete_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    channel = await db.get(Channel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Canal não encontrado")
    await db.delete(channel)
    await db.commit()
    return {"message": f"Canal {channel.name} removido"}


@router.post("/channels/{channel_id}/verify", response_model=ChannelVerifyResponse, tags=["Canais"])
async def verify_single_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
    bot=Depends(get_bot),
):
    channel = await db.get(Channel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Canal não encontrado")

    result = await bot.verify_channel(channel.telegram_id)

    # Atualizar no banco
    channel.bot_is_admin = result["bot_is_admin"]
    if result.get("name"):
        channel.name = result["name"]
    if result["member_count"]:
        channel.member_count = result["member_count"]
    channel.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return ChannelVerifyResponse(
        success=result["success"],
        telegram_id=channel.telegram_id,
        bot_is_admin=result["bot_is_admin"],
        can_post=result["can_post"],
        can_delete=result["can_delete"],
        can_pin=result["can_pin"],
        member_count=result["member_count"],
        message=result["message"],
    )


@router.post("/channels/bulk-verify", response_model=BulkVerifyResponse, tags=["Canais"])
async def bulk_verify_channels(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
    bot=Depends(get_bot),
):
    result = await db.execute(
        select(Channel).where(Channel.status == "active")
    )
    channels = result.scalars().all()

    results = []
    verified = 0
    errors = 0

    for ch in channels:
        res = await bot.verify_channel(ch.telegram_id)
        ch.bot_is_admin = res["bot_is_admin"]
        if res.get("name"):
            ch.name = res["name"]
        if res["member_count"]:
            ch.member_count = res["member_count"]

        r = ChannelVerifyResponse(
            success=res["success"],
            telegram_id=ch.telegram_id,
            bot_is_admin=res["bot_is_admin"],
            can_post=res["can_post"],
            can_delete=res["can_delete"],
            can_pin=res["can_pin"],
            member_count=res["member_count"],
            message=res["message"],
        )
        results.append(r)

        if res["success"]:
            verified += 1
        else:
            errors += 1

    await db.commit()

    return BulkVerifyResponse(
        total=len(channels),
        verified=verified,
        errors=errors,
        results=results,
    )


# =====================================================
#  DISPATCH CONFIG
# =====================================================

@router.get("/dispatch/config", response_model=DispatchConfigResponse, tags=["Disparo"])
async def get_dispatch_config(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    config = await db.scalar(select(DispatchConfig).limit(1))
    if not config:
        # Criar config padrão
        config = DispatchConfig()
        db.add(config)
        await db.commit()
        await db.refresh(config)
    return config


@router.put("/dispatch/config", response_model=DispatchConfigResponse, tags=["Disparo"])
async def update_dispatch_config(
    body: DispatchConfigUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    config = await db.scalar(select(DispatchConfig).limit(1))
    if not config:
        config = DispatchConfig()
        db.add(config)

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(config, field, value)

    config.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(config)
    return config


@router.post("/dispatch/trigger", response_model=TriggerResponse, tags=["Disparo"])
async def trigger_dispatch(
    _: str = Depends(get_current_admin),
    bot=Depends(get_bot),
):
    if not bot.is_running:
        return TriggerResponse(success=False, message="Bot não está conectado")

    log_id = await bot.dispatch("manual")
    if log_id:
        return TriggerResponse(success=True, message="Disparo realizado", dispatch_log_id=log_id)
    return TriggerResponse(success=False, message="Disparo falhou — verifique as configurações")


@router.post("/dispatch/cleanup", response_model=TriggerResponse, tags=["Disparo"])
async def trigger_cleanup(
    _: str = Depends(get_current_admin),
    bot=Depends(get_bot),
):
    if not bot.is_running:
        return TriggerResponse(success=False, message="Bot não está conectado")

    result = await bot.cleanup()
    return TriggerResponse(
        success=True,
        message=f"Apagadas: {result['deleted']} | Falhas: {result['failed']}",
    )


# =====================================================
#  DISPATCH HISTORY
# =====================================================

@router.get("/dispatch/history", response_model=list[DispatchLogResponse], tags=["Histórico"])
async def get_dispatch_history(
    limit: int = Query(20, le=100),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    result = await db.execute(
        select(DispatchLog).order_by(DispatchLog.started_at.desc()).limit(limit)
    )
    return result.scalars().all()


@router.get("/dispatch/history/{log_id}", response_model=DispatchLogDetailResponse, tags=["Histórico"])
async def get_dispatch_detail(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    result = await db.execute(
        select(DispatchLog)
        .where(DispatchLog.id == log_id)
        .options(selectinload(DispatchLog.sent_messages))
    )
    log = result.scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=404, detail="Log não encontrado")
    return log


# =====================================================
#  METRICS
# =====================================================

@router.get("/metrics/members", response_model=list[ChannelMetricsResponse], tags=["Métricas"])
async def get_member_metrics(
    days: int = Query(30, le=90),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Todos os canais
    channels_result = await db.execute(select(Channel).order_by(Channel.name))
    channels = channels_result.scalars().all()

    response = []
    for ch in channels:
        # Histórico
        hist_result = await db.execute(
            select(MemberCountHistory)
            .where(
                MemberCountHistory.channel_telegram_id == ch.telegram_id,
                MemberCountHistory.recorded_at >= since,
            )
            .order_by(MemberCountHistory.recorded_at)
        )
        history = [
            MemberCountEntry(member_count=h.member_count, recorded_at=h.recorded_at)
            for h in hist_result.scalars().all()
        ]

        response.append(ChannelMetricsResponse(
            telegram_id=ch.telegram_id,
            name=ch.name,
            current_count=ch.member_count,
            history=history,
        ))

    return response


@router.get("/metrics/members/{channel_telegram_id}", response_model=ChannelMetricsResponse, tags=["Métricas"])
async def get_channel_metrics(
    channel_telegram_id: int,
    days: int = Query(30, le=90),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    channel = await db.scalar(
        select(Channel).where(Channel.telegram_id == channel_telegram_id)
    )
    if not channel:
        raise HTTPException(status_code=404, detail="Canal não encontrado")

    since = datetime.now(timezone.utc) - timedelta(days=days)
    hist_result = await db.execute(
        select(MemberCountHistory)
        .where(
            MemberCountHistory.channel_telegram_id == channel_telegram_id,
            MemberCountHistory.recorded_at >= since,
        )
        .order_by(MemberCountHistory.recorded_at)
    )
    history = [
        MemberCountEntry(member_count=h.member_count, recorded_at=h.recorded_at)
        for h in hist_result.scalars().all()
    ]

    return ChannelMetricsResponse(
        telegram_id=channel.telegram_id,
        name=channel.name,
        current_count=channel.member_count,
        history=history,
    )


# =====================================================
#  TOP GROWTH — Ranking de crescimento pós-disparo
# =====================================================

async def _compute_top_growth(db: AsyncSession, limit: int = 5) -> TopGrowthResponse:
    """
    Calcula o ranking de canais que mais cresceram desde o último disparo.

    Lógica:
    1. Busca o último DispatchLog.
    2. Para cada canal que recebeu a mensagem (SentMessage sem erro),
       busca o registro de MemberCountHistory mais próximo ANTES do
       disparo para obter members_before.
    3. Compara com Channel.member_count atual (members_now).
    4. Calcula o delta e retorna ordenado por crescimento.
    5. Determina se o disparo ainda está "ao vivo" (mensagens ainda
       não foram apagadas = limpeza não ocorreu).
    """
    # 1. Último disparo
    last_dispatch = await db.scalar(
        select(DispatchLog).order_by(DispatchLog.started_at.desc()).limit(1)
    )
    if not last_dispatch:
        return TopGrowthResponse()

    dispatch_time = last_dispatch.started_at

    # 2. Canais que receberam a mensagem com sucesso neste disparo
    sent_result = await db.execute(
        select(SentMessage).where(
            SentMessage.dispatch_log_id == last_dispatch.id,
            SentMessage.message_id > 0,  # sem erro
            SentMessage.error.is_(None),
        )
    )
    sent_messages = sent_result.scalars().all()

    if not sent_messages:
        return TopGrowthResponse(
            dispatch_id=last_dispatch.id,
            dispatch_started_at=dispatch_time,
        )

    # Verificar se a limpeza já ocorreu (alguma msg foi deletada)
    any_deleted = any(msg.deleted for msg in sent_messages)
    is_live = not any_deleted

    # 3. Para cada canal, calcular crescimento
    growth_entries = []
    for msg in sent_messages:
        ch_tid = msg.channel_telegram_id

        # Buscar contagem ANTES do disparo (registro mais recente anterior ao disparo)
        before_record = await db.scalar(
            select(MemberCountHistory.member_count)
            .where(
                MemberCountHistory.channel_telegram_id == ch_tid,
                MemberCountHistory.recorded_at <= dispatch_time,
            )
            .order_by(MemberCountHistory.recorded_at.desc())
            .limit(1)
        )

        # Se não há registro anterior, tenta pegar o primeiro registro
        # depois do disparo como fallback (canal pode ter sido adicionado
        # junto com o disparo e a primeira coleta ocorreu logo depois)
        if before_record is None:
            before_record = await db.scalar(
                select(MemberCountHistory.member_count)
                .where(
                    MemberCountHistory.channel_telegram_id == ch_tid,
                )
                .order_by(MemberCountHistory.recorded_at.asc())
                .limit(1)
            )

        if before_record is None:
            continue  # sem dados de histórico, pular

        # Contagem atual do canal
        channel = await db.scalar(
            select(Channel).where(Channel.telegram_id == ch_tid)
        )
        if not channel:
            continue

        members_now = channel.member_count
        members_before = before_record
        growth = members_now - members_before
        growth_pct = (growth / members_before * 100) if members_before > 0 else 0.0

        growth_entries.append(TopGrowthEntry(
            telegram_id=ch_tid,
            name=msg.channel_name or channel.name,
            members_before=members_before,
            members_now=members_now,
            growth=growth,
            growth_percent=round(growth_pct, 2),
        ))

    # 4. Ordenar por crescimento absoluto (desc) e limitar
    growth_entries.sort(key=lambda x: x.growth, reverse=True)
    growth_entries = growth_entries[:limit]

    return TopGrowthResponse(
        dispatch_id=last_dispatch.id,
        dispatch_started_at=dispatch_time,
        cleanup_done=any_deleted,
        is_live=is_live,
        channels=growth_entries,
    )


@router.get("/metrics/top-growth", response_model=TopGrowthResponse, tags=["Métricas"])
async def get_top_growth(
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    """
    Retorna o ranking de canais que mais cresceram desde o último disparo.
    Aceita ?limit=5 (dashboard) ou ?limit=10 (métricas).
    """
    return await _compute_top_growth(db, limit=limit)


# =====================================================
#  DASHBOARD
# =====================================================

@router.get("/dashboard/summary", response_model=DashboardSummary, tags=["Dashboard"])
async def get_dashboard_summary(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_admin),
    bot=Depends(get_bot),
):
    total = await db.scalar(select(func.count(Channel.id)))
    active = await db.scalar(
        select(func.count(Channel.id)).where(Channel.status == "active")
    )
    error_channels = await db.scalar(
        select(func.count(Channel.id)).where(Channel.status == "error")
    )
    total_members = await db.scalar(
        select(func.coalesce(func.sum(Channel.member_count), 0))
    )

    # Total de disparos realizados
    total_dispatches = await db.scalar(
        select(func.count(DispatchLog.id))
    )

    # Taxa de sucesso dos disparos (últimos 30 dias)
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    recent_dispatches_result = await db.execute(
        select(DispatchLog).where(DispatchLog.started_at >= thirty_days_ago)
    )
    recent_dispatches = recent_dispatches_result.scalars().all()
    if recent_dispatches:
        total_sent = sum(d.success_count + d.fail_count for d in recent_dispatches)
        total_success = sum(d.success_count for d in recent_dispatches)
        success_rate = round((total_success / total_sent * 100) if total_sent > 0 else 0.0, 1)
    else:
        success_rate = 0.0

    # Crescimento da rede nos últimos 7 dias
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)

    # Soma dos membros 7 dias atrás (pegar o registro mais antigo de cada canal
    # dentro da janela de 7 dias)
    channels_result = await db.execute(select(Channel))
    all_channels = channels_result.scalars().all()
    members_7d_ago = 0
    for ch in all_channels:
        oldest_in_window = await db.scalar(
            select(MemberCountHistory.member_count)
            .where(
                MemberCountHistory.channel_telegram_id == ch.telegram_id,
                MemberCountHistory.recorded_at >= seven_days_ago,
            )
            .order_by(MemberCountHistory.recorded_at.asc())
            .limit(1)
        )
        if oldest_in_window is not None:
            members_7d_ago += oldest_in_window
        else:
            # Se não tem registro na janela, usar o registro mais recente
            # anterior à janela como base
            fallback = await db.scalar(
                select(MemberCountHistory.member_count)
                .where(
                    MemberCountHistory.channel_telegram_id == ch.telegram_id,
                    MemberCountHistory.recorded_at < seven_days_ago,
                )
                .order_by(MemberCountHistory.recorded_at.desc())
                .limit(1)
            )
            if fallback is not None:
                members_7d_ago += fallback
            else:
                # Sem histórico nenhum — usa o count atual como base (crescimento = 0)
                members_7d_ago += ch.member_count

    network_growth_7d = (total_members or 0) - members_7d_ago

    # Último disparo
    last_dispatch = await db.scalar(
        select(DispatchLog).order_by(DispatchLog.started_at.desc()).limit(1)
    )

    # Info do próximo disparo
    config = await db.scalar(select(DispatchConfig).limit(1))
    next_info = None
    if config and config.is_active:
        # Traduzir dias da semana para português
        day_map = {
            "mon": "Seg", "tue": "Ter", "wed": "Qua",
            "thu": "Qui", "fri": "Sex", "sat": "Sáb", "sun": "Dom",
        }
        days_raw = config.schedule_days or ""
        days_pt = ", ".join(
            day_map.get(d.strip().lower(), d.strip())
            for d in days_raw.split(",") if d.strip()
        )
        next_info = f"{days_pt} às {config.schedule_hour}:{config.schedule_minute:02d}"

    # Bot status
    bot_config = await db.scalar(select(BotConfig).limit(1))

    # Top growth para o dashboard (top 5)
    top_growth = await _compute_top_growth(db, limit=5)

    return DashboardSummary(
        total_channels=total or 0,
        active_channels=active or 0,
        error_channels=error_channels or 0,
        total_members=total_members or 0,
        total_dispatches=total_dispatches or 0,
        success_rate=success_rate,
        network_growth_7d=network_growth_7d,
        last_dispatch=DispatchLogResponse.model_validate(last_dispatch) if last_dispatch else None,
        next_dispatch_info=next_info,
        bot_connected=bot.is_running,
        bot_username=bot_config.bot_username if bot_config else None,
        top_growth=top_growth,
    )