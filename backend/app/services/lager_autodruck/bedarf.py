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

Liefertermine: hat ein Auftrag ein "Versand bis", zaehlt fuer jedes Teil der
frueheste Termin, fuer den es noch fehlt. Er hebt die Dringlichkeit an und
bestimmt die Reihenfolge in der Warteschlange.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

# Versand in hoechstens so vielen Tagen (oder schon ueberfaellig) -> "hoch",
# in hoechstens TERMIN_TAGE_MITTEL Tagen -> mindestens "mittel".
TERMIN_TAGE_HOCH = 2
TERMIN_TAGE_MITTEL = 7


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
    termin: date | None = None  # fruehestes "Versand bis" der Auftraege, die das Teil brauchen


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
    termin: date | None = None
    gesperrt: float = 0.0  # in gesperrten Lagerorten, nicht in "bestand" enthalten


def datum(wert: object) -> date | None:
    """ "2026-09-30" oder "2026-09-30T00:00:00+00:00" -> date; sonst None."""
    if not wert:
        return None
    try:
        return date.fromisoformat(str(wert)[:10])
    except ValueError:
        return None


def gesperrt_je_teil(daten: dict[str, list[dict]]) -> dict[str, float]:
    """Menge in gesperrten Lagerorten je Teil - zaehlt nicht als verfuegbar."""
    ergebnis: dict[str, float] = {}
    for zeile in daten.get("gesperrt", []):
        teil = str(zeile.get("part_id"))
        ergebnis[teil] = ergebnis.get(teil, 0.0) + max(0.0, zahl(zeile.get("menge")))
    return ergebnis


def verfuegbarer_bestand(teil: dict, gesperrt: dict[str, float]) -> float:
    """Bestand ohne gesperrte Lagerorte (nie unter 0)."""
    return max(0.0, zahl(teil.get("bestand")) - gesperrt.get(str(teil.get("id")), 0.0))


def nachfrage_je_teil(daten: dict[str, list[dict]]) -> dict[str, float]:
    """Bedarf aus offenen Auftraegen, heruntergebrochen auf druckbare Einzelteile.

    Wie das Lager beim Abschliessen eines Auftrags: zuerst werden fertige Sets
    vom Set-Bestand genommen, nur fuer den Rest werden Bestandteile gebraucht
    (auch bei Sets in Sets). Ohne das wuerde fuer Sets, die schon fertig im
    Regal liegen, trotzdem nachgedruckt.
    """
    return bedarf_je_teil(daten)[0]


def bedarf_je_teil(daten: dict[str, list[dict]]) -> tuple[dict[str, float], dict[str, date]]:
    """Wie nachfrage_je_teil, zusaetzlich der frueheste Liefertermin je Teil.

    Auftraege mit frueherem Termin bekommen fertige Sets zuerst - so wie sie
    auch zuerst verschickt werden.
    """
    komponenten_je_set: dict[str, list[tuple[str, float]]] = {}
    for k in daten.get("komponenten", []):
        komponenten_je_set.setdefault(str(k.get("part_id")), []).append(
            (str(k.get("komponente_id")), zahl(k.get("menge")))
        )
    gesperrt = gesperrt_je_teil(daten)
    set_bestand = {
        str(t.get("id")): verfuegbarer_bestand(t, gesperrt)
        for t in daten.get("teile", [])
        if str(t.get("id")) in komponenten_je_set
    }

    nachfrage: dict[str, float] = {}
    termine: dict[str, date] = {}

    def verteilen(teil: str, menge: float, pfad: tuple[str, ...], termin: date | None) -> None:
        bestandteile = komponenten_je_set.get(teil)
        if not bestandteile or teil in pfad:  # Einzelteil (oder Kreis - dann nicht weiter zerlegen)
            nachfrage[teil] = nachfrage.get(teil, 0.0) + menge
            if termin is not None and (teil not in termine or termin < termine[teil]):
                termine[teil] = termin
            return
        vom_lager = min(menge, set_bestand.get(teil, 0.0))
        set_bestand[teil] = set_bestand.get(teil, 0.0) - vom_lager
        rest = menge - vom_lager
        if rest <= 0:
            return
        for komponente, je_set in bestandteile:
            verteilen(komponente, rest * je_set, pfad + (teil,), termin)

    termin_je_auftrag = {str(a.get("id")): datum(a.get("versand_bis")) for a in daten.get("auftraege", [])}
    positionen = [
        p
        for p in daten.get("positionen", [])
        if str(p.get("order_id")) in termin_je_auftrag and p.get("part_id") and zahl(p.get("menge")) > 0
    ]
    # Frueheste Termine zuerst, ohne Termin zuletzt.
    positionen.sort(key=lambda p: termin_je_auftrag[str(p.get("order_id"))] or date.max)
    for pos in positionen:
        verteilen(str(pos.get("part_id")), zahl(pos.get("menge")), (), termin_je_auftrag[str(pos.get("order_id"))])
    return nachfrage, termine


