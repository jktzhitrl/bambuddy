"""Benachrichtigungen des Lager-Autodrucks.

Nutzt Bambuddys vorhandene Benachrichtigungs-Kanaele (ntfy, Telegram, E-Mail,
Pushover ...). Welche Kanaele welche Meldungen bekommen, steht in den
Lager-Autodruck-Einstellungen - an Bambuddys eigenen Kanal-Einstellungen wird
nichts geaendert. Ruhezeiten der Kanaele gelten wie ueberall in Bambuddy.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.notification import NotificationProvider
from backend.app.services.lager_autodruck import konfig

logger = logging.getLogger(__name__)

# Schluessel -> Beschreibung (fuer Oberflaeche und Validierung)
EREIGNISSE = {
    "freigabe": "Druck wartet auf Freigabe",
    "eingeplant": "Druck automatisch eingeplant",
    "platte": "Druck fertig - Platte abräumen",
    "buchungsfehler": "Buchung kommt nicht im Lager an",
    "angehalten": "Regel angehalten",
    "lager_offline": "Lager nicht erreichbar",
}
STANDARD = list(konfig.MELDEN_STANDARD)


async def kanaele(db: AsyncSession, ids: list[int]) -> list[NotificationProvider]:
    if not ids:
        return []
    abfrage = select(NotificationProvider).where(
        NotificationProvider.id.in_(ids), NotificationProvider.enabled.is_(True)
    )
    return list((await db.execute(abfrage)).scalars().all())


async def senden(
    db: AsyncSession,
    k: konfig.Konfig,
    ereignis: str,
    titel: str,
    text: str,
    *,
    erzwingen: bool = False,
    ohne: set[int] | None = None,
) -> int:
    """Schickt eine Meldung an die gewaehlten Kanaele; liefert die Anzahl Kanaele.

    Fehler beim Senden werden nur protokolliert - eine Benachrichtigung darf
    den Autodruck nie aufhalten. ``ohne``: Kanal-IDs, die schon anders
    beliefert wurden (Telegram mit Knoepfen).
    """
    if not erzwingen and ereignis not in k.melden:
        return 0
    try:
        ziele = [z for z in await kanaele(db, k.melden_an) if z.id not in (ohne or set())]
        if not ziele:
            return 0
        # Spaeter Import: notification_service zieht viel von Bambuddy nach sich.
        from backend.app.services.notification_service import notification_service

        await notification_service._send_to_providers(ziele, titel, text, db, event_type=f"lager_autodruck_{ereignis}")
        return len(ziele)
    except Exception:  # noqa: BLE001
        logger.exception("Lager-Autodruck: Benachrichtigung '%s' fehlgeschlagen", ereignis)
        return 0
