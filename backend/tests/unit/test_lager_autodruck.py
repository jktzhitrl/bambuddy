"""Tests fuer das Fork-Modul Lager-Autodruck."""

from datetime import date, datetime, timedelta
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
from backend.app.services.lager_autodruck import bedarf, konfig, nachtruhe
from backend.app.services.lager_autodruck.service import (
    LagerAutodruckService,
    dateiname_aus_druck,
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


def test_fertige_sets_im_lager_decken_bedarf_zuerst():
    daten = _daten(
        teile=[
            {"id": "a", "name": "A", "bestand": 0, "mindestbestand": 0},
            {"id": "set", "name": "Set", "bestand": 3, "mindestbestand": 0},
        ],
        komponenten=[{"part_id": "set", "komponente_id": "a", "menge": 2}],
        auftraege=[{"id": 1}, {"id": 2}],
        positionen=[{"order_id": 1, "part_id": "set", "menge": 4}, {"order_id": 2, "part_id": "set", "menge": 1}],
    )
    # 5 Sets bestellt, 3 fertig im Regal -> nur 2 Sets aus Einzelteilen = 4x A
    assert bedarf.nachfrage_je_teil(daten) == {"a": 4}


def test_set_im_set():
    daten = _daten(
        teile=[
            {"id": "gross", "name": "Gross", "bestand": 0},
            {"id": "klein", "name": "Klein", "bestand": 1},
            {"id": "a", "name": "A", "bestand": 0},
        ],
        komponenten=[
            {"part_id": "gross", "komponente_id": "klein", "menge": 2},
            {"part_id": "klein", "komponente_id": "a", "menge": 3},
        ],
        auftraege=[{"id": 1}],
        positionen=[{"order_id": 1, "part_id": "gross", "menge": 1}],
    )
    # 1 Gross = 2 Klein, 1 Klein liegt da -> 1 Klein bauen = 3x A
    assert bedarf.nachfrage_je_teil(daten) == {"a": 3}


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


def _ruhe(puffer=0):
    return nachtruhe.Nachtruhe.aus_text("22:00", "07:00", puffer)


def test_nachtruhe_kurzer_druck_vor_dem_schlafen(utc):
    # 18:00 + 3h = 21:00 -> fertig vor 22:00
    assert nachtruhe.fruehester_start(_ruhe(), datetime(2026, 9, 23, 18, 0), timedelta(hours=3)) is None


def test_nachtruhe_verschiebt_bis_fertig_zum_aufstehen(utc):
    # 20:00 + 10h = 06:00 -> mitten in der Nacht; Start 21:00, fertig 07:00
    start = nachtruhe.fruehester_start(_ruhe(), datetime(2026, 9, 23, 20, 0), timedelta(hours=10))
    assert start == datetime(2026, 9, 23, 21, 0)


def test_nachtruhe_puffer_zaehlt_mit(utc):
    # 19:00 + 3h = 22:00 waere knapp ok, mit 15 Min Puffer nicht mehr -> fertig 07:00
    start = nachtruhe.fruehester_start(_ruhe(15), datetime(2026, 9, 23, 19, 0), timedelta(hours=3))
    assert start == datetime(2026, 9, 24, 3, 45)


def test_nachtruhe_nachts_eingeplant_endet_nach_dem_aufstehen(utc):
    # 23:00 + 2h = 01:00 -> verschieben auf Start 05:00, fertig 07:00
    start = nachtruhe.fruehester_start(_ruhe(), datetime(2026, 9, 23, 23, 0), timedelta(hours=2))
    assert start == datetime(2026, 9, 24, 5, 0)
    # 23:00 + 9h = 08:00 -> nach dem Aufstehen, darf sofort
    assert nachtruhe.fruehester_start(_ruhe(), datetime(2026, 9, 23, 23, 0), timedelta(hours=9)) is None


def test_nachtruhe_sehr_langer_druck(utc):
    # 30h-Druck um 12:00 endet 18:00 am Folgetag -> erlaubt
    assert nachtruhe.fruehester_start(_ruhe(), datetime(2026, 9, 23, 12, 0), timedelta(hours=30)) is None
    # 30h-Druck um 20:00 endet 02:00 uebermorgen -> fertig 07:00 statt dessen
    start = nachtruhe.fruehester_start(_ruhe(), datetime(2026, 9, 23, 20, 0), timedelta(hours=30))
    assert start == datetime(2026, 9, 24, 1, 0)


def test_nachtruhe_grenze(utc):
    # 3h-Druck um 12:00: darf bis 19:00 starten (fertig 22:00)
    assert nachtruhe.naechste_grenze(_ruhe(), datetime(2026, 9, 23, 12, 0), timedelta(hours=3)) == datetime(
        2026, 9, 23, 19, 0
    )


def test_nachtruhe_ungueltig_oder_aus():
    assert nachtruhe.Nachtruhe.aus_text("22:00", "22:00", 0) is None
    assert nachtruhe.Nachtruhe.aus_text("abc", "07:00", 0) is None
    assert konfig.Konfig(nachtruhe_aktiv=False).nachtruhe() is None


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
        self.auftraege = []
        self.positionen = []
        self.packdaten = {}
        self.buchungen = []
        self.fehler = False
        self.antwort = "gebucht"

    async def lade_bestandsdaten(self):
        return _daten(self.teile, auftraege=self.auftraege, positionen=self.positionen)

    async def lade_packdaten(self):
        return {"teile": self.teile, "auftraege": self.auftraege, "positionen": self.positionen, **self.packdaten}

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
        konfig.Konfig(
            aktiv=True, supabase_url="https://x", supabase_anon_key="a", email="e", passwort="p", nachtruhe_aktiv=False
        ),
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


async def test_ki_nur_mit_schalter(umgebung, db_session):
    service, lager, drucker, archiv, sessions = umgebung
    await konfig.speichern(
        db_session,
        konfig.Konfig(
            aktiv=True,
            supabase_url="https://x",
            supabase_anon_key="a",
            email="e",
            passwort="p",
            anthropic_api_key="sk-test",
            ki_verwenden=False,
        ),
    )
    await _regel_anlegen(sessions, archiv, drucker, modus=MODUS_KI, stueck_je_druck=10)
    aufrufe = []

    async def fake_ki(kandidaten, *, api_key, modell=None):
        aufrufe.append(api_key)
        return {}

    with patch("backend.app.services.lager_autodruck.ki.einschaetzen", fake_ki):
        await service.durchlauf()
    # Schluessel hinterlegt, aber Schalter aus -> kein kostenpflichtiger Aufruf.
    assert aufrufe == [None]
    (job,) = await _alle(sessions, LagerDruckJob)
    assert job.begruendung.startswith("Bestand")


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


async def test_nachtruhe_haelt_langen_druck_zurueck(umgebung, db_session, utc):
    service, lager, drucker, archiv, sessions = umgebung
    await konfig.speichern(
        db_session,
        konfig.Konfig(
            aktiv=True,
            supabase_url="https://x",
            supabase_anon_key="a",
            email="e",
            passwort="p",
            nachtruhe_aktiv=True,
            schlafen="22:00",
            aufstehen="07:00",
            puffer_minuten=0,
        ),
    )
    async with sessions() as db:
        (await db.get(PrintArchive, archiv.id)).print_time_seconds = 10 * 3600
        await db.commit()
    await _regel_anlegen(sessions, archiv, drucker, stueck_je_druck=10)

    with patch("backend.app.services.lager_autodruck.service.utcnow_naive", return_value=datetime(2026, 9, 23, 20, 0)):
        await service.durchlauf()
    (item,) = await _alle(sessions, PrintQueueItem)
    # 20:00 + 10h waere 06:00 -> Start 21:00, fertig 07:00
    assert item.scheduled_time == datetime(2026, 9, 23, 21, 0)
    assert service.naechste_grenze is None

    # Morgens um 10 darf derselbe Druck sofort (fertig 20:00); Grenze 12:00.
    with patch("backend.app.services.lager_autodruck.service.utcnow_naive", return_value=datetime(2026, 9, 24, 10, 0)):
        await service.durchlauf()
    (item,) = await _alle(sessions, PrintQueueItem)
    assert item.scheduled_time is None
    assert service.naechste_grenze == datetime(2026, 9, 24, 12, 0)


# --- Benachrichtigungen -------------------------------------------------------------


@pytest.fixture
async def gemeldet(umgebung, db_session):
    """Kanal anlegen, im Autodruck auswaehlen und gesendete Meldungen mitschneiden."""
    from backend.app.models.notification import NotificationProvider

    kanal = NotificationProvider(name="Handy", provider_type="ntfy", config="{}", enabled=True)
    db_session.add(kanal)
    await db_session.commit()
    k = await konfig.laden(db_session)
    k.melden_an = [kanal.id]
    k.melden = ["freigabe", "buchungsfehler", "angehalten", "lager_offline"]
    await konfig.speichern(db_session, k)

    meldungen = []

    async def fake_senden(providers, title, message, db, event_type="unknown", **kw):
        meldungen.append((event_type, title, message, [p.name for p in providers]))

    with patch("backend.app.services.notification_service.notification_service._send_to_providers", fake_senden):
        yield meldungen


async def test_meldung_bei_freigabe(umgebung, gemeldet):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker, modus=MODUS_FREIGABE, stueck_je_druck=10)
    await service.durchlauf()
    ((ereignis, titel, text, kanaele),) = gemeldet
    assert ereignis == "lager_autodruck_freigabe" and kanaele == ["Handy"]
    assert "Halter" in text
    # Naechster Lauf plant nichts Neues -> keine neue Meldung.
    await service.durchlauf()
    assert len(gemeldet) == 1


