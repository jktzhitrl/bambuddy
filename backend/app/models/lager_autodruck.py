"""Tabellen fuer das Modul "Lager-Autodruck" (nur in diesem Fork).

Das Modul verbindet Bambuddy mit dem Werkstattlager (Supabase): es liest den
Bestand, stellt fehlende Teile automatisch in die Druckwarteschlange und
verbucht fertige Drucke zurueck ins Lager.

- ``LagerDruckRegel``   eine Regel je Lager-Teil: welche Druckdatei, wie viele
                         Stueck pro Druck, auf welchem Drucker, wie automatisch.
- ``LagerDruckJob``     ein Druckauftrag, den das Modul angelegt hat (genau ein
                         Eintrag in der Bambuddy-Warteschlange).
- ``LagerBuchung``      Ausgang Richtung Lager: jede Druckmeldung wird hier erst
                         gespeichert und dann gesendet, damit bei Netzproblemen
                         nichts verloren geht.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

# Wie automatisch eine Regel druckt.
MODUS_AUTOMATISCH = "automatisch"  # immer ohne Nachfrage drucken
MODUS_KI = "ki"  # ohne Nachfrage nur bei KI-Dringlichkeit "niedrig", sonst Freigabe
MODUS_FREIGABE = "freigabe"  # immer erst freigeben
MODUS_AUS = "aus"  # Regel pausiert
MODI = (MODUS_AUTOMATISCH, MODUS_KI, MODUS_FREIGABE, MODUS_AUS)

# Lebenslauf eines Jobs.
JOB_WARTET = "wartet_auf_freigabe"
JOB_GEPLANT = "geplant"
JOB_DRUCKT = "druckt"
JOB_FERTIG = "fertig"
JOB_FEHLDRUCK = "fehldruck"
JOB_ABGEBROCHEN = "abgebrochen"
JOB_VERWORFEN = "verworfen"
JOB_OFFEN = (JOB_WARTET, JOB_GEPLANT, JOB_DRUCKT)

# Zustand einer Buchung Richtung Lager.
BUCHUNG_OFFEN = "offen"
BUCHUNG_GESENDET = "gesendet"
BUCHUNG_FEHLER = "fehler"


class LagerDruckRegel(Base):
    __tablename__ = "lager_druck_regeln"

    id: Mapped[int] = mapped_column(primary_key=True)
    # parts.id im Lager (uuid als Text).
    part_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Name nur zur Anzeige zwischengespeichert, massgeblich bleibt das Lager.
    part_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    archive_id: Mapped[int | None] = mapped_column(ForeignKey("print_archives.id", ondelete="SET NULL"), nullable=True)
    plate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Unter diesem Namen landet der Druck im Lager (druck_zuordnung.dateiname).
    dateiname: Mapped[str] = mapped_column(String(255))
    stueck_je_druck: Mapped[int] = mapped_column(Integer, default=1)

    modus: Mapped[str] = mapped_column(String(20), default=MODUS_KI)

    # Drucker: entweder fest (printer_id) oder "irgendein freier" vom Modell
    # target_model, optional nur an einem Standort.
    printer_id: Mapped[int | None] = mapped_column(ForeignKey("printers.id", ondelete="SET NULL"), nullable=True)
    target_model: Mapped[str | None] = mapped_column(String(50), nullable=True)
    target_location: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Hoechstens so viele neue Druckauftraege pro Tag (leer = kein Limit).
    # Wann gestartet wird, regelt die Nachtruhe in den Einstellungen.
    max_drucke_pro_tag: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class LagerDruckJob(Base):
    __tablename__ = "lager_druck_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    regel_id: Mapped[int | None] = mapped_column(
        ForeignKey("lager_druck_regeln.id", ondelete="SET NULL"), nullable=True, index=True
    )
    part_id: Mapped[str] = mapped_column(String(64), index=True)
    part_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    queue_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_queue.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stueck: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default=JOB_GEPLANT, index=True)
    dringlichkeit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    begruendung: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Wer freigegeben hat ("automatisch", Benutzername, "Bambuddy-Warteschlange").
    freigegeben_von: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Wann "wartet auf Freigabe"/"eingeplant" gemeldet wurde (None = noch offen).
    gemeldet_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LagerBuchung(Base):
    __tablename__ = "lager_buchungen"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("lager_druck_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    drucker: Mapped[str] = mapped_column(String(100))
    dateiname: Mapped[str] = mapped_column(String(255))
    # Zusammen mit drucker + dateiname der Schluessel, mit dem das Lager
    # doppelte Meldungen erkennt - muss pro Druck eindeutig sein.
    zeitstempel: Mapped[str] = mapped_column(String(40))
    gramm: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # "fertig" | "fehldruck"
    zustand: Mapped[str] = mapped_column(String(20), default=BUCHUNG_OFFEN, index=True)
    versuche: Mapped[int] = mapped_column(Integer, default=0)
    # Antwort von druck_verbuchen im Lager, z.B. "gebucht", "unbekannt".
    ergebnis: Mapped[str | None] = mapped_column(String(50), nullable=True)
    fehler: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    gesendet_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
