"""API fuer das Modul Lager-Autodruck (nur in diesem Fork).

Alle Pfade liegen unter /api/v1/lager-autodruck. Lesen braucht settings:read,
Aendern settings:update; Freigeben/Verwerfen von Druckauftraegen die
Warteschlangen-Rechte queue:update_all.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes._url_safety import assert_safe_lan_service_url
from backend.app.api.routes.library_variants import normalize_model_name
from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.lager_autodruck import (
    BUCHUNG_GESENDET,
    BUCHUNG_OFFEN,
    JOB_GEPLANT,
    JOB_VERWORFEN,
    JOB_WARTET,
    MODI,
    MODUS_KI,
    LagerBuchung,
    LagerDruckJob,
    LagerDruckRegel,
)
from backend.app.models.notification import NotificationProvider
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.services.lager_autodruck import konfig, melden, nachtruhe
from backend.app.services.lager_autodruck.service import dateiname_aus_archiv, lager_autodruck_service
from backend.app.services.lager_autodruck.supabase import LagerClient, LagerFehler
from backend.app.utils.local_time import utcnow_naive

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lager-autodruck", tags=["lager-autodruck"])

_GEHEIM_PLATZHALTER = "********"


def _iso(zeit) -> str | None:
    return zeit.isoformat() + "Z" if zeit else None


# --- Einstellungen -------------------------------------------------------------


class KonfigDaten(BaseModel):
    aktiv: bool = False
    supabase_url: str = ""
    supabase_anon_key: str = ""
    email: str = ""
    # Leer oder Platzhalter = unveraendert lassen.
    passwort: str = ""
    ki_verwenden: bool = False
    anthropic_api_key: str = ""
    ki_modell: str = ""
    intervall_minuten: int = Field(5, ge=1, le=1440)
    alle_drucke_verbuchen: bool = True
    nachtruhe_aktiv: bool = True
    schlafen: str = "22:00"
    aufstehen: str = "07:00"
    puffer_minuten: int = Field(15, ge=0, le=240)
    melden_an: list[int] = Field(default_factory=list)
    melden: list[str] = Field(default_factory=lambda: list(konfig.MELDEN_STANDARD))

    @field_validator("melden")
    @classmethod
    def _melden(cls, v: list[str]) -> list[str]:
        unbekannt = set(v) - set(melden.EREIGNISSE)
        if unbekannt:
            raise ValueError(f"Unbekannte Meldung(en): {', '.join(sorted(unbekannt))}")
        return list(dict.fromkeys(v))


def _konfig_antwort(k: konfig.Konfig) -> dict:
    daten = asdict(k)
    for name in konfig.GEHEIM:
        daten[name] = _GEHEIM_PLATZHALTER if daten[name] else ""
    daten["eingerichtet"] = k.eingerichtet
    return daten


@router.get("/konfig")
async def konfig_lesen(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    return _konfig_antwort(await konfig.laden(db))


@router.put("/konfig")
async def konfig_speichern(
    daten: KonfigDaten,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    alt = await konfig.laden(db)
    if daten.supabase_url.strip():
        try:
            assert_safe_lan_service_url(daten.supabase_url.strip(), label="Supabase-Adresse")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    for feld in ("schlafen", "aufstehen"):
        if nachtruhe.uhrzeit(getattr(daten, feld)) is None:
            raise HTTPException(400, "Uhrzeit bitte als HH:MM angeben")
    neu = konfig.Konfig(
        aktiv=daten.aktiv,
        supabase_url=daten.supabase_url.strip().rstrip("/"),
        supabase_anon_key=daten.supabase_anon_key.strip(),
        email=daten.email.strip(),
        passwort=alt.passwort if daten.passwort in ("", _GEHEIM_PLATZHALTER) else daten.passwort,
        anthropic_api_key=(
            alt.anthropic_api_key if daten.anthropic_api_key == _GEHEIM_PLATZHALTER else daten.anthropic_api_key.strip()
        ),
        ki_verwenden=daten.ki_verwenden,
        ki_modell=daten.ki_modell.strip() or alt.ki_modell,
        intervall_minuten=daten.intervall_minuten,
        alle_drucke_verbuchen=daten.alle_drucke_verbuchen,
        nachtruhe_aktiv=daten.nachtruhe_aktiv,
        schlafen=nachtruhe.uhrzeit(daten.schlafen).strftime("%H:%M"),
        aufstehen=nachtruhe.uhrzeit(daten.aufstehen).strftime("%H:%M"),
        puffer_minuten=daten.puffer_minuten,
        melden_an=list(dict.fromkeys(daten.melden_an)),
        melden=daten.melden,
    )
    await konfig.speichern(db, neu)
    lager_autodruck_service.aufwecken()
    return _konfig_antwort(neu)


@router.post("/verbindung-testen")
async def verbindung_testen(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    k = await konfig.laden(db)
    if not k.eingerichtet:
        raise HTTPException(400, "Bitte zuerst Supabase-Adresse, Schluessel, E-Mail und Passwort speichern.")
    try:
        teile = await LagerClient(k.supabase_url, k.supabase_anon_key, k.email, k.passwort).lade_teile()
    except LagerFehler as e:
        return {"ok": False, "meldung": str(e)}
    return {"ok": True, "meldung": f"Verbunden - {len(teile)} Teile im Lager gefunden."}


@router.get("/benachrichtigung/kanaele")
async def kanaele_lesen(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    """Bambuddys Benachrichtigungs-Kanaele zur Auswahl, plus die moeglichen Meldungen."""
    kanaele = (await db.execute(select(NotificationProvider).order_by(NotificationProvider.name))).scalars().all()
    return {
        "kanaele": [{"id": p.id, "name": p.name, "typ": p.provider_type, "aktiv": bool(p.enabled)} for p in kanaele],
        "ereignisse": [{"id": k, "titel": t} for k, t in melden.EREIGNISSE.items()],
    }


@router.post("/benachrichtigung/testen")
async def benachrichtigung_testen(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    k = await konfig.laden(db)
    anzahl = await melden.senden(
        db,
        k,
        "test",
        "Lager-Autodruck: Testnachricht",
        "So sehen Meldungen vom Lager-Autodruck aus.",
        erzwingen=True,
    )
    if not anzahl:
        return {"ok": False, "meldung": "Kein aktiver Kanal ausgewählt (erst speichern)."}
    return {"ok": True, "meldung": f"Testnachricht an {anzahl} Kanal/Kanäle gesendet."}


# --- Auswahllisten fuer die Regeln ----------------------------------------------


@router.get("/teile")
async def teile_lesen(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    k = await konfig.laden(db)
    if not k.eingerichtet:
        raise HTTPException(400, "Verbindung zum Lager ist noch nicht eingerichtet.")
    try:
        return await lager_autodruck_service.lager_client(k).lade_teile()
    except LagerFehler as e:
        raise HTTPException(502, str(e)) from e


@router.get("/archive")
async def archive_suchen(
    q: str = Query("", max_length=100),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    abfrage = select(PrintArchive).order_by(PrintArchive.id.desc()).limit(50)
    if q.strip():
        muster = f"%{q.strip()}%"
        abfrage = abfrage.where(or_(PrintArchive.print_name.ilike(muster), PrintArchive.filename.ilike(muster)))
    archive = (await db.execute(abfrage)).scalars().all()
    return [
        {
            "id": a.id,
            "name": dateiname_aus_archiv(a),
            "filename": a.filename,
            "modell": a.sliced_for_model,
            "gramm": a.filament_used_grams,
            "druckzeit_s": a.print_time_seconds,
        }
        for a in archive
    ]


@router.get("/drucker")
async def drucker_lesen(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    drucker = (await db.execute(select(Printer).order_by(Printer.name))).scalars().all()
    return [{"id": p.id, "name": p.name, "modell": p.model, "standort": p.location} for p in drucker]


# --- Regeln -----------------------------------------------------------------------


class RegelDaten(BaseModel):
    part_id: str = Field(min_length=1, max_length=64)
    part_name: str | None = None
    archive_id: int | None = None
    plate_id: int | None = Field(None, ge=1)
    dateiname: str = ""
    stueck_je_druck: int = Field(1, ge=1, le=10000)
    modus: str = MODUS_KI
    printer_id: int | None = None
    target_model: str | None = None
    target_location: str | None = None
    max_drucke_pro_tag: int | None = Field(None, ge=0, le=1000)

    @field_validator("modus")
    @classmethod
    def _modus(cls, v: str) -> str:
        if v not in MODI:
            raise ValueError(f"Modus muss einer von {', '.join(MODI)} sein")
        return v


def _regel_antwort(r: LagerDruckRegel) -> dict:
    return {
        "id": r.id,
        "part_id": r.part_id,
        "part_name": r.part_name,
        "archive_id": r.archive_id,
        "plate_id": r.plate_id,
        "dateiname": r.dateiname,
        "stueck_je_druck": r.stueck_je_druck,
        "modus": r.modus,
        "printer_id": r.printer_id,
        "target_model": r.target_model,
        "target_location": r.target_location,
        "max_drucke_pro_tag": r.max_drucke_pro_tag,
    }


async def _regel_uebernehmen(db: AsyncSession, regel: LagerDruckRegel, daten: RegelDaten) -> None:
    archiv = await db.get(PrintArchive, daten.archive_id) if daten.archive_id else None
    if daten.archive_id and archiv is None:
        raise HTTPException(404, "Druckdatei (Archiv) nicht gefunden")
    if daten.printer_id and await db.get(Printer, daten.printer_id) is None:
        raise HTTPException(404, "Drucker nicht gefunden")
    target_model = None if daten.printer_id else normalize_model_name(daten.target_model)
    if archiv is not None and not daten.printer_id and not target_model and not archiv.sliced_for_model:
        raise HTTPException(400, "Bitte einen Drucker oder ein Druckermodell waehlen.")
    dateiname = daten.dateiname.strip() or (dateiname_aus_archiv(archiv) if archiv else "")
    if not dateiname:
        raise HTTPException(400, "Bitte eine Druckdatei waehlen oder einen Dateinamen angeben.")

    regel.part_id = daten.part_id
    regel.part_name = daten.part_name
    regel.archive_id = daten.archive_id
    regel.plate_id = daten.plate_id
    regel.dateiname = dateiname
    regel.stueck_je_druck = daten.stueck_je_druck
    regel.modus = daten.modus
    regel.printer_id = daten.printer_id
    regel.target_model = target_model
    regel.target_location = (daten.target_location or None) if target_model else None
    regel.max_drucke_pro_tag = daten.max_drucke_pro_tag
    # Auch ohne inhaltliche Aenderung: Speichern hebt eine Sperre wegen einer
    # nicht zugeordneten Buchung auf (siehe service._planen).
    regel.updated_at = utcnow_naive()


async def _zuordnung_melden(db: AsyncSession, regel: LagerDruckRegel) -> str | None:
    """druck_zuordnung im Lager nachziehen; liefert eine Warnung statt zu scheitern."""
    k = await konfig.laden(db)
    if not k.eingerichtet:
        return "Lager noch nicht verbunden - die Zuordnung im Lager wird beim naechsten Speichern angelegt."
    try:
        await lager_autodruck_service.lager_client(k).zuordnung_speichern(
            dateiname=regel.dateiname,
            part_id=regel.part_id,
            stueck_je_druck=regel.stueck_je_druck,
            archive_id=regel.archive_id,
            printer_id=regel.printer_id,
        )
    except LagerFehler as e:
        return f"Regel gespeichert, aber die Zuordnung im Lager konnte nicht angelegt werden: {e}"
    return None


@router.get("/regeln")
async def regeln_lesen(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    regeln = (await db.execute(select(LagerDruckRegel).order_by(LagerDruckRegel.part_name))).scalars().all()
    return [_regel_antwort(r) for r in regeln]


@router.post("/regeln")
async def regel_anlegen(
    daten: RegelDaten,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    vorhanden = (
        await db.execute(select(LagerDruckRegel).where(LagerDruckRegel.part_id == daten.part_id))
    ).scalar_one_or_none()
    if vorhanden:
        raise HTTPException(409, "Fuer dieses Teil gibt es schon eine Regel.")
    regel = LagerDruckRegel()
    await _regel_uebernehmen(db, regel, daten)
    db.add(regel)
    await db.commit()
    await db.refresh(regel)
    warnung = await _zuordnung_melden(db, regel)
    return {**_regel_antwort(regel), "warnung": warnung}


@router.put("/regeln/{regel_id}")
async def regel_aendern(
    regel_id: int,
    daten: RegelDaten,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    regel = await db.get(LagerDruckRegel, regel_id)
    if regel is None:
        raise HTTPException(404, "Regel nicht gefunden")
    if daten.part_id != regel.part_id:
        andere = (
            await db.execute(select(LagerDruckRegel).where(LagerDruckRegel.part_id == daten.part_id))
        ).scalar_one_or_none()
        if andere:
            raise HTTPException(409, "Fuer dieses Teil gibt es schon eine Regel.")
    await _regel_uebernehmen(db, regel, daten)
    await db.commit()
    await db.refresh(regel)
    warnung = await _zuordnung_melden(db, regel)
    return {**_regel_antwort(regel), "warnung": warnung}


@router.delete("/regeln/{regel_id}")
async def regel_loeschen(
    regel_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    regel = await db.get(LagerDruckRegel, regel_id)
    if regel is None:
        raise HTTPException(404, "Regel nicht gefunden")
    await db.delete(regel)
    await db.commit()
    return {"ok": True}


# --- Status, Jobs, Buchungen --------------------------------------------------------


@router.get("/status")
async def status_lesen(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    k = await konfig.laden(db)
    s = lager_autodruck_service
    offene_buchungen = len(
        (await db.execute(select(LagerBuchung.id).where(LagerBuchung.zustand != BUCHUNG_GESENDET))).all()
    )
    return {
        "aktiv": k.aktiv,
        "eingerichtet": k.eingerichtet,
        "ki_aktiv": k.ki_verwenden and bool(k.anthropic_api_key),
        "intervall_minuten": k.intervall_minuten,
        "letzter_lauf": s.letzter_lauf.isoformat() + "Z" if s.letzter_lauf else None,
        "letztes_ergebnis": s.letztes_ergebnis,
        "letzter_fehler": s.letzter_fehler,
        "uebersicht": s.uebersicht,
        "offene_buchungen": offene_buchungen,
    }


@router.post("/pruefen")
async def jetzt_pruefen(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    return await lager_autodruck_service.durchlauf()


@router.get("/jobs")
async def jobs_lesen(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    jobs = (await db.execute(select(LagerDruckJob).order_by(LagerDruckJob.id.desc()).limit(limit))).scalars().all()
    ids = [j.queue_item_id for j in jobs if j.queue_item_id]
    items = {}
    if ids:
        abfrage = select(PrintQueueItem).where(PrintQueueItem.id.in_(ids))
        items = {i.id: i for i in (await db.execute(abfrage)).scalars()}
    return [
        {
            "id": j.id,
            "regel_id": j.regel_id,
            "part_id": j.part_id,
            "part_name": j.part_name,
            "queue_item_id": j.queue_item_id,
            "stueck": j.stueck,
            "status": j.status,
            "dringlichkeit": j.dringlichkeit,
            "begruendung": j.begruendung,
            "freigegeben_von": j.freigegeben_von,
            "created_at": j.created_at.isoformat() + "Z" if j.created_at else None,
            "finished_at": j.finished_at.isoformat() + "Z" if j.finished_at else None,
            # Nachtruhe: fruehester Start, falls der Druck zurueckgehalten wird.
            "geplanter_start": _iso(items[j.queue_item_id].scheduled_time) if j.queue_item_id in items else None,
            "druckdauer_s": items[j.queue_item_id].print_time_seconds if j.queue_item_id in items else None,
        }
        for j in jobs
    ]


async def _offener_job(db: AsyncSession, job_id: int) -> tuple[LagerDruckJob, PrintQueueItem | None]:
    job = await db.get(LagerDruckJob, job_id)
    if job is None:
        raise HTTPException(404, "Auftrag nicht gefunden")
    item = await db.get(PrintQueueItem, job.queue_item_id) if job.queue_item_id else None
    return job, item


@router.post("/jobs/{job_id}/freigeben")
async def job_freigeben(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    benutzer: User | None = RequirePermissionIfAuthEnabled(Permission.QUEUE_UPDATE_ALL),
):
    job, item = await _offener_job(db, job_id)
    if job.status != JOB_WARTET or item is None or item.status != "pending":
        raise HTTPException(409, "Dieser Auftrag wartet nicht (mehr) auf Freigabe.")
    item.manual_start = False
    job.status = JOB_GEPLANT
    job.freigegeben_von = benutzer.username if benutzer else "Oberflaeche"
    # Auch ein freigegebener Druck soll nicht in der Nacht fertig werden.
    lager_autodruck_service.nachtruhe_fuer_item(await konfig.laden(db), item, utcnow_naive())
    await db.commit()
    lager_autodruck_service.aufwecken()
    return {"ok": True, "geplanter_start": _iso(item.scheduled_time)}


@router.post("/jobs/{job_id}/verwerfen")
async def job_verwerfen(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.QUEUE_UPDATE_ALL),
):
    job, item = await _offener_job(db, job_id)
    if job.status not in (JOB_WARTET, JOB_GEPLANT):
        raise HTTPException(409, "Nur Auftraege, die noch nicht drucken, koennen verworfen werden.")
    if item is not None:
        if item.status != "pending":
            raise HTTPException(409, "Der Druck laeuft bereits.")
        await db.delete(item)
    job.status = JOB_VERWORFEN
    job.queue_item_id = None
    job.finished_at = utcnow_naive()
    await db.commit()
    return {"ok": True}


@router.get("/buchungen")
async def buchungen_lesen(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    buchungen = (await db.execute(select(LagerBuchung).order_by(LagerBuchung.id.desc()).limit(limit))).scalars().all()
    return [
        {
            "id": b.id,
            "job_id": b.job_id,
            "drucker": b.drucker,
            "dateiname": b.dateiname,
            "gramm": b.gramm,
            "status": b.status,
            "zustand": b.zustand,
            "versuche": b.versuche,
            "ergebnis": b.ergebnis,
            "fehler": b.fehler,
            "created_at": b.created_at.isoformat() + "Z" if b.created_at else None,
            "gesendet_at": b.gesendet_at.isoformat() + "Z" if b.gesendet_at else None,
        }
        for b in buchungen
    ]


@router.post("/buchungen/{buchung_id}/erneut")
async def buchung_erneut(
    buchung_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    buchung = await db.get(LagerBuchung, buchung_id)
    if buchung is None:
        raise HTTPException(404, "Buchung nicht gefunden")
    if buchung.zustand == BUCHUNG_GESENDET:
        raise HTTPException(409, "Diese Buchung ist schon im Lager angekommen.")
    buchung.zustand = BUCHUNG_OFFEN
    buchung.versuche = 0
    await db.commit()
    lager_autodruck_service.aufwecken()
    return {"ok": True}
