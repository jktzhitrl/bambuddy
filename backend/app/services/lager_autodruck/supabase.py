"""Schlanker Supabase-Zugang fuer das Lager (PostgREST + Auth per httpx).

Meldet sich mit einem normalen Lager-Konto an (z.B. drucker@..., Rolle
editor) - kein service_role-Schluessel. Damit gelten die RLS-Regeln des
Lagers unveraendert, und in der Historie steht bei jeder Buchung, von wem sie
kam. Genau so hat es vorher api/druck-fertig.js im Lager gemacht.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from backend.app.api.routes._url_safety import assert_safe_lan_service_url

logger = logging.getLogger(__name__)


class LagerFehler(Exception):
    """Lager nicht erreichbar, Anmeldung falsch oder Anfrage abgelehnt."""


class LagerClient:
    def __init__(self, url: str, anon_key: str, email: str, passwort: str, *, timeout: float = 20.0):
        self._basis = url.rstrip("/")
        # Gleiche URL-Pruefung wie Bambuddys andere LAN-Dienste (kein file://,
        # keine Cloud-Metadaten-Adressen ...). Fehler erst beim Zugriff melden.
        try:
            assert_safe_lan_service_url(self._basis, label="Supabase-Adresse")
            self._url_fehler: str | None = None
        except ValueError as e:
            self._url_fehler = str(e)
        self._anon_key = anon_key
        self._email = email
        self._passwort = passwort
        self._timeout = timeout
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._laeuft_ab: float = 0.0
        # Tests reichen hier einen httpx.MockTransport herein.
        self.transport: httpx.AsyncBaseTransport | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self.transport)

    async def _anmelden(self) -> None:
        if self._url_fehler:
            raise LagerFehler(self._url_fehler)
        # Gueltiges Token noch mindestens eine Minute? Dann nichts tun.
        if self._access_token and time.monotonic() < self._laeuft_ab - 60:
            return
        async with self._client() as client:
            antwort = None
            if self._refresh_token:
                antwort = await client.post(
                    f"{self._basis}/auth/v1/token",
                    params={"grant_type": "refresh_token"},
                    headers={"apikey": self._anon_key},
                    json={"refresh_token": self._refresh_token},
                )
                if antwort.status_code != 200:
                    antwort = None
            if antwort is None:
                antwort = await client.post(
                    f"{self._basis}/auth/v1/token",
                    params={"grant_type": "password"},
                    headers={"apikey": self._anon_key},
                    json={"email": self._email, "password": self._passwort},
                )
        if antwort.status_code != 200:
            self._access_token = None
            self._refresh_token = None
            raise LagerFehler(f"Anmeldung am Lager fehlgeschlagen ({antwort.status_code}): {_kurz(antwort)}")
        daten = antwort.json()
        self._access_token = daten.get("access_token")
        self._refresh_token = daten.get("refresh_token")
        self._laeuft_ab = time.monotonic() + float(daten.get("expires_in") or 3600)
        if not self._access_token:
            raise LagerFehler("Anmeldung am Lager lieferte kein Zugangs-Token.")

    async def _anfrage(self, methode: str, pfad: str, **kwargs: Any) -> Any:
        await self._anmelden()
        headers = {
            "apikey": self._anon_key,
            "Authorization": f"Bearer {self._access_token}",
            **kwargs.pop("headers", {}),
        }
        try:
            async with self._client() as client:
                antwort = await client.request(methode, f"{self._basis}{pfad}", headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise LagerFehler(f"Lager nicht erreichbar: {e}") from e
        if antwort.status_code == 401:
            # Token serverseitig verworfen - beim naechsten Mal neu anmelden.
            self._access_token = None
        if antwort.status_code >= 400:
            raise LagerFehler(f"Lager antwortete {antwort.status_code} auf {pfad}: {_kurz(antwort)}")
        if not antwort.content:
            return None
        return antwort.json()

    async def lesen(self, tabelle: str, params: dict[str, str]) -> list[dict]:
        daten = await self._anfrage("GET", f"/rest/v1/{tabelle}", params=params)
        return list(daten or [])

    async def rpc(self, funktion: str, argumente: dict[str, Any]) -> Any:
        return await self._anfrage("POST", f"/rest/v1/rpc/{funktion}", json=argumente)

    async def upsert(self, tabelle: str, zeile: dict[str, Any], on_conflict: str) -> None:
        await self._anfrage(
            "POST",
            f"/rest/v1/{tabelle}",
            params={"on_conflict": on_conflict},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=zeile,
        )

    # --- fachliche Abfragen -------------------------------------------------

    async def lade_bestandsdaten(self) -> dict[str, list[dict]]:
        """Alles, was fuer die Bedarfsrechnung gebraucht wird."""
        teile = await self.lesen(
            "parts",
            {
                "select": "id,name,kategorie,bestand,mindestbestand,material_id,material_verbrauch",
                "geloescht_am": "is.null",
            },
        )
        komponenten = await self.lesen("part_components", {"select": "part_id,komponente_id,menge"})
        auftraege = await self.lesen(
            "orders",
            {"select": "id,status,versand_bis", "geloescht_am": "is.null", "status": "neq.Abgeschlossen"},
        )
        positionen = await self.lesen("order_items", {"select": "order_id,part_id,menge"})
        return {
            "teile": teile,
            "komponenten": komponenten,
            "auftraege": auftraege,
            "positionen": positionen,
        }

    async def lade_packdaten(self) -> dict[str, list[dict]]:
        """Alles fuer die Packliste (Bestellungen, die komplett gepackt werden koennen)."""
        teile = await self.lesen("parts", {"select": "id,name,bestand,lagerort", "geloescht_am": "is.null"})
        komponenten = await self.lesen("part_components", {"select": "part_id,komponente_id,menge"})
        lagerorte = await self.lesen("part_lagerorte", {"select": "part_id,ort,menge"})
        kameras = await self.lesen("cameras", {"select": "typ,bestand", "geloescht_am": "is.null"})
        auftraege = await self.lesen(
            "orders",
            {
                "select": "id,kunde,status,versand_bis,bestelldatum,kamera,system_anzahl",
                "geloescht_am": "is.null",
                "status": 'in.("Offen","In Arbeit","Fertig")',
            },
        )
        positionen = await self.lesen("order_items", {"select": "order_id,part_id,menge", "order": "position"})
        sonderposten = await self.lesen("order_sonderposten", {"select": "order_id,beschreibung,menge"})
        return {
            "teile": teile,
            "komponenten": komponenten,
            "lagerorte": lagerorte,
            "kameras": kameras,
            "auftraege": auftraege,
            "positionen": positionen,
            "sonderposten": sonderposten,
        }

    async def lade_teile(self) -> list[dict]:
        return await self.lesen(
            "parts",
            {"select": "id,name,bestand,mindestbestand", "geloescht_am": "is.null", "order": "name"},
        )

    async def druck_verbuchen(
        self, *, drucker: str, dateiname: str, zeitstempel: str, gramm: float | None, status: str
    ) -> str:
        """Ruft druck_verbuchen im Lager auf (dieselbe Funktion wie der alte Webhook).

        Das Lager sucht ueber druck_zuordnung.dateiname das Teil, bucht Stueck
        und Material und erkennt Wiederholungen an drucker+dateiname+zeitstempel.
        """
        ergebnis = await self.rpc(
            "druck_verbuchen",
            {
                "p_drucker": drucker,
                "p_datei": dateiname,
                "p_zeitstempel": zeitstempel,
                "p_gramm": gramm,
                "p_status": status,
            },
        )
        return str(ergebnis) if ergebnis is not None else ""

    async def zuordnung_speichern(
        self,
        *,
        dateiname: str,
        part_id: str,
        stueck_je_druck: int,
        archive_id: int | None,
        printer_id: int | None,
    ) -> None:
        """Haelt druck_zuordnung im Lager passend zur Regel, damit Buchungen ankommen."""
        await self.upsert(
            "druck_zuordnung",
            {
                "dateiname": dateiname,
                "part_id": part_id,
                "stueck_je_druck": max(1, int(stueck_je_druck or 1)),
                "aktiv": True,
                "bambuddy_archive_id": archive_id,
                "bambuddy_printer_id": printer_id,
            },
            on_conflict="dateiname",
        )


def _kurz(antwort: httpx.Response) -> str:
    try:
        text = antwort.text
    except Exception:  # noqa: BLE001
        return ""
    return text[:300]
