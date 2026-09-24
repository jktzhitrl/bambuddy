"""Telegram-Knoepfe: Freigeben/Verwerfen und "Platte ist frei" direkt im Chat.

Nutzt die Telegram-Kanaele, die in den Lager-Autodruck-Einstellungen fuer
Meldungen ausgewaehlt sind (Bot-Token und Chat-ID aus Bambuddys Kanal). Die
Knopfdruecke holt Bambuddy selbst per Long-Polling (getUpdates) ab - es
braucht also keine oeffentliche Adresse und keinen Webhook.

Sicherheit: angenommen wird ein Knopfdruck nur aus dem Chat, der im Kanal
eingetragen ist. Wer in diesem Chat ist, darf freigeben - in einer Gruppe also
jedes Mitglied.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services.lager_autodruck import konfig, melden

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
# So lange haelt Telegram eine getUpdates-Anfrage offen, wenn nichts passiert.
POLL_SEKUNDEN = 30
# Pause, wenn keine Knoepfe eingerichtet sind oder Telegram nicht erreichbar ist.
PAUSE_SEKUNDEN = 60


@dataclass(frozen=True)
class Bot:
    provider_id: int
    token: str
    chat_id: str
    thread_id: int | None = None


async def bots(db: AsyncSession, k: konfig.Konfig) -> list[Bot]:
    """Telegram-Kanaele unter den gewaehlten Meldungs-Kanaelen."""
    ergebnis = []
    for kanal in await melden.kanaele(db, k.melden_an):
        if kanal.provider_type != "telegram":
            continue
        try:
            daten = json.loads(kanal.config) if isinstance(kanal.config, str) else (kanal.config or {})
        except ValueError:
            continue
        token = str(daten.get("bot_token") or "").strip()
        chat_id = str(daten.get("chat_id") or "").strip()
        if not token or not chat_id:
            continue
        thread = str(daten.get("message_thread_id") or "").strip()
        ergebnis.append(Bot(kanal.id, token, chat_id, int(thread) if thread.isdigit() else None))
    return ergebnis


async def knopf_bots(db: AsyncSession, k: konfig.Konfig) -> list[Bot]:
    """Bots, ueber die Nachrichten mit Knoepfen gehen (leer = Knoepfe aus)."""
    if not k.telegram_knoepfe:
        return []
    return await bots(db, k)


def knoepfe(*zeilen: list[tuple[str, str]]) -> dict:
    """[(Beschriftung, callback_data), ...] je Zeile -> reply_markup."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in zeile] for zeile in zeilen]}


class TelegramFehler(Exception):
    pass


async def _aufruf(client: httpx.AsyncClient, bot: Bot, methode: str, *, timeout: float = 20.0, **kwargs: Any) -> Any:
    try:
        antwort = await client.post(f"{API}/bot{bot.token}/{methode}", timeout=timeout, **kwargs)
    except httpx.HTTPError as e:
        # Die URL enthaelt das Token - daher nur den Fehlertyp melden.
        raise TelegramFehler(f"Telegram nicht erreichbar ({type(e).__name__})") from None
    try:
        daten = antwort.json()
    except ValueError:
        raise TelegramFehler(f"Telegram antwortete {antwort.status_code}") from None
    if not daten.get("ok"):
        raise TelegramFehler(f"Telegram: {daten.get('description') or antwort.status_code}")
    return daten.get("result")


async def nachricht(bot: Bot, text: str, markup: dict | None = None, foto: bytes | None = None) -> bool:
    """Schickt eine Nachricht (mit Foto, wenn vorhanden). Fehler nur ins Log."""
    felder: dict[str, Any] = {"chat_id": bot.chat_id}
    if bot.thread_id is not None:
        felder["message_thread_id"] = bot.thread_id
    try:
        async with httpx.AsyncClient() as client:
            if foto:
                felder["caption"] = text[:1024]
                if markup:
                    felder["reply_markup"] = json.dumps(markup)
                await _aufruf(
                    client, bot, "sendPhoto", data=felder, files={"photo": ("platte.jpg", foto, "image/jpeg")}
                )
            else:
                felder["text"] = text
                if markup:
                    felder["reply_markup"] = markup
                await _aufruf(client, bot, "sendMessage", json=felder)
        return True
    except TelegramFehler as e:
        logger.warning("Lager-Autodruck: Telegram-Nachricht nicht gesendet: %s", e)
        return False


# (callback_data, wer) -> Antwort fuer den Chat
Ausfuehren = Callable[[str, str], Awaitable[str]]


