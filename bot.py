"""
Bot Telegram — lógica de disparo, limpeza, verificação e métricas.
Roda junto com o FastAPI no mesmo processo.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    Channel, SourceChannel, DispatchConfig, DispatchLog,
    SentMessage, MemberCountHistory, BotConfig,
)
from config import get_settings

logger = logging.getLogger(__name__)


class TelegramBot:
    """Gerencia o bot do Telegram e toda a lógica de disparo."""

    def __init__(self, session_factory):
        self.session_factory = session_factory  # async_sessionmaker
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self._polling_task: Optional[asyncio.Task] = None

    # ===================== LIFECYCLE =====================

    async def start(self, token: str):
        """Inicializa o bot com o token fornecido."""
        if self.bot:
            await self.stop()

        self.bot = Bot(token=token, parse_mode=ParseMode.HTML)
        self.dp = Dispatcher()
        self._setup_handlers()

        # Inicia polling em background
        self._polling_task = asyncio.create_task(self._run_polling())
        logger.info("🤖 Bot Telegram iniciado (polling)")

        # Notifica admin
        settings = get_settings()
        if settings.ADMIN_CHAT_ID:
            try:
                await self.bot.send_message(
                    settings.ADMIN_CHAT_ID,
                    "🟢 <b>Bot Listas v2 Online!</b>\n\n"
                    "Sistema de Divulgação Cruzada iniciado com sucesso.\n"
                    "Painel web disponível para gerenciamento."
                )
            except Exception as e:
                logger.warning(f"Não foi possível notificar admin: {e}")

    async def stop(self):
        """Para o bot."""
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None

        if self.bot:
            await self.bot.session.close()
            self.bot = None
            self.dp = None
            logger.info("🔴 Bot Telegram parado")

    async def _run_polling(self):
        """Executa o polling do bot."""
        try:
            await self.dp.start_polling(self.bot)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Erro no polling do bot: {e}")

    @property
    def is_running(self) -> bool:
        return self.bot is not None

    # ===================== VERIFICAÇÕES =====================

    async def verify_bot_token(self, token: str) -> Tuple[bool, Optional[str]]:
        """Testa um token e retorna (success, username)."""
        try:
            temp_bot = Bot(token=token)
            me = await temp_bot.get_me()
            await temp_bot.session.close()
            return True, me.username
        except Exception as e:
            logger.error(f"Token inválido: {e}")
            return False, None

    async def verify_channel(self, channel_telegram_id: int) -> dict:
        """Verifica se o bot é admin em um canal e retorna permissões."""
        result = {
            "success": False,
            "bot_is_admin": False,
            "can_post": False,
            "can_delete": False,
            "can_pin": False,
            "member_count": 0,
            "name": None,
            "message": "",
        }

        if not self.bot:
            result["message"] = "Bot não está conectado"
            return result

        try:
            chat = await self.bot.get_chat(channel_telegram_id)
            result["name"] = chat.title

            # CORREÇÃO: o objeto Chat retornado por get_chat() NÃO tem o
            # atributo "members_count" no aiogram 3.x — essa contagem
            # precisa ser buscada com uma chamada separada da API do
            # Telegram (get_chat_member_count). O atributo antigo era de
            # versões anteriores da lib / de outras bibliotecas Telegram.
            try:
                result["member_count"] = await self.bot.get_chat_member_count(channel_telegram_id)
            except Exception as count_err:
                logger.warning(f"Não foi possível obter member_count de {channel_telegram_id}: {count_err}")
                result["member_count"] = 0

            member = await self.bot.get_chat_member(channel_telegram_id, self.bot.id)
            if member.status in ("administrator", "creator"):
                result["bot_is_admin"] = True
                result["can_post"] = getattr(member, "can_post_messages", True)
                result["can_delete"] = getattr(member, "can_delete_messages", False)
                result["can_pin"] = getattr(member, "can_pin_messages", False)
                result["success"] = True
                result["message"] = "Bot é admin com permissões verificadas"
            else:
                result["message"] = "Bot não é administrador neste canal"

        except TelegramBadRequest as e:
            result["message"] = f"Canal não encontrado ou inacessível: {e}"
        except TelegramForbiddenError as e:
            result["message"] = f"Bot foi removido ou bloqueado: {e}"
        except Exception as e:
            result["message"] = f"Erro ao verificar: {e}"

        return result

    # ===================== DISPARO =====================

    async def dispatch(self, dispatch_type: str = "manual") -> Optional[int]:
        """
        Executa o disparo: encaminha a mensagem do canal central
        para os canais participantes (ou canal oficial).
        Retorna o dispatch_log_id ou None se falhar.
        """
        if not self.bot:
            logger.error("Bot não está conectado para disparo")
            return None

        settings = get_settings()

        async with self.session_factory() as session:
            # Buscar config de disparo
            config = await session.scalar(select(DispatchConfig).limit(1))
            if not config or not config.message_id:
                logger.error("Configuração de disparo não encontrada ou message_id não definido")
                await self._notify_admin("❌ <b>Disparo falhou:</b> message_id não configurado no painel.")
                return None

            # Buscar canal central
            source = await session.scalar(select(SourceChannel).limit(1))
            if not source:
                logger.error("Canal central não configurado")
                await self._notify_admin("❌ <b>Disparo falhou:</b> canal central não configurado.")
                return None

            # Determinar canais de destino
            if config.mode == "single_channel" and config.target_channel:
                # Modo canal oficial: encaminha apenas para um canal
                channels = [{"telegram_id": config.target_channel, "name": "Canal Oficial"}]
            else:
                # Modo broadcast: encaminha para todos os canais ativos
                result = await session.execute(
                    select(Channel).where(Channel.status == "active")
                )
                channels = [
                    {"telegram_id": ch.telegram_id, "name": ch.name}
                    for ch in result.scalars().all()
                ]

            if not channels:
                logger.warning("Nenhum canal ativo para disparo")
                await self._notify_admin("⚠️ <b>Disparo cancelado:</b> nenhum canal ativo configurado.")
                return None

            # Criar log de disparo
            log = DispatchLog(
                dispatch_type=dispatch_type,
                mode=config.mode,
                message_id=config.message_id,
                total_channels=len(channels),
            )
            session.add(log)
            await session.flush()  # para obter o log.id

            # Notificar admin sobre início
            await self._notify_admin(
                f"🚀 <b>DISPARO {dispatch_type.upper()} INICIADO!</b>\n\n"
                f"📊 Canais: {len(channels)}\n"
                f"📌 Mensagem ID: {config.message_id}\n"
                f"🔄 Modo: {config.mode}"
            )

            success = 0
            failed = 0
            deactivated = 0
            errors = []

            for ch in channels:
                try:
                    # Encaminhar mensagem
                    sent = await self.bot.forward_message(
                        chat_id=ch["telegram_id"],
                        from_chat_id=source.telegram_id,
                        message_id=config.message_id,
                    )

                    pinned = False
                    if config.auto_pin:
                        try:
                            await self.bot.pin_chat_message(
                                chat_id=ch["telegram_id"],
                                message_id=sent.message_id,
                                disable_notification=True,
                            )
                            pinned = True
                        except Exception as pin_err:
                            logger.warning(f"Não fixou em {ch['name']}: {pin_err}")

                    # Registrar mensagem enviada
                    session.add(SentMessage(
                        dispatch_log_id=log.id,
                        channel_telegram_id=ch["telegram_id"],
                        channel_name=ch["name"],
                        message_id=sent.message_id,
                        pinned=pinned,
                    ))

                    success += 1
                    logger.info(f"✅ Enviado para {ch['name']} ({ch['telegram_id']})")
                    await asyncio.sleep(0.5)

                except (TelegramBadRequest, TelegramForbiddenError) as e:
                    failed += 1
                    error_msg = self._parse_telegram_error(e)
                    errors.append(f"{ch['name']}: {error_msg}")

                    # Registrar erro
                    session.add(SentMessage(
                        dispatch_log_id=log.id,
                        channel_telegram_id=ch["telegram_id"],
                        channel_name=ch["name"],
                        message_id=0,
                        error=error_msg,
                    ))

                    # Desativar canal com erro
                    await session.execute(
                        update(Channel)
                        .where(Channel.telegram_id == ch["telegram_id"])
                        .values(status="error", error_reason=error_msg)
                    )
                    deactivated += 1
                    logger.warning(f"❌ Falha em {ch['name']}: {error_msg}")

                except Exception as e:
                    failed += 1
                    errors.append(f"{ch['name']}: {str(e)}")
                    session.add(SentMessage(
                        dispatch_log_id=log.id,
                        channel_telegram_id=ch["telegram_id"],
                        channel_name=ch["name"],
                        message_id=0,
                        error=str(e),
                    ))

            # Atualizar log
            log.success_count = success
            log.fail_count = failed
            log.deactivated = deactivated
            log.finished_at = datetime.now(timezone.utc)

            await session.commit()

            # Relatório
            report = (
                f"📊 <b>RELATÓRIO DE DISPARO</b>\n\n"
                f"✅ Sucesso: {success}\n"
                f"❌ Falhas: {failed}\n"
            )
            if deactivated:
                report += f"🛑 Desativados: {deactivated}\n"
            if errors:
                report += "\n<b>Erros:</b>\n"
                for err in errors[:10]:
                    report += f"• {err}\n"

            await self._notify_admin(report)
            return log.id

    # ===================== LIMPEZA =====================

    async def cleanup(self) -> dict:
        """
        Apaga as mensagens enviadas que ainda não foram deletadas.
        Desfixa antes de deletar.
        """
        if not self.bot:
            return {"deleted": 0, "failed": 0, "message": "Bot não conectado"}

        async with self.session_factory() as session:
            result = await session.execute(
                select(SentMessage).where(
                    SentMessage.deleted == False,
                    SentMessage.message_id > 0,
                )
            )
            pending = result.scalars().all()

            if not pending:
                return {"deleted": 0, "failed": 0, "message": "Nenhuma mensagem pendente"}

            await self._notify_admin(
                f"🗑️ <b>INICIANDO LIMPEZA</b>\n\n"
                f"📊 Mensagens pendentes: {len(pending)}"
            )

            deleted = 0
            failed = 0

            for msg in pending:
                try:
                    # Desfixar
                    if msg.pinned:
                        try:
                            await self.bot.unpin_chat_message(
                                chat_id=msg.channel_telegram_id,
                                message_id=msg.message_id,
                            )
                        except Exception:
                            pass

                    # Deletar
                    await self.bot.delete_message(
                        chat_id=msg.channel_telegram_id,
                        message_id=msg.message_id,
                    )
                    msg.deleted = True
                    deleted += 1
                    await asyncio.sleep(0.3)

                except Exception as e:
                    failed += 1
                    msg.error = str(e)
                    logger.warning(f"Falha ao apagar em {msg.channel_telegram_id}: {e}")

            await session.commit()

            report = (
                f"🗑️ <b>RELATÓRIO DE LIMPEZA</b>\n\n"
                f"✅ Apagadas: {deleted}\n"
                f"❌ Falhas: {failed}"
            )
            await self._notify_admin(report)

            return {"deleted": deleted, "failed": failed, "message": "Limpeza concluída"}

    # ===================== VERIFICAÇÃO PERIÓDICA =====================

    async def verify_active_channels(self):
        """Verifica acessibilidade dos canais ativos e desativa os inacessíveis."""
        if not self.bot:
            return

        logger.info("🔍 Verificação periódica de canais ativos")

        async with self.session_factory() as session:
            result = await session.execute(
                select(Channel).where(Channel.status == "active")
            )
            active = result.scalars().all()

            for ch in active:
                try:
                    await self.bot.get_chat(ch.telegram_id)
                except (TelegramBadRequest, TelegramForbiddenError) as e:
                    error_msg = self._parse_telegram_error(e)
                    ch.status = "error"
                    ch.error_reason = error_msg
                    logger.warning(f"Canal {ch.name} inacessível: {error_msg}")

                    await self._notify_admin(
                        f"🚨 <b>CANAL INACESSÍVEL</b>\n\n"
                        f"📢 {ch.name} (<code>{ch.telegram_id}</code>)\n"
                        f"Motivo: {error_msg}\n"
                        f"Status: desativado automaticamente"
                    )
                except Exception as e:
                    logger.error(f"Erro ao verificar {ch.name}: {e}")

                await asyncio.sleep(0.5)

            await session.commit()

    # ===================== COLETA DE MÉTRICAS =====================

    async def collect_member_counts(self):
        """Coleta contagem de membros de todos os canais e salva histórico."""
        if not self.bot:
            return

        logger.info("📈 Coletando contagem de membros")

        async with self.session_factory() as session:
            result = await session.execute(select(Channel))
            channels = result.scalars().all()

            for ch in channels:
                try:
                    # CORREÇÃO: mesmo bug do verify_channel — o Chat não
                    # tem o atributo members_count, é preciso usar
                    # get_chat_member_count() (chamada separada da API).
                    count = await self.bot.get_chat_member_count(ch.telegram_id)

                    ch.member_count = count

                    session.add(MemberCountHistory(
                        channel_telegram_id=ch.telegram_id,
                        member_count=count,
                    ))

                except Exception as e:
                    logger.warning(f"Não coletou membros de {ch.name}: {e}")

                await asyncio.sleep(0.1)

            await session.commit()

        logger.info("📈 Coleta de métricas concluída")

    # ===================== HANDLERS DO BOT =====================

    def _setup_handlers(self):
        """Configura os comandos do bot no Telegram."""

        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            await message.reply(
                "🤖 <b>Bot de Listas de Divulgação v2</b>\n\n"
                "Sistema automatizado de divulgação cruzada.\n"
                "Gerencie tudo pelo painel web!\n\n"
                "Comandos admin:\n"
                "/status - Status do sistema\n"
                "/disparo - Disparo manual\n"
                "/limpar - Limpeza manual"
            )

        @self.dp.message(Command("status"))
        async def cmd_status(message: types.Message):
            settings = get_settings()
            if str(message.from_user.id) != str(settings.ADMIN_CHAT_ID):
                return

            async with self.session_factory() as session:
                total = await session.scalar(select(func.count(Channel.id)))
                active = await session.scalar(
                    select(func.count(Channel.id)).where(Channel.status == "active")
                )
                config = await session.scalar(select(DispatchConfig).limit(1))

            # CORREÇÃO: o f-string original tinha
            # "{config.schedule_minute:02d if config else '00'}", que não
            # é uma expressão Python válida (format spec não aceita
            # condicional dessa forma) e gerava ValueError sempre que
            # /status era executado. Calculamos o minuto formatado antes,
            # em uma variável separada, e apenas o inserimos no f-string.
            minute_str = f"{config.schedule_minute:02d}" if config else "00"
            msg = (
                f"📊 <b>STATUS DO SISTEMA</b>\n\n"
                f"📡 Canais totais: {total}\n"
                f"✅ Canais ativos: {active}\n"
                f"📌 Message ID: {config.message_id if config else 'N/A'}\n"
                f"🔄 Modo: {config.mode if config else 'N/A'}\n"
                f"📅 Agenda: {config.schedule_days if config else 'N/A'} às {config.schedule_hour if config else 'N/A'}:{minute_str}"
            )
            await message.reply(msg)

        @self.dp.message(Command("disparo"))
        async def cmd_disparo(message: types.Message):
            settings = get_settings()
            if str(message.from_user.id) != str(settings.ADMIN_CHAT_ID):
                return
            await message.reply("🚀 Iniciando disparo manual...")
            await self.dispatch("manual")

        @self.dp.message(Command("limpar"))
        async def cmd_limpar(message: types.Message):
            settings = get_settings()
            if str(message.from_user.id) != str(settings.ADMIN_CHAT_ID):
                return
            await message.reply("🗑️ Iniciando limpeza manual...")
            result = await self.cleanup()
            await message.reply(
                f"✅ Apagadas: {result['deleted']} | ❌ Falhas: {result['failed']}"
            )

    # ===================== UTILITÁRIOS =====================

    async def _notify_admin(self, text: str):
        """Envia mensagem ao admin."""
        settings = get_settings()
        if not self.bot or not settings.ADMIN_CHAT_ID:
            return
        try:
            await self.bot.send_message(settings.ADMIN_CHAT_ID, text)
        except Exception as e:
            logger.error(f"Erro ao notificar admin: {e}")

    @staticmethod
    def _parse_telegram_error(e: Exception) -> str:
        """Converte erros do Telegram em mensagens legíveis."""
        msg = str(e).lower()
        if "chat not found" in msg:
            return "Canal/Grupo não encontrado (pode ter sido excluído)"
        elif "bot was blocked" in msg or "bot was kicked" in msg:
            return "Bot foi removido ou bloqueado"
        elif "user_deactivated" in msg:
            return "Conta do bot desativada"
        elif "not enough rights" in msg:
            return "Bot sem permissões suficientes"
        return str(e)