async def test_eingeplant_nur_wenn_gewaehlt(umgebung, gemeldet):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker, modus=MODUS_AUTOMATISCH, stueck_je_druck=10)
    await service.durchlauf()
    assert gemeldet == []  # "eingeplant" ist standardmaessig aus


async def test_meldung_nach_drei_buchungsfehlern_einmal(umgebung, gemeldet):
    service, lager, drucker, archiv, sessions = umgebung
    lager.fehler = True
    await service.bei_druckende(drucker.id, {"status": "completed", "subtask_name": "Deckel"}, None, None)
    for _ in range(4):
        await service.durchlauf()
    buchungsfehler = [m for m in gemeldet if m[0] == "lager_autodruck_buchungsfehler"]
    assert len(buchungsfehler) == 1 and "Deckel" in buchungsfehler[0][2]


async def test_meldung_wenn_lager_nicht_verbucht(umgebung, gemeldet):
    service, lager, drucker, archiv, sessions = umgebung
    lager.antwort = "unbekannt"
    await service.bei_druckende(drucker.id, {"status": "completed", "subtask_name": "Deckel"}, None, None)
    ((ereignis, titel, text, _),) = gemeldet
    assert ereignis == "lager_autodruck_buchungsfehler" and "unbekannt" in text


async def test_meldung_regel_angehalten_einmal(umgebung, gemeldet):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker, stueck_je_druck=10)
    lager.antwort = "unbekannt"
    await service.durchlauf()
    (item,) = await _alle(sessions, PrintQueueItem)
    await service.bei_druckende(drucker.id, {"status": "completed"}, item.id, archiv.id)
    await service.durchlauf()
    await service.durchlauf()
    angehalten = [m for m in gemeldet if m[0] == "lager_autodruck_angehalten"]
    assert len(angehalten) == 1


