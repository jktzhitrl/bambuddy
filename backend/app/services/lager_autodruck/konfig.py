"""Einstellungen des Lager-Autodrucks.

Liegen als ein JSON-Wert in der Bambuddy-Tabelle ``settings`` (Schluessel
``lager_autodruck``). Passwort und API-Schluessel werden mit Bambuddys
Geheimnis-Verschluesselung abgelegt, wenn diese eingerichtet ist.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.db_dialect import upsert_setting
from backend.app.core.encryption import mfa_decrypt, mfa_encrypt
from backend.app.models.settings import Settings
from backend.app.services.lager_autodruck.ki import STANDARD_MODELL
from backend.app.services.lager_autodruck.nachtruhe import Nachtruhe

logger = logging.getLogger(__name__)

SCHLUESSEL = "lager_autodruck"
# Welche Meldungen standardmaessig verschickt werden (siehe melden.EREIGNISSE).
MELDEN_STANDARD = ("freigabe", "buchungsfehler", "angehalten", "lager_offline")
GEHEIM = ("passwort", "anthropic_api_key")


@dataclass
class Konfig:
    aktiv: bool = False
    supabase_url: str = ""
    supabase_anon_key: str = ""
    email: str = ""
    passwort: str = ""
    # KI-Einschaetzung kostet pro Aufruf Geld - deshalb eigener Schalter,
    # standardmaessig aus. Aus = feste Regeln (bedarf.regel_dringlichkeit).
    ki_verwenden: bool = False
    anthropic_api_key: str = ""
    ki_modell: str = STANDARD_MODELL
    intervall_minuten: int = 5
    # Auch Drucke, die nicht vom Autodruck kommen (von Hand gestartet), ins
    # Lager melden - ersetzt den frueheren "Druck fertig"-Webhook.
    alle_drucke_verbuchen: bool = True
    # Nachtruhe: kein Autodruck soll zwischen Schlafengehen und Aufstehen
    # fertig werden (Ortszeit, "HH:MM"). Puffer fuer Aufheizen/Abweichung.
    nachtruhe_aktiv: bool = True
    schlafen: str = "22:00"
    aufstehen: str = "07:00"
    puffer_minuten: int = 15
    # Benachrichtigungen: IDs von Bambuddy-Kanaelen und welche Meldungen.
    melden_an: list[int] = field(default_factory=list)
    melden: list[str] = field(default_factory=lambda: list(MELDEN_STANDARD))

    def nachtruhe(self) -> Nachtruhe | None:
        if not self.nachtruhe_aktiv:
            return None
        return Nachtruhe.aus_text(self.schlafen, self.aufstehen, self.puffer_minuten)

    @property
    def eingerichtet(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key and self.email and self.passwort)


async def laden(db: AsyncSession) -> Konfig:
    zeile = (await db.execute(select(Settings).where(Settings.key == SCHLUESSEL))).scalar_one_or_none()
    if zeile is None or not zeile.value:
        return Konfig()
    try:
        roh = json.loads(zeile.value)
    except ValueError:
        logger.error("Lager-Autodruck: Einstellungen nicht lesbar - nutze Standardwerte")
        return Konfig()
    felder = {k: v for k, v in roh.items() if k in Konfig.__dataclass_fields__}
    for name in GEHEIM:
        if felder.get(name):
            try:
                felder[name] = mfa_decrypt(felder[name])
            except RuntimeError:
                logger.error("Lager-Autodruck: %s kann nicht entschluesselt werden", name)
                felder[name] = ""
    konfig = Konfig(**felder)
    konfig.intervall_minuten = max(1, int(konfig.intervall_minuten or 5))
    return konfig


async def speichern(db: AsyncSession, konfig: Konfig) -> None:
    daten = asdict(konfig)
    for name in GEHEIM:
        if daten.get(name):
            daten[name] = mfa_encrypt(daten[name])
    await upsert_setting(db, Settings, SCHLUESSEL, json.dumps(daten))
    await db.commit()
