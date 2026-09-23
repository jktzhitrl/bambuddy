"""Tests fuer das Fork-Modul Lager-Autodruck."""

from datetime import datetime
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.lager_autodruck import (
    BUCHUNG_FEHLER,
    BUCHUNG_GESENDET,
    JOB_ABGEBROCHEN,
    JOB_FERTIG,
    JOB_GEPLANT,
    JOB_VERWORFEN,
    JOB_WARTET,
    MODUS_AUS,
    MODUS_AUTOMATISCH,
    MODUS_FREIGABE,
    MODUS_KI,
    LagerBuchung,
    LagerDruckJob,
    LagerDruckRegel,
)
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.lager_autodruck import bedarf, konfig
from backend.app.services.lager_autodruck.service import (
    LagerAutodruckService,
    dateiname_aus_druck,
    startzeit,
)
from backend.app.services.lager_autodruck.supabase import LagerClient, LagerFehler

# --- Bedarfsrechnung -----------------------------------------------------------


def _daten(teile, komponenten=(), auftraege=(), positionen=()):
    return {
        "teile": list(teile),
        "komponenten": list(komponenten),
        "auftraege": list(auftraege),
        "positionen": list(positionen),
    }


def _regel(part_id="a", je_druck=1, **kw):
    return bedarf.RegelEingabe(regel_id=1, part_id=part_id, stueck_je_druck=je_druck, **kw)


def test_ueber_mindestbestand_nichts_zu_tun():
    daten = _daten([{"id": "a", "name": "A", "bestand": 6, "mindestbestand": 5}])
    kandidaten, uebersicht = bedarf.berechne(daten, [_regel()], {})
    assert kandidaten == []
    assert uebersicht[0].bestand == 6


def test_auf_doppelten_mindestbestand_auffuellen_in_ganzen_druecken():
    daten = _daten([{"id": "a", "name": "A", "bestand": "3", "mindestbestand": "5"}])
    (k,), _ = bedarf.berechne(daten, [_regel(je_druck=4)], {})
    # fehlend = 10 - 3 = 7 -> 2 Druecke a 4 Stueck
    assert (k.druecke, k.stueck) == (2, 8)


def test_in_arbeit_zaehlt_als_bestand_kein_doppelter_druck():
    daten = _daten([{"id": "a", "name": "A", "bestand": 3, "mindestbestand": 5}])
    kandidaten, _ = bedarf.berechne(daten, [_regel()], {"a": 7})
    assert kandidaten == []


def test_offene_auftraege_und_sets_erzeugen_bedarf():
    daten = _daten(
        teile=[
            {"id": "a", "name": "A", "bestand": 10, "mindestbestand": 2},
            {"id": "set", "name": "Set", "bestand": 0, "mindestbestand": 0},
        ],
        komponenten=[{"part_id": "set", "komponente_id": "a", "menge": 3}],
        auftraege=[{"id": 1, "status": "Offen"}],
        positionen=[
            {"order_id": 1, "part_id": "set", "menge": 4},  # 12x a
            {"order_id": 99, "part_id": "a", "menge": 100},  # Auftrag nicht offen
        ],
    )
    (k,), _ = bedarf.berechne(daten, [_regel()], {})
    assert k.nachfrage == 12
    assert k.verfuegbar == -2
    assert k.stueck == 6  # 2*2 - (-2)


def test_regel_auf_set_wird_uebersprungen():
    daten = _daten(
        teile=[{"id": "set", "name": "Set", "bestand": 0, "mindestbestand": 5}],
        komponenten=[{"part_id": "set", "komponente_id": "a", "menge": 1}],
    )
    kandidaten, uebersicht = bedarf.berechne(daten, [_regel("set")], {})
    assert kandidaten == []
    assert "Set" in uebersicht[0].hinweis


def test_mindestbestand_null_druckt_nur_bei_echtem_bedarf():
    daten = _daten([{"id": "a", "name": "A", "bestand": 0, "mindestbestand": 0}])
    assert bedarf.berechne(daten, [_regel()], {})[0] == []


def test_tageslimit_kappt_und_sperrt():
    daten = _daten([{"id": "a", "name": "A", "bestand": 0, "mindestbestand": 5}])
    (k,), _ = bedarf.berechne(daten, [_regel(max_drucke_pro_tag=3, heute_angelegt=1)], {})
    assert (k.druecke, k.gekappt) == (2, True)
    kandidaten, uebersicht = bedarf.berechne(daten, [_regel(max_drucke_pro_tag=3, heute_angelegt=3)], {})
    assert kandidaten == [] and uebersicht[0].hinweis == "Tageslimit erreicht"