async def test_meldung_lager_offline_und_wieder_da(umgebung, gemeldet, monkeypatch):
    service, lager, drucker, archiv, sessions = umgebung
    await _regel_anlegen(sessions, archiv, drucker, stueck_je_druck=10)

    async def kaputt():
        raise LagerFehler("Zeitueberschreitung")

    original = lager.lade_bestandsdaten
    monkeypatch.setattr(lager, "lade_bestandsdaten", kaputt)
    for _ in range(4):
        await service.durchlauf()
    assert [m[1] for m in gemeldet] == ["Lager-Autodruck: Lager nicht erreichbar"]

    monkeypatch.setattr(lager, "lade_bestandsdaten", original)
    await service.durchlauf()
    assert gemeldet[-1][1] == "Lager-Autodruck: Lager wieder erreichbar"


async def test_freigabe_meldung_nachts_gesammelt_und_morgens_geschickt(umgebung, gemeldet, db_session, utc):
    service, lager, drucker, archiv, sessions = umgebung
    k = await konfig.laden(db_session)
    k.nachtruhe_aktiv, k.schlafen, k.aufstehen, k.puffer_minuten = True, "22:00", "07:00", 0
    await konfig.speichern(db_session, k)
    await _regel_anlegen(sessions, archiv, drucker, modus=MODUS_FREIGABE, stueck_je_druck=2)
    zeit = "backend.app.services.lager_autodruck.service.utcnow_naive"

    with patch(zeit, return_value=datetime(2026, 9, 23, 23, 0)):
        await service.durchlauf()
    assert len(await _alle(sessions, LagerDruckJob)) == 4
    assert gemeldet == []  # nachts nichts

    with patch(zeit, return_value=datetime(2026, 9, 24, 7, 5)):
        await service.durchlauf()
    ((ereignis, titel, text, _),) = gemeldet
    assert ereignis == "lager_autodruck_freigabe"
    assert "4 Drucke Halter (8 Stück" in text  # zusammengefasst

    with patch(zeit, return_value=datetime(2026, 9, 24, 8, 0)):
        await service.durchlauf()
    assert len(gemeldet) == 1  # nur einmal


async def test_schema_nachziehen_ist_harmlos(test_engine):
    from backend.app.services.lager_autodruck.service import schema_nachziehen

    with patch("backend.app.core.database.engine", test_engine):
        await schema_nachziehen()
        await schema_nachziehen()