def berechne(
    daten: dict[str, list[dict]],
    regeln: list[RegelEingabe],
    in_arbeit: dict[str, float],
) -> tuple[list[Kandidat], list[Uebersicht]]:
    teile = {str(t.get("id")): t for t in daten.get("teile", [])}
    sets = {str(k.get("part_id")) for k in daten.get("komponenten", [])}
    nachfrage, termine = bedarf_je_teil(daten)
    gesperrt = gesperrt_je_teil(daten)

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
        # Gesperrte Lagerorte zaehlen nicht: "Bestand" ist hier der verfuegbare.
        bestand = verfuegbarer_bestand(teil, gesperrt)
        mindest = zahl(teil.get("mindestbestand"))
        termin = termine.get(regel.part_id)
        eintrag = Uebersicht(
            regel.regel_id,
            regel.part_id,
            name,
            bestand,
            mindest,
            bedarf,
            arbeit,
            termin=termin,
            gesperrt=gesperrt.get(regel.part_id, 0.0),
        )
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
                termin=termin,
            )
        )
    return kandidaten, uebersicht


def termin_dringlichkeit(k: Kandidat, heute: date) -> str | None:
    """Mindest-Dringlichkeit aus dem Liefertermin.

    Nur wenn Bestand und laufende Drucke die offenen Auftraege nicht decken -
    sonst liegt die Ware schon da und der Termin ist kein Grund zur Eile.
    """
    if k.termin is None or k.bestand + k.in_arbeit >= k.nachfrage:
        return None
    tage = (k.termin - heute).days
    if tage <= TERMIN_TAGE_HOCH:
        return "hoch"
    if tage <= TERMIN_TAGE_MITTEL:
        return "mittel"
    return None


def mindestens(dringlichkeit: str, untergrenze: str | None) -> str:
    """Die dringendere von beiden (hoch > mittel > niedrig)."""
    rang = {"hoch": 0, "mittel": 1, "niedrig": 2}
    if untergrenze is None or rang.get(dringlichkeit, 3) <= rang[untergrenze]:
        return dringlichkeit
    return untergrenze


def regel_dringlichkeit(k: Kandidat, heute: date | None = None) -> str:
    """Rueckfall, wenn keine KI eingerichtet ist oder sie nicht antwortet."""
    if k.bestand <= 0 and k.nachfrage > 0:
        stufe = "hoch"
    elif k.verfuegbar <= k.mindestbestand / 2:
        stufe = "mittel"
    else:
        stufe = "niedrig"
    return mindestens(stufe, termin_dringlichkeit(k, heute) if heute else None)


def regel_begruendung(k: Kandidat) -> str:
    text = f"Bestand {k.bestand:g}, Mindestbestand {k.mindestbestand:g}"
    if k.in_arbeit:
        text += f", {k.in_arbeit:g} schon in Arbeit"
    if k.nachfrage:
        text += f", {k.nachfrage:g} Stueck fuer offene Auftraege benoetigt"
        if k.termin:
            text += f" (Versand bis {k.termin:%d.%m.})"
    return text + "."
