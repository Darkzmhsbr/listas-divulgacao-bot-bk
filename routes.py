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
    total_members = await db.scalar(
        select(func.coalesce(func.sum(Channel.member_count), 0))
    )

    # Último disparo
    last_dispatch = await db.scalar(
        select(DispatchLog).order_by(DispatchLog.started_at.desc()).limit(1)
    )

    # Info do próximo disparo
    config = await db.scalar(select(DispatchConfig).limit(1))
    next_info = None
    if config and config.is_active:
        next_info = f"{config.schedule_days} às {config.schedule_hour}:{config.schedule_minute:02d}"

    # Bot status
    bot_config = await db.scalar(select(BotConfig).limit(1))

    return DashboardSummary(
        total_channels=total or 0,
        active_channels=active or 0,
        total_members=total_members or 0,
        last_dispatch=DispatchLogResponse.model_validate(last_dispatch) if last_dispatch else None,
        next_dispatch_info=next_info,
        bot_connected=bot.is_running,
        bot_username=bot_config.bot_username if bot_config else None,
    )