class Rueckkanal:
    """Holt Knopfdruecke von Telegram ab und fuehrt sie aus."""

    def __init__(self, ausfuehren: Ausfuehren) -> None:
        self._ausfuehren = ausfuehren
        self._offset: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._gewarnt: set[str] = set()

    def start(self) -> None:
        if self._task is None:
            from backend.app.core.tasks import spawn_background_task

            self._task = spawn_background_task(self._schleife(), name="lager-autodruck-telegram")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _schleife(self) -> None:
        from backend.app.core import database

        await asyncio.sleep(30)
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    async with database.async_session() as db:
                        k = await konfig.laden(db)
                        liste = await knopf_bots(db, k)
                    # Ein Token kann in mehreren Kanaelen stecken - nur einmal abholen.
                    je_token: dict[str, list[Bot]] = {}
                    for b in liste:
                        je_token.setdefault(b.token, []).append(b)
                    if not je_token:
                        await asyncio.sleep(PAUSE_SEKUNDEN)
                        continue
                    ergebnisse = await asyncio.gather(
                        *(self.abholen(client, gruppe) for gruppe in je_token.values()), return_exceptions=True
                    )
                    if all(e is False or isinstance(e, BaseException) for e in ergebnisse):
                        await asyncio.sleep(PAUSE_SEKUNDEN)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - die Schleife darf nie sterben
                    logger.exception("Lager-Autodruck: Telegram-Rueckkanal fehlgeschlagen")
                    await asyncio.sleep(PAUSE_SEKUNDEN)

    async def abholen(self, client: httpx.AsyncClient, gruppe: list[Bot]) -> bool:
        """Einmal getUpdates fuer ein Bot-Token; False bei Fehler."""
        bot = gruppe[0]
        chats = {b.chat_id: b for b in gruppe}
        parameter: dict[str, Any] = {"timeout": POLL_SEKUNDEN, "allowed_updates": ["callback_query"]}
        if bot.token in self._offset:
            parameter["offset"] = self._offset[bot.token]
        try:
            updates = await _aufruf(client, bot, "getUpdates", json=parameter, timeout=POLL_SEKUNDEN + 15)
        except TelegramFehler as e:
            # 409 = ein anderes Programm holt fuer diesen Bot ab (oder ein Webhook ist gesetzt).
            if str(e) not in self._gewarnt:
                self._gewarnt.add(str(e))
                logger.warning("Lager-Autodruck: Telegram-Knoepfe nicht abrufbar: %s", e)
            return False
        self._gewarnt.clear()
        for update in updates or []:
            self._offset[bot.token] = int(update.get("update_id", 0)) + 1
            anfrage = update.get("callback_query")
            if anfrage:
                await self.knopf(client, chats, anfrage)
        return True

    async def knopf(self, client: httpx.AsyncClient, chats: dict[str, Bot], anfrage: dict) -> None:
        botschaft = anfrage.get("message") or {}
        chat_id = str((botschaft.get("chat") or {}).get("id", ""))
        bot = chats.get(chat_id)
        if bot is None:
            # Fremder Chat: nichts ausfuehren, nur den Ladekreis am Knopf beenden.
            antwort_bot = next(iter(chats.values()))
            await self._beantworten(client, antwort_bot, anfrage, "Nicht erlaubt.")
            logger.warning("Lager-Autodruck: Telegram-Knopf aus fremdem Chat %s ignoriert", chat_id)
            return
        absender = anfrage.get("from") or {}
        wer = absender.get("first_name") or absender.get("username") or "Telegram"
        try:
            ergebnis = await self._ausfuehren(str(anfrage.get("data") or ""), f"Telegram ({wer})")
        except Exception:  # noqa: BLE001
            logger.exception("Lager-Autodruck: Telegram-Knopf fehlgeschlagen")
            ergebnis = "Fehler - bitte in der Druckübersicht nachsehen."
        await self._beantworten(client, bot, anfrage, ergebnis)
        # Knoepfe entfernen und das Ergebnis unter die Nachricht schreiben.
        zusatz = f"\n\n→ {ergebnis} ({wer})"
        try:
            if "photo" in botschaft:
                await _aufruf(
                    client,
                    bot,
                    "editMessageCaption",
                    json={
                        "chat_id": chat_id,
                        "message_id": botschaft.get("message_id"),
                        "caption": ((botschaft.get("caption") or "") + zusatz)[:1024],
                    },
                )
            else:
                await _aufruf(
                    client,
                    bot,
                    "editMessageText",
                    json={
                        "chat_id": chat_id,
                        "message_id": botschaft.get("message_id"),
                        "text": (botschaft.get("text") or "") + zusatz,
                    },
                )
        except TelegramFehler as e:
            logger.info("Lager-Autodruck: Telegram-Nachricht nicht aktualisiert: %s", e)

    async def _beantworten(self, client: httpx.AsyncClient, bot: Bot, anfrage: dict, text: str) -> None:
        try:
            await _aufruf(
                client, bot, "answerCallbackQuery", json={"callback_query_id": anfrage.get("id"), "text": text[:200]}
            )
        except TelegramFehler:
            pass
