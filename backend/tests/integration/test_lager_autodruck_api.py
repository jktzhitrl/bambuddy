"""API-Tests fuer das Fork-Modul Lager-Autodruck."""

from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.lager_autodruck import JOB_GEPLANT, JOB_VERWORFEN, JOB_WARTET, LagerDruckJob
from backend.app.models.print_queue import PrintQueueItem

BASIS = "/api/v1/lager-autodruck"


async def test_konfig_verraet_keine_geheimnisse(async_client):
    antwort = await async_client.put(
        f"{BASIS}/konfig",
        json={
            "aktiv": True,
            "supabase_url": "https://lager.example/",
            "supabase_anon_key": "anon",
            "email": "drucker@x",
            "passwort": "geheim",
            "anthropic_api_key": "sk-test",
        },
    )
    assert antwort.status_code == 200
    daten = (await async_client.get(f"{BASIS}/konfig")).json()
    assert daten["passwort"] == "********" and daten["anthropic_api_key"] == "********"
    assert daten["supabase_url"] == "https://lager.example"
    assert daten["nachtruhe_aktiv"] is True and daten["schlafen"] == "22:00"
    assert daten["eingerichtet"] is True

    # Platzhalter zuruecksenden laesst das Passwort unveraendert.
    daten["email"] = "neu@x"
    await async_client.put(f"{BASIS}/konfig", json=daten)
    from backend.app.core.database import async_session
    from backend.app.services.lager_autodruck import konfig

    async with async_session() as db:
        k = await konfig.laden(db)
    assert k.passwort == "geheim" and k.email == "neu@x"


async def _archiv(db_session):
    archiv = PrintArchive(
        filename="Halter.gcode.3mf", file_path="a/h.3mf", file_size=1, print_name="Halter", sliced_for_model="P1S"
    )
    db_session.add(archiv)
    await db_session.commit()
    return archiv


async def test_regel_anlegen_aendern_loeschen(async_client, db_session, printer_factory):
    drucker = await printer_factory(name="P1S", model="P1S")
    archiv = await _archiv(db_session)

    antwort = await async_client.post(
        f"{BASIS}/regeln",
        json={
            "part_id": "teil-1",
            "part_name": "Halter",
            "archive_id": archiv.id,
            "stueck_je_druck": 4,
            "modus": "freigabe",
            "printer_id": drucker.id,
        },
    )
    assert antwort.status_code == 200, antwort.text
    regel = antwort.json()
    assert regel["dateiname"] == "Halter"  # aus dem Archiv uebernommen
    assert regel["warnung"]  # Lager noch nicht verbunden

    doppelt = await async_client.post(f"{BASIS}/regeln", json={"part_id": "teil-1", "archive_id": archiv.id})
    assert doppelt.status_code == 409

    regel["modus"] = "unsinn"
    assert (await async_client.put(f"{BASIS}/regeln/{regel['id']}", json=regel)).status_code == 422
    regel["modus"] = "automatisch"
    regel["printer_id"] = None
    regel["target_model"] = "P1S"
    geaendert = (await async_client.put(f"{BASIS}/regeln/{regel['id']}", json=regel)).json()
    assert geaendert["modus"] == "automatisch" and geaendert["target_model"] == "P1S"

    assert len((await async_client.get(f"{BASIS}/regeln")).json()) == 1
    assert (await async_client.delete(f"{BASIS}/regeln/{regel['id']}")).status_code == 200
    assert (await async_client.get(f"{BASIS}/regeln")).json() == []


async def test_freigeben_und_verwerfen(async_client, db_session, printer_factory):
    drucker = await printer_factory(name="P1S", model="P1S")
    archiv = await _archiv(db_session)
    items = []
    for _ in range(2):
        item = PrintQueueItem(printer_id=drucker.id, archive_id=archiv.id, manual_start=True, status="pending")
        db_session.add(item)
        items.append(item)
    await db_session.commit()
    for item in items:
        db_session.add(LagerDruckJob(part_id="teil-1", queue_item_id=item.id, stueck=4, status=JOB_WARTET))
    await db_session.commit()
    jobs = (await db_session.execute(select(LagerDruckJob).order_by(LagerDruckJob.id))).scalars().all()

    assert (await async_client.post(f"{BASIS}/jobs/{jobs[0].id}/freigeben")).status_code == 200
    assert (await async_client.post(f"{BASIS}/jobs/{jobs[0].id}/freigeben")).status_code == 409
    assert (await async_client.post(f"{BASIS}/jobs/{jobs[1].id}/verwerfen")).status_code == 200

    liste = {j["id"]: j for j in (await async_client.get(f"{BASIS}/jobs")).json()}
    assert liste[jobs[0].id]["status"] == JOB_GEPLANT
    assert liste[jobs[1].id]["status"] == JOB_VERWORFEN
    db_session.expire_all()
    verbleibend = (await db_session.execute(select(PrintQueueItem))).scalars().all()
    assert [(i.id, i.manual_start) for i in verbleibend] == [(items[0].id, False)]