def test_teil_fehlt_im_lager():
    kandidaten, uebersicht = bedarf.berechne(_daten([]), [_regel("weg")], {})
    assert kandidaten == [] and uebersicht[0].hinweis == "Teil im Lager nicht gefunden"


# --- Hilfsfunktionen ---------------------------------------------------------------


def test_dateiname_wie_frueherer_webhook():
    assert dateiname_aus_druck("Halter_links_v2", "x.3mf") == "Halter links v2"
    assert dateiname_aus_druck(None, "/data/Metadata/Halter.gcode.3mf") == "Halter"
    assert dateiname_aus_druck("", "") == "Unbekannt"


@pytest.fixture
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")


def test_startzeit_im_fenster_sofort(utc):
    assert startzeit("07:00", "22:00", datetime(2026, 9, 23, 12, 0)) is None
    assert startzeit(None, None, datetime(2026, 9, 23, 3, 0)) is None


def test_startzeit_vor_und_nach_dem_fenster(utc):
    assert startzeit("07:00", "22:00", datetime(2026, 9, 23, 5, 0)) == datetime(2026, 9, 23, 7, 0)
    assert startzeit("07:00", "22:00", datetime(2026, 9, 23, 23, 0)) == datetime(2026, 9, 24, 7, 0)


def test_startzeit_fenster_ueber_mitternacht(utc):
    assert startzeit("22:00", "06:00", datetime(2026, 9, 23, 2, 0)) is None
    assert startzeit("22:00", "06:00", datetime(2026, 9, 23, 12, 0)) == datetime(2026, 9, 23, 22, 0)


# --- Supabase-Zugang ------------------------------------------------------------------


async def test_lager_client_meldet_an_und_ruft_druck_verbuchen():
    anfragen = []

    def handler(request: httpx.Request) -> httpx.Response:
        anfragen.append(request)
        if request.url.path == "/auth/v1/token":
            return httpx.Response(200, json={"access_token": "tok", "refresh_token": "r", "expires_in": 3600})
        if request.url.path == "/rest/v1/rpc/druck_verbuchen":
            assert request.headers["authorization"] == "Bearer tok"
            assert request.headers["apikey"] == "anon"
            return httpx.Response(200, json="gebucht")
        return httpx.Response(404)

    client = LagerClient("https://lager.example/", "anon", "drucker@x", "geheim")
    client.transport = httpx.MockTransport(handler)
    ergebnis = await client.druck_verbuchen(
        drucker="P1S", dateiname="Halter", zeitstempel="t", gramm=12.5, status="fertig"
    )
    assert ergebnis == "gebucht"
    # Zweiter Aufruf nutzt das vorhandene Token - keine zweite Anmeldung.
    await client.druck_verbuchen(drucker="P1S", dateiname="Halter", zeitstempel="t2", gramm=None, status="fertig")
    assert [r.url.path for r in anfragen].count("/auth/v1/token") == 1


async def test_lager_client_falsches_passwort():
    client = LagerClient("https://lager.example", "anon", "drucker@x", "falsch")
    client.transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(LagerFehler, match="Anmeldung"):
        await client.lade_teile()


# --- Durchlauf gegen die Datenbank ---------------------------------------------------------


class FakeLager:
    def __init__(self, teile):
        self.teile = teile
        self.buchungen = []
        self.fehler = False
        self.antwort = "gebucht"

    async def lade_bestandsdaten(self):
        return _daten(self.teile)

    async def druck_verbuchen(self, **kw):
        if self.fehler:
            raise LagerFehler("offline")
        self.buchungen.append(kw)
        return self.antwort


@pytest.fixture
async def umgebung(test_engine, db_session, printer_factory, monkeypatch):
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    drucker = await printer_factory(name="P1S Werkstatt", model="P1S")
    archiv = PrintArchive(
        filename="Halter.gcode.3mf",
        file_path="archive/halter.3mf",
        file_size=1,
        print_name="Halter",
        filament_used_grams=12.5,
        sliced_for_model="P1S",
    )
    db_session.add(archiv)
    await db_session.commit()
    await konfig.speichern(
        db_session,
        konfig.Konfig(aktiv=True, supabase_url="https://x", supabase_anon_key="a", email="e", passwort="p"),
    )

    service = LagerAutodruckService()
    lager = FakeLager([{"id": "teil-1", "name": "Halter", "bestand": 1, "mindestbestand": 4}])
    monkeypatch.setattr(service, "lager_client", lambda k: lager)
    with patch("backend.app.core.database.async_session", sessions):
        yield service, lager, drucker, archiv, sessions