async def test_schema_nachziehen_ergaenzt_fehlende_spalte(tmp_path):
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.app.services.lager_autodruck.service import schema_nachziehen

    alt = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'alt.db'}")
    async with alt.begin() as conn:
        await conn.execute(text("CREATE TABLE lager_druck_jobs (id INTEGER PRIMARY KEY, created_at TIMESTAMP)"))
        await conn.execute(text("INSERT INTO lager_druck_jobs (id, created_at) VALUES (1, '2026-09-23 10:00:00')"))
    with patch("backend.app.core.database.engine", alt):
        await schema_nachziehen()
    async with alt.connect() as conn:
        spalten = await conn.run_sync(lambda c: {s["name"] for s in inspect(c).get_columns("lager_druck_jobs")})
        gemeldet = (await conn.execute(text("SELECT gemeldet_at FROM lager_druck_jobs"))).scalar()
    await alt.dispose()
    assert "gemeldet_at" in spalten and gemeldet is not None


async def test_vorschau_bei_ausgeschaltetem_autodruck(umgebung, db_session):
    service, lager, drucker, archiv, sessions = umgebung
    k = await konfig.laden(db_session)
    k.aktiv = False
    k.ki_verwenden, k.anthropic_api_key = True, "sk-test"
    await konfig.speichern(db_session, k)
    await _regel_anlegen(sessions, archiv, drucker, modus=MODUS_FREIGABE, stueck_je_druck=2)
    aufrufe = []

    async def fake_ki(kandidaten, *, api_key, modell=None):
        aufrufe.append(api_key)
        return {}

    with patch("backend.app.services.lager_autodruck.ki.einschaetzen", fake_ki):
        ergebnis = await service.durchlauf()
    assert ergebnis["wuerde_anlegen"] == 1
    assert await _alle(sessions, PrintQueueItem) == [] and await _alle(sessions, LagerDruckJob) == []
    (v,) = service.vorschau
    assert (v["name"], v["druecke"], v["stueck"], v["ohne_freigabe"]) == ("Halter", 4, 8, False)
    assert service.uebersicht[0]["bestand"] == 1
    assert aufrufe == [None]  # Vorschau fragt die KI nicht (kostet nichts)


# --- Liefertermine -------------------------------------------------------------


def test_termin_frueheste_je_teil_fertige_sets_zuerst_an_fruehe_auftraege():
    daten = _daten(
        [{"id": "set", "bestand": 1}, {"id": "a", "bestand": 0}, {"id": "b", "bestand": 0}],
        komponenten=[{"part_id": "set", "komponente_id": "a", "menge": 1}],
        auftraege=[
            {"id": "spaet", "versand_bis": "2026-10-20"},
            {"id": "frueh", "versand_bis": "2026-10-01T00:00:00+00:00"},
            {"id": "ohne", "versand_bis": None},
        ],
        positionen=[
            {"order_id": "spaet", "part_id": "set", "menge": 1},
            {"order_id": "frueh", "part_id": "set", "menge": 1},
            {"order_id": "ohne", "part_id": "b", "menge": 2},
            {"order_id": "frueh", "part_id": "b", "menge": 1},
        ],
    )
    nachfrage, termine = bedarf.bedarf_je_teil(daten)
    # Das fertige Set geht an den fruehen Auftrag, fuer "a" bleibt der spaete.
    assert nachfrage == {"a": 1, "b": 3}
    assert termine == {"a": date(2026, 10, 20), "b": date(2026, 10, 1)}
    assert bedarf.nachfrage_je_teil(daten) == nachfrage


def _kandidat(**kw):
    werte = {
        "regel_id": 1,
        "part_id": "a",
        "name": "A",
        "kategorie": None,
        "bestand": 0,
        "mindestbestand": 10,
        "nachfrage": 3,
        "in_arbeit": 0,
        "verfuegbar": 7,
        "druecke": 1,
        "stueck": 1,
    }
    werte.update(kw)
    return bedarf.Kandidat(**werte)


def test_termin_hebt_dringlichkeit_nur_wenn_bestand_nicht_reicht():
    heute = date(2026, 9, 24)
    ohne = _kandidat(bestand=5, verfuegbar=2, mindestbestand=4)
    assert bedarf.regel_dringlichkeit(ohne, heute) == "mittel"
    assert bedarf.regel_dringlichkeit(_kandidat(bestand=2, termin=date(2026, 9, 26)), heute) == "hoch"
    assert bedarf.regel_dringlichkeit(_kandidat(bestand=2, termin=date(2026, 9, 20)), heute) == "hoch"  # ueberfaellig
    niedrig = {"bestand": 2, "verfuegbar": 9, "mindestbestand": 4}
    assert bedarf.regel_dringlichkeit(_kandidat(**niedrig, termin=date(2026, 9, 30)), heute) == "mittel"
    assert bedarf.regel_dringlichkeit(_kandidat(**niedrig, termin=date(2026, 10, 30)), heute) == "niedrig"
    # Bestand + laufende Drucke decken die Auftraege: Termin egal.
    gedeckt = _kandidat(bestand=2, in_arbeit=1, nachfrage=3, verfuegbar=9, mindestbestand=4, termin=date(2026, 9, 25))
    assert bedarf.regel_dringlichkeit(gedeckt, heute) == "niedrig"
    assert bedarf.mindestens("niedrig", "hoch") == "hoch" and bedarf.mindestens("hoch", "mittel") == "hoch"


