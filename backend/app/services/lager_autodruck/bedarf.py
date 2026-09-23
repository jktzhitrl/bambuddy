"""Bedarfsrechnung: welches Teil muss wie oft nachgedruckt werden?

Reine Funktionen ohne Datenbank oder Netz, damit sie leicht zu testen sind.
Entspricht der frueheren Logik aus api/_lib/auto-druck-kern.js im Lager, mit
zwei Aenderungen:

- Drucke, die schon in der Warteschlange stehen oder laufen ("in Arbeit"),
  zaehlen als Bestand. Dadurch wird nichts doppelt gedruckt, und ein zweiter
  Auftrag fuer dasselbe Teil ist moeglich, wenn der erste nicht reicht.
- Offene Kundenauftraege verbrauchen Bestand: verfuegbar ist
  bestand + in_arbeit - bedarf_aus_auftraegen.

Nachgefuellt wird wie bisher auf den doppelten Mindestbestand.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def zahl(wert: object) -> float:
    if wert is None or wert == "":
        return 0.0
    try:
        n = float(wert)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return n if math.isfinite(n) else 0.0


@dataclass
class RegelEingabe:
    regel_id: int
    part_id: str
    stueck_je_druck: int
    max_drucke_pro_tag: int | None = None
    heute_angelegt: int = 0  # Druckauftraege, die heute schon fuer diese Regel entstanden


@dataclass
class Kandidat:
    regel_id: int
    part_id: str
    name: str
    kategorie: str | None
    bestand: float
    mindestbestand: float
    nachfrage: float
    in_arbeit: float
    verfuegbar: float
    druecke: int
    stueck: int  # druecke * stueck_je_druck
    gekappt: bool = False  # Tageslimit hat druecke verringert


@dataclass
class Uebersicht:
    """Stand je Regel - fuer die Anzeige, auch wenn nichts zu tun ist."""

    regel_id: int
    part_id: str
    name: str | None
    bestand: float | None
    mindestbestand: float | None
    nachfrage: float
    in_arbeit: float
    hinweis: str | None = None


def nachfrage_je_teil(daten: dict[str, list[dict]]) -> dict[str, float]:
    """Bedarf aus offenen Auftraegen, heruntergebrochen auf druckbare Einzelteile.

    Wie das Lager beim Abschliessen eines Auftrags: zuerst werden fertige Sets
    vom Set-Bestand genommen, nur fuer den Rest werden Bestandteile gebraucht
    (auch bei Sets in Sets). Ohne das wuerde fuer Sets, die schon fertig im
    Regal liegen, trotzdem nachgedruckt.
    """
    komponenten_je_set: dict[str, list[tuple[str, float]]] = {}
    for k in daten.get("komponenten", []):
        komponenten_je_set.setdefault(str(k.get("part_id")), []).append(
            (str(k.get("komponente_id")), zahl(k.get("menge")))
        )
    set_bestand = {
        str(t.get("id")): max(0.0, zahl(t.get("bestand")))
        for t in daten.get("teile", [])
        if str(t.get("id")) in komponenten_je_set
    }

    nachfrage: dict[str, float] = {}

    def verteilen(teil: str, menge: float, pfad: tuple[str, ...]) -> None:
        bestandteile = komponenten_je_set.get(teil)
        if not bestandteile or teil in pfad:  # Einzelteil (oder Kreis - dann nicht weiter zerlegen)
            nachfrage[teil] = nachfrage.get(teil, 0.0) + menge
            return
        vom_lager = min(menge, set_bestand.get(teil, 0.0))
        set_bestand[teil] = set_bestand.get(teil, 0.0) - vom_lager
        rest = menge - vom_lager
        if rest <= 0:
            return
        for komponente, je_set in bestandteile:
            verteilen(komponente, rest * je_set, pfad + (teil,))

    offene = {str(a.get("id")) for a in daten.get("auftraege", [])}
    for pos in daten.get("positionen", []):
        if str(pos.get("order_id")) not in offene or not pos.get("part_id"):
            continue
        menge = zahl(pos.get("menge"))
        if menge > 0:
            verteilen(str(pos.get("part_id")), menge, ())
    return nachfrage


def berechne(
    daten: dict[str, list[dict]],
    regeln: list[RegelEingabe],
    in_arbeit: dict[str, float],
) -> tuple[list[Kandidat], list[Uebersicht]]:
    teile = {str(t.get("id")): t for t in daten.get("teile", [])}
    sets = {str(k.get("part_id")) for k in daten.get("komponenten", [])}
    nachfrage = nachfrage_je_teil(daten)

    kandidaten: list[Kandidat] = []
    uebersicht: list[Uebersicht] = []

    for regel in regeln:
        teil = teile.get(regel.part_id)
        arbeit = in_arbeit.get(regel.part_id, 0.0)
        bedarf = nachfrage.get(regel.part_id, 0.0)
        if teil is None:
            uebersicht.append(
                Uebersicht(
                    regel.regel_id, regel.part_id, None, None, None, bedarf, arbeit, "Teil im Lager nicht gefunden"
                )
            )
            continue

        name = str(teil.get("name") or regel.part_id)
        bestand = zahl(teil.get("bestand"))
        mindest = zahl(teil.get("mindestbestand"))
        eintrag = Uebersicht(regel.regel_id, regel.part_id, name, bestand, mindest, bedarf, arbeit)
        uebersicht.append(eintrag)

        if regel.part_id in sets:
            eintrag.hinweis = "Ist ein Set - Regeln gehoeren an die Einzelteile"
            continue

        verfuegbar = bestand + arbeit - bedarf
        if verfuegbar > mindest or (mindest <= 0 and verfuegbar >= 0):
            continue

        fehlend = 2 * mindest - verfuegbar
        je_druck = max(1, int(regel.stueck_je_druck or 1))
        druecke = max(1, math.ceil(fehlend / je_druck))

        gekappt = False
        if regel.max_drucke_pro_tag is not None and regel.max_drucke_pro_tag >= 0:
            rest = regel.max_drucke_pro_tag - regel.heute_angelegt
            if rest <= 0:
                eintrag.hinweis = "Tageslimit erreicht"
                continue
            if druecke > rest:
                druecke = rest
                gekappt = True

        kandidaten.append(
            Kandidat(
                regel_id=regel.regel_id,
                part_id=regel.part_id,
                name=name,
                kategorie=teil.get("kategorie"),
                bestand=bestand,
                mindestbestand=mindest,
                nachfrage=bedarf,
                in_arbeit=arbeit,
                verfuegbar=verfuegbar,
                druecke=druecke,
                stueck=druecke * je_druck,
                gekappt=gekappt,
            )
        )
    return kandidaten, uebersicht


def regel_dringlichkeit(k: Kandidat) -> str:
    """Rueckfall, wenn keine KI eingerichtet ist oder sie nicht antwortet."""
    if k.bestand <= 0 and k.nachfrage > 0:
        return "hoch"
    if k.verfuegbar <= k.mindestbestand / 2:
        return "mittel"
    return "niedrig"


def regel_begruendung(k: Kandidat) -> str:
    text = f"Bestand {k.bestand:g}, Mindestbestand {k.mindestbestand:g}"
    if k.in_arbeit:
        text += f", {k.in_arbeit:g} schon in Arbeit"
    if k.nachfrage:
        text += f", {k.nachfrage:g} Stueck fuer offene Auftraege benoetigt"
    return text + "."
