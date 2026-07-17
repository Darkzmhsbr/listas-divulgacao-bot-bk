"""
Modelos do banco de dados (SQLAlchemy Async).
Todas as tabelas do sistema em um único arquivo.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Boolean,
    DateTime, ForeignKey
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# ===================== ADMIN =====================

class Admin(Base):
    __tablename__ = "admins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ===================== BOT CONFIG =====================

class BotConfig(Base):
    __tablename__ = "bot_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_token = Column(Text, nullable=False)
    bot_username = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ===================== CANAL CENTRAL (SOURCE) =====================

class SourceChannel(Base):
    __tablename__ = "source_channel"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False)
    name = Column(String(200), nullable=True)
    bot_is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ===================== CANAIS/GRUPOS PARTICIPANTES =====================

class Channel(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    link = Column(String(300), nullable=True)
    type = Column(String(20), default="channel")  # 'channel' ou 'group'
    status = Column(String(20), default="active")  # 'active', 'inactive', 'error'
    member_count = Column(Integer, default=0)
    bot_is_admin = Column(Boolean, default=False)
    added_by = Column(String(50), default="manual")  # 'manual', 'site_sync', 'bot_command'
    error_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ===================== CONFIG DE DISPARO =====================

class DispatchConfig(Base):
    __tablename__ = "dispatch_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(20), default="broadcast")  # 'broadcast' ou 'single_channel'
    target_channel = Column(BigInteger, nullable=True)  # usado no modo 'single_channel'
    message_id = Column(Integer, nullable=True)  # ID da mensagem no canal central
    folder_link = Column(Text, nullable=True)  # link da pasta do Telegram
    schedule_days = Column(String(50), default="mon,wed,fri")
    schedule_hour = Column(Integer, default=20)
    schedule_minute = Column(Integer, default=0)
    cleanup_days = Column(String(50), default="tue,thu,sat")
    cleanup_hour = Column(Integer, default=12)
    cleanup_minute = Column(Integer, default=0)
    auto_delete = Column(Boolean, default=True)
    auto_pin = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ===================== HISTÓRICO DE DISPAROS =====================

class DispatchLog(Base):
    __tablename__ = "dispatch_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dispatch_type = Column(String(20), nullable=False)  # 'auto' ou 'manual'
    mode = Column(String(20), nullable=False)
    message_id = Column(Integer, nullable=False)
    total_channels = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)
    deactivated = Column(Integer, default=0)
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime(timezone=True), nullable=True)

    sent_messages = relationship("SentMessage", back_populates="dispatch_log")


# ===================== MENSAGENS ENVIADAS =====================

class SentMessage(Base):
    __tablename__ = "sent_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dispatch_log_id = Column(Integer, ForeignKey("dispatch_log.id"), nullable=True)
    channel_telegram_id = Column(BigInteger, nullable=False)
    channel_name = Column(String(200), nullable=True)
    message_id = Column(Integer, nullable=False)  # message_id da msg encaminhada
    pinned = Column(Boolean, default=False)
    deleted = Column(Boolean, default=False)
    error = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    dispatch_log = relationship("DispatchLog", back_populates="sent_messages")


# ===================== HISTÓRICO DE MEMBROS =====================

class MemberCountHistory(Base):
    __tablename__ = "member_count_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_telegram_id = Column(BigInteger, nullable=False)
    member_count = Column(Integer, nullable=False)
    recorded_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