async def test_naher_termin_kommt_zuerst_in_die_warteschlange(umgebung, db_session):
    service, lager, drucker, archiv, sessions = umgebung
    lager.teile = [
        {"id": "teil-1", "name": "Halter", "bestand": 0, "mindestbestand": 0},
        {"id": "teil-2", "name": "Ring", "bestand": 0, "mindestbestand": 0},
    ]
    lager.auftraege = [{"id": "o1", "versand_bis": "2099-01-10"}, {"id": "o2", "versand_bis": "2099-01-05"}]
    lager.positionen = [
        {"order_id": "o1", "part_id": "teil-1", "menge": 1},
        {"order_id": "o2", "part_id": "teil-2", "menge": 1},
    ]
    async with sessions() as db:
        for part_id, name in (("teil-1", "Halter"), ("teil-2", "Ring")):
            db.add(
                LagerDruckRegel(
                    part_id=part_id,
                    part_name=name,
                    archive_id=archiv.id,
                    dateiname=name,
                    stueck_je_druck=1,
                    modus=MODUS_AUTOMATISCH,
                    printer_id=drucker.id,
                )
            )
        await db.commit()
    # Schon ein Druck in der Warteschlange - "hoch" wird davor eingereiht.
    db_session.add(PrintQueueItem(printer_id=drucker.id, archive_id=archiv.id, position=1, status="pending"))
    await db_session.commit()

    await service.durchlauf()
    jobs = {j.part_id: j for j in await _alle(sessions, LagerDruckJob)}
    assert jobs["teil-1"].dringlichkeit == jobs["teil-2"].dringlichkeit == "hoch"  # Bestand 0, Bedarf da
    items = {i.id: i.position for i in await _alle(sessions, PrintQueueItem)}
    reihenfolge = sorted(items, key=items.get)
    # Frueherer Termin (Ring) zuerst, dann Halter, dann der alte Eintrag.
    assert reihenfolge == [jobs["teil-2"].queue_item_id, jobs["teil-1"].queue_item_id, 1]
    assert "Versand bis 05.01." in jobs["teil-2"].begruendung
    assert service.uebersicht[1]["termin"] == date(2099, 1, 5)


# --- Telegram-Knoepfe ------------------------------------------------------------


async def _wartende_auftraege(umgebung, stueck=10):
    service, lager, drucker, archiv, sessions = umgebung
    regel_id = await _regel_anlegen(sessions, archiv, drucker, modus=MODUS_FREIGABE, stueck_je_druck=stueck)
    await service.durchlauf()
    jobs = await _alle(sessions, LagerDruckJob)
    assert jobs and {j.status for j in jobs} == {JOB_WARTET}
    return regel_id, jobs


