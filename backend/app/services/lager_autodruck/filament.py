"""Filament ans Lager: Bambuddys Spulen-Inventar fuehrt den Filamentbestand.

Jede Bambuddy-Sorte (Material, Typ, Hersteller, Farbe) wird einmal einem
Lager-Material zugeordnet. Bei jedem Durchlauf schickt Bambuddy die Summe
der Restgewichte aller aktiven Spulen je Lager-Material ans Lager
(filament_abgleich). Das Lager zieht dann selbst kein Material mehr ab -
der echte Verbrauch kommt aus Bambuddy (AMS bzw. Druckdatei).

Liegt als eigener JSON-Wert in ``settings`` (Schluessel ``lager_filament``),
getrennt von den Autodruck-Einstellungen.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.db_dialect import upsert_setting
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool

logger = logging.getLogger(__name__)

SCHLUESSEL = "lager_filament"


@dataclass
class FilamentKonfig:
    aktiv: bool = False
    # Sorten-Schluessel (siehe sorte_schluessel) -> id des Lager-Materials
    zuordnung: dict[str, str] = field(default_factory=dict)


async def laden(db: AsyncSession) -> FilamentKonfig:
    zeile = (await db.execute(select(Settings).where(Settings.key == SCHLUESSEL))).scalar_one_or_none()
    if zeile is None or not zeile.value:
        return FilamentKonfig()
    try:
        roh = json.loads(zeile.value)
    except ValueError:
        logger.error("Lager-Filament: Einstellungen nicht lesbar - nutze Standardwerte")
        return FilamentKonfig()
    zuordnung = roh.get("zuordnung") if isinstance(roh.get("zuordnung"), dict) else {}
    return FilamentKonfig(
        aktiv=bool(roh.get("aktiv")),
        zuordnung={str(k): str(v) for k, v in zuordnung.items() if k and v},
    )


async def speichern(db: AsyncSession, fk: FilamentKonfig) -> None:
    await upsert_setting(db, Settings, SCHLUESSEL, json.dumps(asdict(fk)))
    await db.commit()


def _teil(text: str | None) -> str:
    return " ".join((text or "").split())


def sorte_schluessel(material: str | None, subtype: str | None, brand: str | None, color_name: str | None) -> str:
    """Gleiche Sorte = gleiches Material, Typ, Hersteller und Farbname (Gross/klein egal)."""
    return "|".join(_teil(t).casefold() for t in (material, subtype, brand, color_name))


def rest_g(spule: Spool) -> float:
    # Wie Bambuddy selbst (Inventar, Bestandsprognose): Nenngewicht minus Verbrauch.
    return max(0.0, float(spule.label_weight or 0) - float(spule.weight_used or 0))


async def aktive_spulen(db: AsyncSession) -> list[Spool]:
    return list((await db.execute(select(Spool).where(Spool.archived_at.is_(None)))).scalars().all())


def sorten(spulen: Iterable[Spool]) -> list[dict]:
    """Bambuddy-Sorten mit Anzahl Spulen und Restgewicht, fuer die Zuordnung."""
    je_sorte: dict[str, dict] = {}
    for s in spulen:
        schluessel = sorte_schluessel(s.material, s.subtype, s.brand, s.color_name)
        eintrag = je_sorte.get(schluessel)
        if eintrag is None:
            eintrag = je_sorte[schluessel] = {
                "schluessel": schluessel,
                "material": _teil(s.material),
                "subtype": _teil(s.subtype),
                "brand": _teil(s.brand),
                "color_name": _teil(s.color_name),
                "rgba": s.rgba,
                "spulen": 0,
                "rest_g": 0.0,
            }
        eintrag["spulen"] += 1
        eintrag["rest_g"] += rest_g(s)
    for eintrag in je_sorte.values():
        eintrag["rest_g"] = round(eintrag["rest_g"], 1)
    return sorted(je_sorte.values(), key=lambda e: (e["material"], e["subtype"], e["brand"], e["color_name"]))


def summen(spulen: Iterable[Spool], zuordnung: dict[str, str]) -> dict[str, float]:
    """Restgewicht je Lager-Material.

    Jedes zugeordnete Material steht drin, auch mit 0 - sonst bliebe im Lager
    der alte Bestand stehen, wenn die letzte Spule leer oder archiviert ist.
    """
    ergebnis = dict.fromkeys(zuordnung.values(), 0.0)
    for s in spulen:
        material_id = zuordnung.get(sorte_schluessel(s.material, s.subtype, s.brand, s.color_name))
        if material_id:
            ergebnis[material_id] += rest_g(s)
    return {material_id: round(g, 1) for material_id, g in ergebnis.items()}
