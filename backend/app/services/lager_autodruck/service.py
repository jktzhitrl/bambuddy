"""Lager-Autodruck: Bestand pruefen, Drucke einplanen, Ergebnisse verbuchen.

Ein Durchlauf (alle ``intervall_minuten`` oder per Knopf "Jetzt pruefen"):

1. Jobs abgleichen: Status der eigenen Druckauftraege aus der Bambuddy-
   Warteschlange uebernehmen (freigegeben, druckt, geloescht ...).
2. Offene Buchungen ans Lager senden.
3. Nur wenn der Autodruck eingeschaltet ist: Bestand aus dem Lager holen,
   Bedarf je Regel rechnen, von der KI bewerten lassen und fehlende Drucke in
   die Warteschlange stellen - je nach Regel sofort startbereit oder mit
   "manueller Start" (= wartet auf Freigabe).

Nach jedem Druckende ruft Bambuddy ``bei_druckende`` auf: der Job bekommt
seinen Endstatus und das Ergebnis wird als Buchung ins Lager gemeldet.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.library_variants import normalize_model_name
from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.lager_autodruck import (
    BUCHUNG_FEHLER,
    BUCHUNG_GESENDET,
    BUCHUNG_OFFEN,
    JOB_ABGEBROCHEN,
    JOB_DRUCKT,
    JOB_FEHLDRUCK,
    JOB_FERTIG,
    JOB_GEPLANT,
    JOB_OFFEN,
    JOB_VERWORFEN,
    JOB_WARTET,
    MODUS_AUS,
    MODUS_AUTOMATISCH,
    MODUS_KI,
    LagerBuchung,
    LagerDruckJob,
    LagerDruckRegel,
)
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.lager_autodruck import bedarf, ki, konfig, melden, nachtruhe
from backend.app.services.lager_autodruck.supabase import LagerClient, LagerFehler
from backend.app.utils.local_time import local_day_start, utcnow_naive

logger = logging.getLogger(__name__)

# Nach so vielen Fehlversuchen wird eine Buchung nicht mehr automatisch
# wiederholt (in der Oberflaeche per "Erneut senden" weiter moeglich).
MAX_VERSUCHE = 20
_RANG = {"hoch": 0, "mittel": 1, "niedrig": 2}
# Antworten von druck_verbuchen, bei denen der Bestand im Lager wirklich stieg.
BUCHUNG_OK = ("gebucht", "schon_gebucht")
# Antworten, bei denen im Lager nichts mehr zu tun ist ("ignoriert" = Drucker
# ist im Lager bewusst ausgeschlossen).
BUCHUNG_ANGEKOMMEN = ("gebucht", "gebucht_fehldruck", "schon_gebucht", "ignoriert")
# Nach so vielen Fehlschlaegen in Folge gibt es eine Benachrichtigung.
MELDEN_NACH_VERSUCHEN = 3


def dateiname_aus_druck(subtask_name: str | None, filename: str | None) -> str:
    """Name eines Drucks so, wie ihn der fruehere Bambuddy-Webhook ans Lager schickte.

    Gleiche Regel wie ``{filename}`` in Bambuddys Benachrichtigungen, damit die
    bestehenden Eintraege in druck_zuordnung weiter passen.
    """
    if subtask_name:
        return subtask_name.replace("_", " ")
    name = os.path.basename(filename or "") or "Unbekannt"
    for endung in (".gcode.3mf", ".gcode", ".3mf"):
        if name.endswith(endung):
            return name[: -len(endung)]
    return name


def dateiname_aus_archiv(archiv: PrintArchive) -> str:
    return (archiv.print_name or "").strip() or dateiname_aus_druck(None, archiv.filename)


class LagerAutodruckService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._lauf_lock = asyncio.Lock()
        self._buchungs_lock = asyncio.Lock()
        # Druckende und Durchlauf koennen gleichzeitig senden wollen.
        self._sende_lock = asyncio.Lock()
        self._client: LagerClient | None = None
        self._client_schluessel: tuple | None = None
        self._aufwecken = asyncio.Event()
        # Stand fuer die Oberflaeche
        self.letzter_lauf: datetime | None = None
        self.letztes_ergebnis: dict | None = None
        self.letzter_fehler: str | None = None
        self.uebersicht: list[dict] = []
        # Frueheste Zeit, zu der ein startbereiter Druck wegen der Nachtruhe
        # zurueckgehalten werden muss - die Schleife wacht dann genau auf.
        self.naechste_grenze: datetime | None = None
        # Damit dieselbe Meldung nicht bei jedem Durchlauf wiederkommt.
        self._gemeldet_gesperrt: set[int] = set()
        self._fehler_in_folge = 0
        self._offline_gemeldet = False

    # --- Lebenszyklus -----------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            from backend.app.core.tasks import spawn_background_task

            self._task = spawn_background_task(self._schleife(), name="lager-autodruck")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def aufwecken(self) -> None:
        """Naechsten Durchlauf sofort starten (z.B. nach geaenderten Einstellungen)."""
        self._aufwecken.set()

    async def _schleife(self) -> None:
        try:
            await schema_nachziehen()
        except Exception:  # noqa: BLE001
            logger.exception("Lager-Autodruck: Tabellen konnten nicht aktualisiert werden")
        # Bambuddy erst in Ruhe hochfahren lassen.
        await asyncio.sleep(30)
        while True:
            intervall = 5
            try:
                async with database.async_session() as db:
                    intervall = (await konfig.laden(db)).intervall_minuten
                await self.durchlauf()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - die Schleife darf nie sterben
                logger.exception("Lager-Autodruck: Durchlauf fehlgeschlagen")
                self.letzter_fehler = str(e)
            warten = intervall * 60.0
            if self.naechste_grenze is not None:
                bis_grenze = (self.naechste_grenze - utcnow_naive()).total_seconds() + 1
                warten = max(1.0, min(warten, bis_grenze))
            try:
                await asyncio.wait_for(self._aufwecken.wait(), timeout=warten)
            except asyncio.TimeoutError:
                pass
            self._aufwecken.clear()

    def lager_client(self, k: konfig.Konfig) -> LagerClient:
        schluessel = (k.supabase_url, k.supabase_anon_key, k.email, k.passwort)
        if self._client is None or self._client_schluessel != schluessel:
            self._client = LagerClient(k.supabase_url, k.supabase_anon_key, k.email, k.passwort)
            self._client_schluessel = schluessel
        return self._client

    # --- Durchlauf ----------------------------------------------------------

    async def durchlauf(self) -> dict:
        async with self._lauf_lock:
            async with database.async_session() as db:
                k = await konfig.laden(db)
                if not k.eingerichtet:
                    return {"ergebnis": "nicht eingerichtet"}
                lager = self.lager_client(k)

                await self.jobs_abgleichen(db)
                await self.nachtruhe_anwenden(db, k)
                gesendet = await self.buchungen_senden(db, lager, k)

                if not k.aktiv:
                    ergebnis = {"ergebnis": "Autodruck ausgeschaltet", "buchungen_gesendet": gesendet}
                    await self.auftraege_melden(db, k)
                else:
                    try:
                        ergebnis = await self._planen(db, k, lager)
                    except LagerFehler as e:
                        self.letzter_fehler = str(e)
                        self.letzter_lauf = utcnow_naive()
                        self.letztes_ergebnis = {"ergebnis": "Fehler", "meldung": str(e)}
                        await self._lager_offline(db, k, str(e))
                        return self.letztes_ergebnis
                    ergebnis["buchungen_gesendet"] = gesendet
                    await self._lager_wieder_da(db, k)
                    await self.auftraege_melden(db, k)

            self.letzter_lauf = utcnow_naive()
            self.letzter_fehler = None
            self.letztes_ergebnis = ergebnis
            return ergebnis

    async def _planen(self, db: AsyncSession, k: konfig.Konfig, lager: LagerClient) -> dict:
        regeln = list((await db.execute(select(LagerDruckRegel))).scalars().all())
        if not regeln:
            self.uebersicht = []
            return {"ergebnis": "keine Regeln", "angelegt": 0}

        # Was ist schon in Arbeit (Warteschlange oder Drucker)?
        in_arbeit: dict[str, float] = defaultdict(float)
        for part_id, stueck in (
            await db.execute(
                select(LagerDruckJob.part_id, LagerDruckJob.stueck).where(LagerDruckJob.status.in_(JOB_OFFEN))
            )
        ).all():
            in_arbeit[part_id] += stueck
        # Fertig gedruckt, aber noch nicht im Lager angekommen: zaehlt weiter als
        # in Arbeit, sonst wuerde bis zur Buchung ein zweites Mal geplant.
        for part_id, stueck in (
            await db.execute(
                select(LagerDruckJob.part_id, LagerDruckJob.stueck)
                .join(LagerBuchung, LagerBuchung.job_id == LagerDruckJob.id)
                .where(LagerDruckJob.status == JOB_FERTIG, LagerBuchung.zustand != BUCHUNG_GESENDET)
            )
        ).all():
            in_arbeit[part_id] += stueck

        # Kam die letzte Buchung einer Regel im Lager nicht an (z.B. "unbekannt",
        # weil druck_zuordnung fehlt), steigt der Bestand nie - dann nicht
        # endlos weiterdrucken, sondern die Regel anhalten, bis sie neu
        # gespeichert wird.
        gesperrt: dict[int, str] = {}
        for r in regeln:
            zeile = (
                await db.execute(
                    select(LagerBuchung.ergebnis, LagerBuchung.created_at)
                    .join(LagerDruckJob, LagerBuchung.job_id == LagerDruckJob.id)
                    .where(
                        LagerDruckJob.regel_id == r.id,
                        LagerBuchung.status == "fertig",
                        LagerBuchung.zustand == BUCHUNG_GESENDET,
                    )
                    .order_by(LagerBuchung.id.desc())
                    .limit(1)
                )
            ).first()
            # Zeitvergleich in Python: SQLite vergleicht Zeitstempel als Text.
            if zeile is None or (r.updated_at and zeile.created_at and zeile.created_at < r.updated_at):
                continue
            letzte = zeile.ergebnis
            if letzte not in BUCHUNG_OK:
                gesperrt[r.id] = (
                    f"Angehalten: letzter Druck kam im Lager als '{letzte}' an. "
                    f"Zuordnung '{r.dateiname}' im Lager pruefen, dann Regel neu speichern."
                )

        tagesbeginn = local_day_start(datetime.now(timezone.utc)).replace(tzinfo=None)
        heute = dict(
            (
                await db.execute(
                    select(LagerDruckJob.regel_id, func.count(LagerDruckJob.id))
                    .where(LagerDruckJob.created_at >= tagesbeginn)
                    .group_by(LagerDruckJob.regel_id)
                )
            ).all()
        )

        eingaben = [
            bedarf.RegelEingabe(
                regel_id=r.id,
                part_id=r.part_id,
                stueck_je_druck=r.stueck_je_druck,
                max_drucke_pro_tag=r.max_drucke_pro_tag,
                heute_angelegt=heute.get(r.id, 0),
            )
            for r in regeln
        ]
        daten = await lager.lade_bestandsdaten()
        kandidaten, uebersicht = bedarf.berechne(daten, eingaben, in_arbeit)

        regel_je_id = {r.id: r for r in regeln}
        for eintrag in uebersicht:
            regel = regel_je_id[eintrag.regel_id]
            if regel.modus == MODUS_AUS:
                eintrag.hinweis = "Regel pausiert"
            elif regel.archive_id is None:
                eintrag.hinweis = "Keine Druckdatei gewaehlt"
            elif regel.id in gesperrt:
                eintrag.hinweis = gesperrt[regel.id]
            if eintrag.name and eintrag.name != regel.part_name:
                regel.part_name = eintrag.name
        await self._sperren_melden(db, k, gesperrt, regel_je_id)
        kandidaten = [
            c
            for c in kandidaten
            if regel_je_id[c.regel_id].modus != MODUS_AUS
            and regel_je_id[c.regel_id].archive_id is not None
            and c.regel_id not in gesperrt
        ]
        self.uebersicht = [e.__dict__ for e in uebersicht]

        if not kandidaten:
            await db.commit()
            return {"ergebnis": "nichts zu drucken", "angelegt": 0, "regeln": len(regeln)}

        einschaetzung = await ki.einschaetzen(
            kandidaten, api_key=k.anthropic_api_key if k.ki_verwenden else None, modell=k.ki_modell
        )
        bewertet = []
        for c in kandidaten:
            dringlichkeit, begruendung = einschaetzung.get(c.part_id) or (
                bedarf.regel_dringlichkeit(c),
                bedarf.regel_begruendung(c),
            )
            bewertet.append((c, dringlichkeit, begruendung))
        # Dringendes zuerst anlegen, damit es auch zuerst in die Warteschlange kommt.
        bewertet.sort(key=lambda x: _RANG.get(x[1], 3))

        angelegt = 0
        wartet = 0
        jetzt = utcnow_naive()
        for c, dringlichkeit, begruendung in bewertet:
            regel = regel_je_id[c.regel_id]
            archiv = await db.get(PrintArchive, regel.archive_id)
            if archiv is None:
                continue
            ohne_freigabe = regel.modus == MODUS_AUTOMATISCH or (regel.modus == MODUS_KI and dringlichkeit == "niedrig")
            target_model = normalize_model_name(regel.target_model)
            if regel.printer_id is None and not target_model:
                target_model = normalize_model_name(archiv.sliced_for_model)
                if not target_model:
                    logger.warning("Lager-Autodruck: Regel %s hat weder Drucker noch Druckermodell", regel.id)
                    continue

            for _ in range(c.druecke):
                item = await self._in_warteschlange(
                    db,
                    regel=regel,
                    archiv=archiv,
                    target_model=None if regel.printer_id else target_model,
                    manueller_start=not ohne_freigabe,
                    vorziehen=dringlichkeit == "hoch",
                )
                # Noch vor dem Commit, sonst koennte die Warteschlange den Druck
                # starten, bevor die Nachtruhe geprueft ist.
                self.nachtruhe_fuer_item(k, item, jetzt)
                db.add(
                    LagerDruckJob(
                        regel_id=regel.id,
                        part_id=regel.part_id,
                        part_name=c.name,
                        queue_item_id=item.id,
                        stueck=max(1, regel.stueck_je_druck),
                        status=JOB_GEPLANT if ohne_freigabe else JOB_WARTET,
                        dringlichkeit=dringlichkeit,
                        begruendung=begruendung,
                        freigegeben_von="automatisch" if ohne_freigabe else None,
                    )
                )
                angelegt += 1
                wartet += 0 if ohne_freigabe else 1
            logger.info(
                "Lager-Autodruck: %s x '%s' eingeplant (%s, %s)",
                c.druecke,
                c.name,
                dringlichkeit,
                "startet automatisch" if ohne_freigabe else "wartet auf Freigabe",
            )
        await db.commit()
        await self.nachtruhe_anwenden(db, k)
        return {"ergebnis": "ok", "angelegt": angelegt, "wartet_auf_freigabe": wartet, "regeln": len(regeln)}

    async def _in_warteschlange(
        self,
        db: AsyncSession,
        *,
        regel: LagerDruckRegel,
        archiv: PrintArchive,
        target_model: str | None,
        manueller_start: bool,
        vorziehen: bool,
    ) -> PrintQueueItem:
        # Gleiche Positionslogik wie beim Anlegen ueber die Warteschlange:
        # je Drucker, bzw. gemeinsam fuer alle nicht zugewiesenen Eintraege.
        if regel.printer_id is not None:
            bereich = (PrintQueueItem.printer_id == regel.printer_id, PrintQueueItem.status == "pending")
        else:
            bereich = (PrintQueueItem.printer_id.is_(None), PrintQueueItem.status == "pending")
        if vorziehen:
            await db.execute(update(PrintQueueItem).where(*bereich).values(position=PrintQueueItem.position + 1))
            position = 1
        else:
            hoechste = (await db.execute(select(func.max(PrintQueueItem.position)).where(*bereich))).scalar()
            position = (hoechste or 0) + 1

        item = PrintQueueItem(
            printer_id=regel.printer_id,
            target_model=target_model,
            target_location=regel.target_location if target_model else None,
            archive_id=archiv.id,
            plate_id=regel.plate_id,
            position=position,
            manual_start=manueller_start,
            status="pending",
            print_time_seconds=archiv.print_time_seconds,
        )
        db.add(item)
        await db.flush()
        return item

    # --- Nachtruhe -----------------------------------------------------------

    def nachtruhe_fuer_item(self, k: konfig.Konfig, item: PrintQueueItem, jetzt: datetime) -> datetime | None:
        """Startzeit eines startbereiten Drucks an die Nachtruhe anpassen.

        Liefert die Grenze, ab der ein jetzt erlaubter Start nicht mehr erlaubt waere.
        """
        ruhe = k.nachtruhe()
        if ruhe is None or not item.print_time_seconds:
            # Ohne Nachtruhe oder ohne bekannte Druckdauer: nicht zurueckhalten.
            item.scheduled_time = None
            return None
        dauer = timedelta(seconds=item.print_time_seconds)
        item.scheduled_time = nachtruhe.fruehester_start(ruhe, jetzt, dauer)
        if item.scheduled_time is None:
            return nachtruhe.naechste_grenze(ruhe, jetzt, dauer)
        return None

    async def nachtruhe_anwenden(self, db: AsyncSession, k: konfig.Konfig) -> None:
        """Alle eigenen, startbereiten Drucke pruefen.

        Laeuft bei jedem Durchlauf und genau an der naechsten Grenze - ein Druck,
        der wegen eines belegten Druckers spaeter startet als gedacht, soll
        trotzdem nicht in der Nacht fertig werden.
        """
        jetzt = utcnow_naive()
        grenzen = []
        jobs = (await db.execute(select(LagerDruckJob).where(LagerDruckJob.status == JOB_GEPLANT))).scalars().all()
        for job in jobs:
            item = await db.get(PrintQueueItem, job.queue_item_id) if job.queue_item_id else None
            if item is None or item.status != "pending" or item.manual_start:
                continue
            grenze = self.nachtruhe_fuer_item(k, item, jetzt)
            if grenze is not None:
                grenzen.append(grenze)
        self.naechste_grenze = min(grenzen) if grenzen else None
        await db.commit()

    # --- Abgleich mit der Warteschlange ------------------------------------

    async def jobs_abgleichen(self, db: AsyncSession) -> None:
        jobs = list(
            (await db.execute(select(LagerDruckJob).where(LagerDruckJob.status.in_(JOB_OFFEN)))).scalars().all()
        )
        for job in jobs:
            item = await db.get(PrintQueueItem, job.queue_item_id) if job.queue_item_id else None
            if item is None:
                # In der Warteschlange geloescht -> nicht mehr in Arbeit.
                job.status = JOB_VERWORFEN
                job.finished_at = job.finished_at or utcnow_naive()
            elif item.status == "printing":
                job.status = JOB_DRUCKT
            elif item.status == "pending":
                if item.manual_start:
                    job.status = JOB_WARTET
                else:
                    if job.status == JOB_WARTET:
                        job.freigegeben_von = job.freigegeben_von or "Bambuddy-Warteschlange"
                    job.status = JOB_GEPLANT
            elif item.status in ("completed", "failed"):
                # Druckende verpasst (z.B. Neustart): Status und Buchung nachholen.
                await self._job_abschliessen(
                    db,
                    job,
                    item.status,
                    drucker_id=item.printer_id,
                    ende=item.completed_at,
                )
            else:  # cancelled, skipped, ...
                job.status = JOB_ABGEBROCHEN
                job.finished_at = job.finished_at or item.completed_at or utcnow_naive()
        await db.commit()

    # --- Druckende -----------------------------------------------------------

    async def bei_druckende(
        self, printer_id: int, data: dict, queue_item_id: int | None, archive_id: int | None
    ) -> None:
        """Wird von Bambuddy nach jedem Druckende aufgerufen (im Hintergrund)."""
        try:
            await self._bei_druckende(printer_id, data, queue_item_id, archive_id)
        except Exception:  # noqa: BLE001 - darf Bambuddys Druckende nie stoeren
            logger.exception("Lager-Autodruck: Verarbeitung des Druckendes fehlgeschlagen")

    async def _bei_druckende(
        self, printer_id: int, data: dict, queue_item_id: int | None, archive_id: int | None
    ) -> None:
        status = data.get("status") or "completed"
        async with database.async_session() as db:
            k = await konfig.laden(db)
            job = None
            if queue_item_id is not None:
                job = (
                    await db.execute(select(LagerDruckJob).where(LagerDruckJob.queue_item_id == queue_item_id))
                ).scalar_one_or_none()

            if job is not None:
                await self._job_abschliessen(db, job, status, drucker_id=printer_id, archive_id=archive_id)
                await db.commit()
            elif k.eingerichtet and k.alle_drucke_verbuchen and status in ("completed", "failed", "aborted"):
                await self._buchung_anlegen(
                    db,
                    job=None,
                    drucker_id=printer_id,
                    dateiname=dateiname_aus_druck(data.get("subtask_name"), data.get("filename")),
                    status="fertig" if status == "completed" else "fehldruck",
                    archive_id=archive_id,
                )
                await db.commit()
            else:
                return

            if k.eingerichtet:
                await self.buchungen_senden(db, self.lager_client(k), k)

    async def _job_abschliessen(
        self,
        db: AsyncSession,
        job: LagerDruckJob,
        status: str,
        *,
        drucker_id: int | None,
        archive_id: int | None = None,
        ende: datetime | None = None,
    ) -> None:
        if status == "completed":
            job.status = JOB_FERTIG
        elif status in ("failed", "aborted"):
            job.status = JOB_FEHLDRUCK
        else:
            job.status = JOB_ABGEBROCHEN
        job.finished_at = job.finished_at or ende or utcnow_naive()
        if job.status == JOB_ABGEBROCHEN:
            return

        regel = await db.get(LagerDruckRegel, job.regel_id) if job.regel_id else None
        if archive_id is None and job.queue_item_id:
            item = await db.get(PrintQueueItem, job.queue_item_id)
            archive_id = item.archive_id if item else None
        await self._buchung_anlegen(
            db,
            job=job,
            drucker_id=drucker_id,
            dateiname=regel.dateiname if regel else (job.part_name or job.part_id),
            status="fertig" if job.status == JOB_FERTIG else "fehldruck",
            archive_id=archive_id,
            zeit=job.finished_at,
        )

    async def _buchung_anlegen(
        self,
        db: AsyncSession,
        *,
        job: LagerDruckJob | None,
        drucker_id: int | None,
        dateiname: str,
        status: str,
        archive_id: int | None,
        zeit: datetime | None = None,
    ) -> None:
        async with self._buchungs_lock:
            if job is not None:
                vorhanden = (await db.execute(select(LagerBuchung.id).where(LagerBuchung.job_id == job.id))).first()
                if vorhanden:
                    return
            drucker = await db.get(Printer, drucker_id) if drucker_id else None
            gramm = None
            if archive_id:
                archiv = await db.get(PrintArchive, archive_id)
                gramm = archiv.filament_used_grams if archiv else None
            zeitpunkt = zeit or utcnow_naive()
            db.add(
                LagerBuchung(
                    job_id=job.id if job else None,
                    drucker=drucker.name if drucker else "?",
                    dateiname=dateiname,
                    # Mikrosekunden machen den Schluessel pro Druck eindeutig.
                    zeitstempel=zeitpunkt.isoformat(timespec="microseconds"),
                    gramm=gramm,
                    status=status,
                    zustand=BUCHUNG_OFFEN,
                )
            )
            await db.flush()

    async def buchungen_senden(self, db: AsyncSession, lager: LagerClient, k: konfig.Konfig) -> int:
        async with self._sende_lock:
            return await self._buchungen_senden(db, lager, k)

    async def _buchungen_senden(self, db: AsyncSession, lager: LagerClient, k: konfig.Konfig) -> int:
        offene = list(
            (
                await db.execute(
                    select(LagerBuchung)
                    .where(LagerBuchung.zustand.in_((BUCHUNG_OFFEN, BUCHUNG_FEHLER)))
                    .where(LagerBuchung.versuche < MAX_VERSUCHE)
                    .order_by(LagerBuchung.id)
                )
            )
            .scalars()
            .all()
        )
        gesendet = 0
        for buchung in offene:
            buchung.versuche += 1
            try:
                buchung.ergebnis = await lager.druck_verbuchen(
                    drucker=buchung.drucker,
                    dateiname=buchung.dateiname,
                    zeitstempel=buchung.zeitstempel,
                    gramm=buchung.gramm,
                    status=buchung.status,
                )
            except LagerFehler as e:
                buchung.zustand = BUCHUNG_FEHLER
                buchung.fehler = str(e)[:1000]
                logger.warning("Lager-Autodruck: Buchung %s nicht gesendet: %s", buchung.id, e)
                await db.commit()
                if buchung.versuche == MELDEN_NACH_VERSUCHEN:
                    await melden.senden(
                        db,
                        k,
                        "buchungsfehler",
                        "Lager-Autodruck: Buchung hängt",
                        f"Der Druck „{buchung.dateiname}“ ({buchung.drucker}) konnte nach "
                        f"{buchung.versuche} Versuchen nicht ans Lager gemeldet werden: {e}\n"
                        "Bambuddy versucht es weiter.",
                    )
                # Lager vermutlich nicht erreichbar - Rest beim naechsten Durchlauf.
                break
            buchung.zustand = BUCHUNG_GESENDET
            buchung.fehler = None
            buchung.gesendet_at = utcnow_naive()
            gesendet += 1
            await db.commit()
            if buchung.ergebnis not in BUCHUNG_ANGEKOMMEN:
                await melden.senden(
                    db,
                    k,
                    "buchungsfehler",
                    "Lager-Autodruck: Druck nicht verbucht",
                    f"Das Lager hat den Druck „{buchung.dateiname}“ ({buchung.drucker}) nicht verbucht: "
                    f"{buchung.ergebnis}. Bitte im Lager unter Druck-Eingang prüfen.",
                )
        return gesendet

    # --- Meldungen ---------------------------------------------------------------

    async def auftraege_melden(self, db: AsyncSession, k: konfig.Konfig) -> None:
        """Neue Auftraege melden: "wartet auf Freigabe" und "automatisch eingeplant".

        Zwischen "Fertig spaetestens" und "Fertig fruehestens" (der Nacht) wird
        nur gesammelt und zur Fertig-fruehestens-Zeit auf einmal geschickt.
        Jeder Auftrag wird genau einmal gemeldet (gemeldet_at), auch ueber einen
        Neustart hinweg. Fehler-Meldungen laufen nicht hierueber, die kommen sofort.
        """
        ruhe = k.nachtruhe()
        if ruhe is not None and nachtruhe.ist_nacht(ruhe, utcnow_naive()):
            return
        jobs = list(
            (
                await db.execute(
                    select(LagerDruckJob).where(LagerDruckJob.gemeldet_at.is_(None)).order_by(LagerDruckJob.id)
                )
            )
            .scalars()
            .all()
        )
        if not jobs:
            return
        wartend = [j for j in jobs if j.status == JOB_WARTET]
        automatisch = [j for j in jobs if j.freigegeben_von == "automatisch" and j.status != JOB_VERWORFEN]
        jetzt = utcnow_naive()
        for j in jobs:
            j.gemeldet_at = jetzt
        await db.commit()
        if wartend:
            await melden.senden(
                db,
                k,
                "freigabe",
                "Lager-Autodruck: Freigabe nötig",
                "Diese Drucke warten in Bambuddy auf deine Freigabe:\n" + _auftragsliste(wartend),
            )
        if automatisch:
            await melden.senden(
                db,
                k,
                "eingeplant",
                "Lager-Autodruck: Drucke eingeplant",
                "Automatisch in die Warteschlange gestellt:\n" + _auftragsliste(automatisch),
            )

    async def _sperren_melden(
        self, db: AsyncSession, k: konfig.Konfig, gesperrt: dict[int, str], regeln: dict[int, LagerDruckRegel]
    ) -> None:
        for regel_id in set(gesperrt) - self._gemeldet_gesperrt:
            regel = regeln[regel_id]
            await melden.senden(
                db,
                k,
                "angehalten",
                "Lager-Autodruck: Regel angehalten",
                f"„{regel.part_name or regel.part_id}“: {gesperrt[regel_id]}",
            )
        self._gemeldet_gesperrt = set(gesperrt)

    async def _lager_offline(self, db: AsyncSession, k: konfig.Konfig, fehler: str) -> None:
        self._fehler_in_folge += 1
        # Erst nach mehreren Fehlschlaegen melden - ein kurzer Aussetzer ist normal.
        if self._fehler_in_folge == MELDEN_NACH_VERSUCHEN and not self._offline_gemeldet:
            self._offline_gemeldet = True
            await melden.senden(
                db,
                k,
                "lager_offline",
                "Lager-Autodruck: Lager nicht erreichbar",
                f"Bambuddy erreicht das Lager seit {self._fehler_in_folge} Prüfungen nicht: {fehler}\n"
                "Solange wird nichts Neues eingeplant.",
            )

    async def _lager_wieder_da(self, db: AsyncSession, k: konfig.Konfig) -> None:
        if self._offline_gemeldet:
            await melden.senden(
                db,
                k,
                "lager_offline",
                "Lager-Autodruck: Lager wieder erreichbar",
                "Die Verbindung zum Lager steht wieder, der Autodruck läuft weiter.",
            )
        self._fehler_in_folge = 0
        self._offline_gemeldet = False


def _auftragsliste(jobs: list[LagerDruckJob]) -> str:
    """Gleiche Teile zusammenfassen: "2 Drucke Halter (8 Stück, hoch)"."""
    gruppen: dict[tuple[str, str | None], list[LagerDruckJob]] = {}
    for j in jobs:
        gruppen.setdefault((j.part_name or j.part_id, j.dringlichkeit), []).append(j)
    zeilen = []
    for (name, dringlichkeit), gruppe in gruppen.items():
        drucke = f"{len(gruppe)} Drucke" if len(gruppe) > 1 else "1 Druck"
        stueck = sum(j.stueck for j in gruppe)
        zeilen.append(f"• {drucke} {name} ({stueck} Stück{', ' + dringlichkeit if dringlichkeit else ''})")
    return "\n".join(zeilen)


async def schema_nachziehen() -> None:
    """Spalten nachtragen, die nach dem ersten Anlegen der Tabellen dazukamen.

    create_all() legt nur fehlende Tabellen an, keine Spalten in bestehenden.
    """
    from sqlalchemy import inspect, text

    async with database.engine.begin() as conn:
        spalten = await conn.run_sync(lambda c: {s["name"] for s in inspect(c).get_columns("lager_druck_jobs")})
        if "gemeldet_at" not in spalten:
            await conn.execute(text("ALTER TABLE lager_druck_jobs ADD COLUMN gemeldet_at TIMESTAMP"))
            # Bestehende Auftraege gelten als gemeldet - sonst kaeme eine Flut.
            await conn.execute(text("UPDATE lager_druck_jobs SET gemeldet_at = created_at"))
            logger.info("Lager-Autodruck: Spalte lager_druck_jobs.gemeldet_at nachgetragen")


lager_autodruck_service = LagerAutodruckService()