async def test_status_ohne_einrichtung(async_client):
    daten = (await async_client.get(f"{BASIS}/status")).json()
    assert daten["eingerichtet"] is False and daten["offene_buchungen"] == 0
    assert (await async_client.post(f"{BASIS}/pruefen")).json()["ergebnis"] == "nicht eingerichtet"


async def test_konfig_lehnt_gefaehrliche_adresse_und_falsche_uhrzeit_ab(async_client):
    antwort = await async_client.put(f"{BASIS}/konfig", json={"supabase_url": "http://169.254.169.254/"})
    assert antwort.status_code == 400
    antwort = await async_client.put(f"{BASIS}/konfig", json={"schlafen": "25:00"})
    assert antwort.status_code == 400


async def test_benachrichtigung_kanaele_und_test(async_client, db_session):
    from unittest.mock import AsyncMock, patch

    from backend.app.models.notification import NotificationProvider

    kanal = NotificationProvider(name="Handy", provider_type="ntfy", config="{}", enabled=True)
    db_session.add(kanal)
    await db_session.commit()

    daten = (await async_client.get(f"{BASIS}/benachrichtigung/kanaele")).json()
    assert daten["kanaele"] == [{"id": kanal.id, "name": "Handy", "typ": "ntfy", "aktiv": True}]
    assert {e["id"] for e in daten["ereignisse"]} >= {"freigabe", "lager_offline"}

    assert (await async_client.post(f"{BASIS}/benachrichtigung/testen")).json()["ok"] is False
    assert (await async_client.put(f"{BASIS}/konfig", json={"melden": ["quatsch"]})).status_code == 422
    assert (await async_client.put(f"{BASIS}/konfig", json={"melden_an": [kanal.id]})).status_code == 200
    with patch(
        "backend.app.services.notification_service.notification_service._send_to_providers", AsyncMock()
    ) as gesendet:
        antwort = (await async_client.post(f"{BASIS}/benachrichtigung/testen")).json()
    assert antwort["ok"] is True and gesendet.await_count == 1


async def test_regel_mit_datei_aus_dem_dateimanager(async_client, db_session):
    from backend.app.models.library import LibraryFile

    gesliced = LibraryFile(
        filename="Arme.gcode.3mf",
        file_path="l/a.3mf",
        file_type="3mf",
        file_size=1,
        file_metadata={"sliced_for_model": "P1S", "print_time_seconds": 3600, "filament_used_grams": 220},
    )
    projekt = LibraryFile(filename="Arme-Projekt.3mf", file_path="l/p.3mf", file_type="3mf", file_size=1)
    db_session.add_all([gesliced, projekt])
    await db_session.commit()

    # Nur geslicte Dateien stehen zur Auswahl.
    liste = (await async_client.get(f"{BASIS}/dateien")).json()
    assert [d["id"] for d in liste] == [gesliced.id]
    assert liste[0]["name"] == "Arme" and liste[0]["modell"] == "P1S"

    antwort = await async_client.post(f"{BASIS}/regeln", json={"part_id": "arm", "library_file_id": gesliced.id})
    assert antwort.status_code == 200, antwort.text
    assert antwort.json()["dateiname"] == "Arme" and antwort.json()["library_file_id"] == gesliced.id

    ungesliced = await async_client.post(f"{BASIS}/regeln", json={"part_id": "b", "library_file_id": projekt.id})
    assert ungesliced.status_code == 400

    archiv = await _archiv(db_session)
    beides = await async_client.post(
        f"{BASIS}/regeln", json={"part_id": "c", "library_file_id": gesliced.id, "archive_id": archiv.id}
    )
    assert beides.status_code == 400