async def _regel_anlegen(sessions, archiv, drucker=None, **kw):
    async with sessions() as db:
        regel = LagerDruckRegel(
            part_id="teil-1",
            part_name="Halter",
            archive_id=archiv.id,
            dateiname="Halter",
            stueck_je_druck=kw.pop("stueck_je_druck", 2),
            modus=kw.pop("modus", MODUS_AUTOMATISCH),
            printer_id=drucker.id if drucker else None,
            **kw,
        )
        db.add(regel)
        await db.commit()
        return regel.id


async def _alle(sessions, modell):
    async with sessions() as db:
        return list((await db.execute(select(modell).order_by(modell.id))).scalars().all())


async def test_durchlauf_plant_fehlende_drucke_einmalig(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker)

    ergebnis = await service.durchlauf()
    # Bestand 1, Mindest 4 -> auf 8 auffuellen = 7 Stueck = 4 Druecke a 2
    assert ergebnis["angelegt"] == 4
    items = await _alle(sessions, PrintQueueItem)
    assert len(items) == 4
    assert all(i.printer_id == drucker.id and not i.manual_start and i.archive_id == archiv.id for i in items)
    jobs = await _alle(sessions, LagerDruckJob)
    assert {j.status for j in jobs} == {JOB_GEPLANT}
    assert jobs[0].freigegeben_von == "automatisch"

    # Zweiter Lauf: alles schon in Arbeit -> nichts Neues.
    assert (await service.durchlauf())["angelegt"] == 0
    assert len(await _alle(sessions, PrintQueueItem)) == 4


async def test_modus_freigabe_legt_manuellen_start_an(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker, modus=MODUS_FREIGABE, stueck_je_druck=10)
    await service.durchlauf()
    (item,) = await _alle(sessions, PrintQueueItem)
    assert item.manual_start is True
    (job,) = await _alle(sessions, LagerDruckJob)
    assert job.status == JOB_WARTET

    # In der Bambuddy-Warteschlange auf "Start" gedrueckt -> Abgleich erkennt Freigabe.
    async with sessions() as db:
        (await db.get(PrintQueueItem, item.id)).manual_start = False
        await db.commit()
    await service.durchlauf()
    (job,) = await _alle(sessions, LagerDruckJob)
    assert job.status == JOB_GEPLANT and job.freigegeben_von == "Bambuddy-Warteschlange"