async def test_knopf_freigeben_bis_zur_nachricht(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    regel_id, jobs = await _wartende_auftraege(umgebung, stueck=2)  # 4 Drucke
    antwort = await service.knopf_ausfuehren(f"f:{regel_id}:{jobs[2].id}", "Telegram (Ben)")
    assert antwort == "3 Drucke freigegeben"
    jobs = await _alle(sessions, LagerDruckJob)
    assert [j.status for j in jobs] == [JOB_GEPLANT] * 3 + [JOB_WARTET]
    assert jobs[0].freigegeben_von == "Telegram (Ben)"
    items = {i.id: i for i in await _alle(sessions, PrintQueueItem)}
    assert [items[j.queue_item_id].manual_start for j in jobs] == [False, False, False, True]
    # Zweimal gedrueckt (oder Telegram liefert erneut): nichts passiert.
    assert await service.knopf_ausfuehren(f"f:{regel_id}:{jobs[2].id}", "x") == "Schon erledigt - nichts wartet mehr."


async def test_knopf_verwerfen(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    regel_id, jobs = await _wartende_auftraege(umgebung)
    assert await service.knopf_ausfuehren(f"v:{regel_id}:{jobs[-1].id}", "x") == "1 Druck verworfen"
    assert await _alle(sessions, PrintQueueItem) == []
    assert (await _alle(sessions, LagerDruckJob))[0].status == JOB_VERWORFEN


async def test_knopf_unbekannt(umgebung):
    service = umgebung[0]
    for daten in ("", "x:1", "f:1", "f:a:b", "p:"):
        assert await service.knopf_ausfuehren(daten, "x") == "Unbekannter Knopf."


async def test_knopf_platte_frei(umgebung):
    service, lager, drucker, archiv, sessions = umgebung
    from backend.app.services.printer_manager import printer_manager

    wartet = {drucker.id}
    with (
        patch.object(printer_manager, "is_awaiting_plate_clear", lambda pid: pid in wartet),
        patch.object(printer_manager, "set_awaiting_plate_clear", lambda pid, a: wartet.discard(pid)),
    ):
        assert await service.knopf_ausfuehren(f"p:{drucker.id}", "x") == "Platte frei - nächster Druck kann starten"
        assert wartet == set()
        assert await service.knopf_ausfuehren(f"p:{drucker.id}", "x") == "Platte war schon als frei gemeldet."


@pytest.fixture
async def telegram_kanal(umgebung, db_session, gemeldet):
    """Zusaetzlich zum ntfy-Kanal ein Telegram-Kanal; Telegram-Nachrichten mitschneiden."""
    import json

    from backend.app.models.notification import NotificationProvider

    kanal = NotificationProvider(
        name="Telegram",
        provider_type="telegram",
        config=json.dumps({"bot_token": "123:abc", "chat_id": "42"}),
        enabled=True,
    )
    db_session.add(kanal)
    await db_session.commit()
    k = await konfig.laden(db_session)
    k.melden_an = [*k.melden_an, kanal.id]
    k.melden = [*k.melden, "platte"]
    await konfig.speichern(db_session, k)

    nachrichten = []

    async def fake_nachricht(bot, text, markup=None, foto=None):
        nachrichten.append((bot.chat_id, text, markup, foto))
        return True

    with patch("backend.app.services.lager_autodruck.telegram.nachricht", fake_nachricht):
        yield nachrichten


async def test_freigabe_meldung_mit_knoepfen_auf_telegram(umgebung, gemeldet, telegram_kanal):
    regel_id, jobs = await _wartende_auftraege(umgebung, stueck=2)
    ((chat, text, markup, foto),) = telegram_kanal
    assert chat == "42" and "Halter" in text and foto is None
    (zeile,) = markup["inline_keyboard"]
    assert [k["callback_data"] for k in zeile] == [f"f:{regel_id}:{jobs[-1].id}", f"v:{regel_id}:{jobs[-1].id}"]
    # Die anderen Kanaele bekommen die Sammelnachricht, Telegram nicht doppelt.
    ((_, _, _, kanaele),) = gemeldet
    assert kanaele == ["Handy"]


async def test_ohne_knoepfe_normale_meldung_auch_auf_telegram(umgebung, gemeldet, telegram_kanal, db_session):
    k = await konfig.laden(db_session)
    k.telegram_knoepfe = False
    await konfig.speichern(db_session, k)
    await _wartende_auftraege(umgebung)
    assert telegram_kanal == []
    ((_, _, _, kanaele),) = gemeldet
    assert sorted(kanaele) == ["Handy", "Telegram"]


async def test_platte_belegt_schickt_foto_und_knopf(umgebung, gemeldet, telegram_kanal, db_session):
    service, lager, drucker, archiv, sessions = umgebung
    from backend.app.api.routes.settings import get_setting
    from backend.app.core.db_dialect import upsert_setting
    from backend.app.models.settings import Settings

    async def kamerabild(printer_id, printer):
        return b"jpeg"

    with patch("backend.app.services.lager_autodruck.service._kamerabild", kamerabild):
        # Ohne "Platte bestaetigen" in Bambuddy: keine Meldung.
        await service.bei_platte_belegt(drucker.id)
        assert telegram_kanal == [] and gemeldet == []

        await upsert_setting(db_session, Settings, "require_plate_clear", "true")
        await db_session.commit()
        assert await get_setting(db_session, "require_plate_clear") == "true"
        await service.bei_platte_belegt(drucker.id)

    ((chat, text, markup, foto),) = telegram_kanal
    assert drucker.name in text and foto == b"jpeg"
    assert markup["inline_keyboard"][0][0]["callback_data"] == f"p:{drucker.id}"
    ((ereignis, _, _, kanaele),) = gemeldet
    assert ereignis == "lager_autodruck_platte" and kanaele == ["Handy"]


def _telegram_client(antworten, aufrufe):
    def handler(request: httpx.Request) -> httpx.Response:
        methode = request.url.path.rsplit("/", 1)[-1]
        aufrufe.append((methode, request))
        ergebnis = antworten.get(methode, True)
        if callable(ergebnis):
            ergebnis = ergebnis(request)
        return httpx.Response(200, json={"ok": True, "result": ergebnis})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_rueckkanal_nur_eigener_chat_und_nachricht_wird_aktualisiert():
    import json

    from backend.app.services.lager_autodruck import telegram

    ausgefuehrt = []

    async def ausfuehren(daten, wer):
        ausgefuehrt.append((daten, wer))
        return "1 Druck freigegeben"

    updates = [
        {
            "update_id": 7,
            "callback_query": {
                "id": "q1",
                "data": "f:1:2",
                "from": {"first_name": "Mallory"},
                "message": {"message_id": 5, "chat": {"id": 666}, "text": "Freigabe nötig"},
            },
        },
        {
            "update_id": 8,
            "callback_query": {
                "id": "q2",
                "data": "f:1:2",
                "from": {"first_name": "Ben"},
                "message": {"message_id": 6, "chat": {"id": 42}, "text": "Freigabe nötig"},
            },
        },
    ]
    aufrufe = []
    rueckkanal = telegram.Rueckkanal(ausfuehren)
    bot = telegram.Bot(provider_id=1, token="123:abc", chat_id="42")

    def get_updates(request):
        # Mit offset sind die Updates bestaetigt - Telegram liefert sie nicht nochmal.
        return [] if "offset" in json.loads(request.content) else updates

    async with _telegram_client({"getUpdates": get_updates}, aufrufe) as client:
        assert await rueckkanal.abholen(client, [bot]) is True
        # Beim naechsten Abholen werden die gesehenen Updates bestaetigt.
        await rueckkanal.abholen(client, [bot])

    assert ausgefuehrt == [("f:1:2", "Telegram (Ben)")]
    methoden = [m for m, _ in aufrufe]
    assert methoden == ["getUpdates", "answerCallbackQuery", "answerCallbackQuery", "editMessageText", "getUpdates"]
    bearbeitet = json.loads(aufrufe[3][1].content)
    assert bearbeitet["chat_id"] == "42" and bearbeitet["message_id"] == 6
    assert bearbeitet["text"] == "Freigabe nötig\n\n→ 1 Druck freigegeben (Ben)"
    assert json.loads(aufrufe[4][1].content)["offset"] == 9


async def test_rueckkanal_fehler_von_telegram():
    from backend.app.services.lager_autodruck import telegram

    def handler(request):
        return httpx.Response(409, json={"ok": False, "description": "Conflict: terminated by other getUpdates"})

    async def ausfuehren(daten, wer):
        raise AssertionError("darf nicht laufen")

    bot = telegram.Bot(provider_id=1, token="123:abc", chat_id="42")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await telegram.Rueckkanal(ausfuehren).abholen(client, [bot]) is False


# --- Packliste ---------------------------------------------------------------------


def _packdaten(**kw):
    daten = {
        "teile": [
            {"id": "halter", "name": "Halter", "bestand": 5, "lagerort": "Dachboden"},
            {"id": "ring", "name": "Ring", "bestand": 20},
            {"id": "set", "name": "System", "bestand": 1},
        ],
        "komponenten": [
            {"part_id": "set", "komponente_id": "halter", "menge": 1},
            {"part_id": "set", "komponente_id": "ring", "menge": 9},
        ],
        "lagerorte": [
            {"part_id": "ring", "ort": "Regal B3", "menge": 20},
            {"part_id": "ring", "ort": "leer", "menge": 0},
        ],
        "kameras": [{"typ": "IMX179", "bestand": 6}],
        "auftraege": [],
        "positionen": [],
        "sonderposten": [],
    }
    daten.update(kw)
    return daten


def test_packliste_set_aus_lager_und_aus_einzelteilen():
    from backend.app.services.lager_autodruck import packliste

    daten = _packdaten(
        auftraege=[
            {
                "id": "a",
                "kunde": "Müller",
                "status": "Offen",
                "versand_bis": "2026-10-01",
                "kamera": "IMX179",
                "system_anzahl": 2,
            }
        ],
        positionen=[
            {"order_id": "a", "part_id": "set", "menge": 2},
            {"order_id": "a", "part_id": "halter", "menge": 1},
        ],
        sonderposten=[{"order_id": "a", "beschreibung": "Montage", "menge": 1}],
    )
    (p,) = packliste.packbare_auftraege(daten)
    assert (p.kunde, p.versand_bis, p.kameras) == ("Müller", date(2026, 10, 1), ("IMX179", 6))  # 2 Systeme x 3
    system, halter = p.positionen
    assert system.aus_lager == 1 and system.zusammenbauen == [
        ("Halter", 1, ["Dachboden"]),
        ("Ring", 9, ["Regal B3 (20)"]),
    ]
    assert halter.orte == ["Dachboden"]
    text = packliste.als_text(p)
    assert "📦 Müller – Versand bis 01.10." in text
    assert "1 fertig im Lager, 1 zusammenbauen aus:" in text
    assert "– 9× Ring – Regal B3 (20)" in text
    assert "6× Kamera IMX179" in text and "1× Montage (Sonderposten" in text


def test_packliste_fehlendes_oder_zu_wenig_nicht_packbar():
    from backend.app.services.lager_autodruck import packliste

    auftraege = [
        {"id": "zu_viel", "kunde": "A", "status": "Offen", "versand_bis": "2026-10-01"},
        {"id": "kamera", "kunde": "B", "status": "Offen", "kamera": "IMX179", "system_anzahl": 3},
        {"id": "unbekannt", "kunde": "C", "status": "Offen"},
        {"id": "nur_sonder", "kunde": "D", "status": "Offen"},
    ]
    positionen = [
        {"order_id": "zu_viel", "part_id": "halter", "menge": 6},
        {"order_id": "kamera", "part_id": "ring", "menge": 1},
        {"order_id": "unbekannt", "part_id": "gibtsnicht", "menge": 1},
    ]
    daten = _packdaten(auftraege=auftraege, positionen=positionen)
    assert packliste.packbare_auftraege(daten) == []  # 9 Kameras gebraucht, 6 da


def test_packliste_verteilt_bestand_nach_termin_und_fertig_zuerst():
    from backend.app.services.lager_autodruck import packliste

    auftraege = [
        {"id": "spaet", "kunde": "Spät", "status": "Offen", "versand_bis": "2026-10-20"},
        {"id": "frueh", "kunde": "Früh", "status": "In Arbeit", "versand_bis": "2026-10-02"},
        {"id": "gepackt", "kunde": "Gepackt", "status": "Fertig", "versand_bis": "2026-12-01"},
        {"id": "gross", "kunde": "Groß", "status": "Offen", "versand_bis": "2026-09-30"},
    ]
    positionen = [
        {"order_id": "spaet", "part_id": "halter", "menge": 2},
        {"order_id": "frueh", "part_id": "halter", "menge": 2},
        {"order_id": "gepackt", "part_id": "halter", "menge": 2},
        {"order_id": "gross", "part_id": "halter", "menge": 50},
    ]
    listen = packliste.packbare_auftraege(_packdaten(auftraege=auftraege, positionen=positionen))
    # 5 Halter: 2 liegen schon gepackt, "Groß" geht nicht auf und hält nichts fest,
    # dann bekommt der frühere Termin die restlichen 3 - für "Spät" reicht es nicht mehr.
    assert [p.kunde for p in listen] == ["Früh"]


async def test_packliste_meldet_jede_bestellung_einmal(umgebung, gemeldet, db_session):
    service, lager, drucker, archiv, sessions = umgebung
    k = await konfig.laden(db_session)
    k.melden = [*k.melden, "packbar"]
    await konfig.speichern(db_session, k)
    lager.teile = [{"id": "teil-1", "name": "Halter", "bestand": 10, "mindestbestand": 0}]
    lager.auftraege = [{"id": "o1", "kunde": "Müller", "status": "Offen"}]
    lager.positionen = [{"order_id": "o1", "part_id": "teil-1", "menge": 2}]

    await service.durchlauf()
    ((ereignis, titel, text, _),) = gemeldet
    assert ereignis == "lager_autodruck_packbar" and "Müller" in text and "2× Halter" in text
    assert service.packbar[0]["kunde"] == "Müller"
    await service.durchlauf()
    assert len(gemeldet) == 1

    # Bestand reicht nicht mehr -> vergessen; wieder genug -> neue Meldung.
    lager.teile[0]["bestand"] = 1
    await service.durchlauf()
    assert service.packbar == []
    lager.teile[0]["bestand"] = 5
    await service.durchlauf()
    assert len(gemeldet) == 2


async def test_packliste_viele_auf_einmal_als_sammelnachricht(umgebung, gemeldet, db_session):
    service, lager, drucker, archiv, sessions = umgebung
    k = await konfig.laden(db_session)
    k.melden = [*k.melden, "packbar"]
    await konfig.speichern(db_session, k)
    lager.teile = [{"id": "teil-1", "name": "Halter", "bestand": 10, "mindestbestand": 0}]
    lager.auftraege = [{"id": f"o{i}", "kunde": f"Kunde {i}", "status": "Offen"} for i in range(4)]
    lager.positionen = [{"order_id": f"o{i}", "part_id": "teil-1", "menge": 1} for i in range(4)]
    await service.durchlauf()
    ((_, titel, text, _),) = gemeldet
    assert titel == "Lager-Autodruck: 4 Bestellungen können gepackt werden"
    assert text.count("📦") == 4


async def test_packliste_fehler_haelt_durchlauf_nicht_auf(umgebung):
    service, lager, drucker, archiv, sessions = umgebung

    async def kaputt():
        raise LagerFehler("offline")

    lager.lade_packdaten = kaputt
    ergebnis = await service.durchlauf()
    assert ergebnis["ergebnis"] != "Fehler"
