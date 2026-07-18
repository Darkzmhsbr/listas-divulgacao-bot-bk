"""
Schemas Pydantic para validação de request/response da API.
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel


# ===================== AUTH =====================

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class AdminResponse(BaseModel):
    id: int
    username: str


# ===================== BOT CONFIG =====================

class BotConfigUpdate(BaseModel):
    bot_token: str

class BotConfigResponse(BaseModel):
    id: int
    bot_username: Optional[str] = None
    is_active: bool
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class BotVerifyResponse(BaseModel):
    success: bool
    bot_username: Optional[str] = None
    message: str


# ===================== SOURCE CHANNEL =====================

class SourceChannelUpdate(BaseModel):
    telegram_id: int

class SourceChannelResponse(BaseModel):
    id: int
    telegram_id: int
    name: Optional[str] = None
    bot_is_admin: bool
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class SourceChannelVerifyResponse(BaseModel):
    success: bool
    name: Optional[str] = None
    bot_is_admin: bool
    can_post: bool
    can_delete: bool
    can_pin: bool
    message: str


# ===================== CHANNELS =====================

class ChannelCreate(BaseModel):
    telegram_id: int
    name: str
    link: Optional[str] = None
    type: str = "channel"

class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    link: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None

class ChannelResponse(BaseModel):
    id: int
    telegram_id: int
    name: str
    link: Optional[str] = None
    type: str
    status: str
    member_count: int
    bot_is_admin: bool
    added_by: str
    error_reason: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class ChannelVerifyResponse(BaseModel):
    success: bool
    telegram_id: int
    bot_is_admin: bool
    can_post: bool
    can_delete: bool
    can_pin: bool
    member_count: int
    message: str

class BulkVerifyResponse(BaseModel):
    total: int
    verified: int
    errors: int
    results: List[ChannelVerifyResponse]


# ===================== DISPATCH CONFIG =====================

class DispatchConfigUpdate(BaseModel):
    mode: Optional[str] = None
    target_channel: Optional[int] = None
    message_id: Optional[int] = None
    folder_link: Optional[str] = None
    schedule_days: Optional[str] = None
    schedule_hour: Optional[int] = None
    schedule_minute: Optional[int] = None
    cleanup_days: Optional[str] = None
    cleanup_hour: Optional[int] = None
    cleanup_minute: Optional[int] = None
    auto_delete: Optional[bool] = None
    auto_pin: Optional[bool] = None
    is_active: Optional[bool] = None

class DispatchConfigResponse(BaseModel):
    id: int
    mode: str
    target_channel: Optional[int] = None
    message_id: Optional[int] = None
    folder_link: Optional[str] = None
    schedule_days: str
    schedule_hour: int
    schedule_minute: int
    cleanup_days: str
    cleanup_hour: int
    cleanup_minute: int
    auto_delete: bool
    auto_pin: bool
    is_active: bool
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ===================== DISPATCH LOG =====================

class DispatchLogResponse(BaseModel):
    id: int
    dispatch_type: str
    mode: str
    message_id: int
    total_channels: int
    success_count: int
    fail_count: int
    deactivated: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class SentMessageResponse(BaseModel):
    id: int
    channel_telegram_id: int
    channel_name: Optional[str] = None
    message_id: int
    pinned: bool
    deleted: bool
    error: Optional[str] = None
    sent_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class DispatchLogDetailResponse(DispatchLogResponse):
    sent_messages: List[SentMessageResponse] = []


# ===================== METRICS =====================

class MemberCountEntry(BaseModel):
    member_count: int
    recorded_at: datetime

class ChannelMetricsResponse(BaseModel):
    telegram_id: int
    name: str
    current_count: int
    history: List[MemberCountEntry] = []


# ===================== TOP GROWTH (NOVO) =====================

class TopGrowthEntry(BaseModel):
    """Um canal/grupo no ranking de crescimento pós-disparo."""
    telegram_id: int
    name: str
    members_before: int
    members_now: int
    growth: int
    growth_percent: float

class TopGrowthResponse(BaseModel):
    """Resposta completa do ranking de crescimento do último disparo."""
    dispatch_id: Optional[int] = None
    dispatch_started_at: Optional[datetime] = None
    cleanup_done: bool = False
    is_live: bool = False
    channels: List[TopGrowthEntry] = []


# ===================== DASHBOARD =====================

class DashboardSummary(BaseModel):
    total_channels: int
    active_channels: int
    error_channels: int = 0
    total_members: int
    total_dispatches: int = 0
    success_rate: float = 0.0
    network_growth_7d: int = 0
    last_dispatch: Optional[DispatchLogResponse] = None
    next_dispatch_info: Optional[str] = None
    bot_connected: bool
    bot_username: Optional[str] = None
    top_growth: Optional[TopGrowthResponse] = None


# ===================== TRIGGER RESPONSE =====================

class TriggerResponse(BaseModel):
    success: bool
    message: str
    dispatch_log_id: Optional[int] = None