async def test_modus_ki_ohne_schluessel_nutzt_feste_regel(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    lager.teile = [{"id": "teil-1", "name": "Halter", "bestand": 3, "mindestbestand": 4}]
    await _regel_anlegen(sessions, archiv, drucker, modus=MODUS_KI, stueck_je_druck=10)
    await service.durchlauf()
    (job,) = await _alle(sessions, LagerDruckJob)
    # verfuegbar 3 > 4/2 -> "niedrig" -> ohne Freigabe
    assert job.dringlichkeit == "niedrig" and job.status == JOB_GEPLANT


async def test_regel_aus_und_autodruck_aus(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker, modus=MODUS_AUS)
    assert (await service.durchlauf())["angelegt"] == 0
    assert service.uebersicht[0]["hinweis"] == "Regel pausiert"


async def test_modellbasiert_aus_archiv(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, None, stueck_je_druck=10)
    await service.durchlauf()
    (item,) = await _alle(sessions, PrintQueueItem)
    assert item.printer_id is None and item.target_model == "P1S"


async def test_druckende_verbucht_im_lager(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker, stueck_je_druck=10)
    await service.durchlauf()
    (item,) = await _alle(sessions, PrintQueueItem)

    await service.bei_druckende(drucker.id, {"status": "completed"}, item.id, archiv.id)

    (job,) = await _alle(sessions, LagerDruckJob)
    assert job.status == JOB_FERTIG
    (buchung,) = await _alle(sessions, LagerBuchung)
    assert buchung.zustand == BUCHUNG_GESENDET and buchung.ergebnis == "gebucht"
    assert lager.buchungen[0]["dateiname"] == "Halter"
    assert lager.buchungen[0]["drucker"] == "P1S Werkstatt"
    assert lager.buchungen[0]["gramm"] == 12.5
    assert lager.buchungen[0]["status"] == "fertig"

    # Doppelte Meldung desselben Drucks -> keine zweite Buchung.
    await service.bei_druckende(drucker.id, {"status": "completed"}, item.id, archiv.id)
    assert len(await _alle(sessions, LagerBuchung)) == 1


async def test_abbruch_bucht_nichts(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker, stueck_je_druck=10)
    await service.durchlauf()
    (item,) = await _alle(sessions, PrintQueueItem)
    await service.bei_druckende(drucker.id, {"status": "cancelled"}, item.id, archiv.id)
    (job,) = await _alle(sessions, LagerDruckJob)
    assert job.status == JOB_ABGEBROCHEN
    assert await _alle(sessions, LagerBuchung) == []


async def test_handdruck_wird_verbucht_und_bei_ausfall_nachgeholt(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    lager.fehler = True
    await service.bei_druckende(drucker.id, {"status": "failed", "subtask_name": "Deckel_klein"}, None, None)
    (buchung,) = await _alle(sessions, LagerBuchung)
    assert buchung.zustand == BUCHUNG_FEHLER and buchung.dateiname == "Deckel klein"

    lager.fehler = False
    await service.durchlauf()
    (buchung,) = await _alle(sessions, LagerBuchung)
    assert buchung.zustand == BUCHUNG_GESENDET
    assert lager.buchungen[0]["status"] == "fehldruck"


async def test_verpasstes_druckende_wird_beim_abgleich_nachgeholt(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker, stueck_je_druck=10)
    await service.durchlauf()
    (item,) = await _alle(sessions, PrintQueueItem)
    async with sessions() as db:
        eintrag = await db.get(PrintQueueItem, item.id)
        eintrag.status = "completed"
        eintrag.completed_at = datetime(2026, 9, 23, 10, 0)
        await db.commit()

    # Das Lager hat die Buchung verarbeitet -> Bestand gestiegen.
    lager.teile = [{"id": "teil-1", "name": "Halter", "bestand": 11, "mindestbestand": 4}]
    await service.durchlauf()
    (job,) = await _alle(sessions, LagerDruckJob)
    assert job.status == JOB_FERTIG
    assert len(lager.buchungen) == 1


async def test_fertig_aber_noch_nicht_gebucht_zaehlt_als_in_arbeit(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker, stueck_je_druck=10)
    await service.durchlauf()
    (item,) = await _alle(sessions, PrintQueueItem)
    lager.fehler = True  # Lager gerade nicht erreichbar
    await service.bei_druckende(drucker.id, {"status": "completed"}, item.id, archiv.id)
    lager.fehler = False

    # Buchung geht jetzt raus; im selben Lauf darf nicht neu geplant werden,
    # obwohl das Lager (hier: Fake) den Bestand noch nicht erhoeht hat.
    lager.fehler = True
    await service.durchlauf()
    assert len(await _alle(sessions, LagerDruckJob)) == 1


async def test_nicht_zugeordnete_buchung_haelt_regel_an(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    regel_id = await _regel_anlegen(sessions, archiv, drucker, stueck_je_druck=10)
    lager.antwort = "unbekannt"
    await service.durchlauf()
    (item,) = await _alle(sessions, PrintQueueItem)
    await service.bei_druckende(drucker.id, {"status": "completed"}, item.id, archiv.id)

    await service.durchlauf()
    assert len(await _alle(sessions, LagerDruckJob)) == 1
    assert "Angehalten" in service.uebersicht[0]["hinweis"]

    # Regel neu speichern hebt die Sperre auf.
    async with sessions() as db:
        (await db.get(LagerDruckRegel, regel_id)).updated_at = datetime(2100, 1, 1)
        await db.commit()
    await service.durchlauf()
    assert len(await _alle(sessions, LagerDruckJob)) == 2


async def test_geloeschter_warteschlangeneintrag_gibt_bedarf_frei(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker, stueck_je_druck=10)
    await service.durchlauf()
    (item,) = await _alle(sessions, PrintQueueItem)
    async with sessions() as db:
        await db.delete(await db.get(PrintQueueItem, item.id))
        await db.commit()

    await service.durchlauf()
    jobs = await _alle(sessions, LagerDruckJob)
    assert [j.status for j in jobs] == [JOB_VERWORFEN, JOB_GEPLANT]


async def test_konfig_verschluesselt_geheimnisse(db_session):
    await konfig.speichern(db_session, konfig.Konfig(passwort="geheim", anthropic_api_key="sk-test"))
    geladen = await konfig.laden(db_session)
    assert geladen.passwort == "geheim" and geladen.anthropic_api_key == "sk-test"
