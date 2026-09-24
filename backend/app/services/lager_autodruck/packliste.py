"""Welche offenen Bestellungen koennen jetzt komplett gepackt werden?

Reine Funktionen ohne Datenbank oder Netz. Gerechnet wird wie im Lager beim
Abschliessen eines Auftrags (planeAbbuchung): zuerst fertige Teile/Sets aus dem
Bestand, fehlende Sets aus ihren direkten Bestandteilen, dazu die Kameras
(Systemanzahl x 3).

Der Bestand wird der Reihe nach verteilt: erst Auftraege mit Status "Fertig"
(schon gepackt, aber noch nicht abgebucht), dann nach Versandtermin, ohne
Termin zuletzt. Ein Auftrag, der nicht komplett aufgeht, haelt nichts fest -
sonst wuerde ein grosser Auftrag alle kleinen blockieren.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from backend.app.services.lager_autodruck.bedarf import datum, zahl

KAMERAS_PRO_SYSTEM = 3
OFFEN = ("Offen", "In Arbeit")
GEPACKT = "Fertig"


@dataclass
class Position:
    name: str
    menge: float
    orte: list[str] = field(default_factory=list)
    # Aus Einzelteilen zusammenzubauen: [(Name, Menge, Orte)]
    zusammenbauen: list[tuple[str, float, list[str]]] = field(default_factory=list)
    aus_lager: float = 0  # davon fertig im Regal


@dataclass
class Packliste:
    order_id: str
    kunde: str
    versand_bis: date | None
    positionen: list[Position]
    kameras: tuple[str, float] | None = None
    sonderposten: list[str] = field(default_factory=list)


def _orte(eintraege: list[dict], fallback: str | None) -> list[str]:
    """Lagerorte mit Bestand, z.B. ["Regal B3 (12)"]; sonst der alte Einzel-Lagerort."""
    orte = [
        f"{e.get('ort')} ({zahl(e.get('menge')):g})" for e in eintraege if e.get("ort") and zahl(e.get("menge")) > 0
    ]
    if not orte and fallback:
        orte = [str(fallback)]
    return orte


def packbare_auftraege(daten: dict[str, list[dict]]) -> list[Packliste]:
    teile = {str(t.get("id")): t for t in daten.get("teile", [])}
    bestand = {tid: max(0.0, zahl(t.get("bestand"))) for tid, t in teile.items()}
    kameras = {str(k.get("typ")): max(0.0, zahl(k.get("bestand"))) for k in daten.get("kameras", [])}
    komponenten: dict[str, list[tuple[str, float]]] = {}
    for k in daten.get("komponenten", []):
        komponenten.setdefault(str(k.get("part_id")), []).append((str(k.get("komponente_id")), zahl(k.get("menge"))))
    orte_je_teil: dict[str, list[dict]] = {}
    for ort in daten.get("lagerorte", []):
        orte_je_teil.setdefault(str(ort.get("part_id")), []).append(ort)
    positionen_je_auftrag: dict[str, list[dict]] = {}
    for p in daten.get("positionen", []):
        positionen_je_auftrag.setdefault(str(p.get("order_id")), []).append(p)
    sonder_je_auftrag: dict[str, list[dict]] = {}
    for s in daten.get("sonderposten", []):
        sonder_je_auftrag.setdefault(str(s.get("order_id")), []).append(s)

    def name(tid: str) -> str:
        return str((teile.get(tid) or {}).get("name") or "unbekanntes Teil")

    def orte(tid: str) -> list[str]:
        return _orte(orte_je_teil.get(tid, []), (teile.get(tid) or {}).get("lagerort"))

    def planen(auftrag: dict) -> tuple[dict[str, float], float, list[Position]] | None:
        """Abzug je Teil, Kameras und Packliste - None, wenn etwas fehlt."""
        abzug: dict[str, float] = {}

        def noch_da(tid: str) -> float:
            return bestand.get(tid, 0.0) - abzug.get(tid, 0.0)

        liste: list[Position] = []
        for pos in positionen_je_auftrag.get(str(auftrag.get("id")), []):
            tid = str(pos.get("part_id") or "")
            menge = zahl(pos.get("menge"))
            if not tid or menge <= 0:
                continue
            if tid not in teile:
                return None
            eintrag = Position(name(tid), menge, orte(tid))
            aus_lager = min(menge, max(0.0, noch_da(tid)))
            if aus_lager > 0:
                abzug[tid] = abzug.get(tid, 0.0) + aus_lager
            eintrag.aus_lager = aus_lager
            offen = menge - aus_lager
            if offen > 0:
                if tid not in komponenten:
                    return None
                for kid, je_set in komponenten[tid]:
                    gebraucht = je_set * offen
                    if gebraucht <= 0:
                        continue
                    if noch_da(kid) < gebraucht:
                        return None
                    abzug[kid] = abzug.get(kid, 0.0) + gebraucht
                    eintrag.zusammenbauen.append((name(kid), gebraucht, orte(kid)))
            liste.append(eintrag)

        kamera_menge = 0.0
        if auftrag.get("kamera"):
            systeme = auftrag.get("system_anzahl")
            if systeme is None or systeme == "":
                systeme = sum(zahl(p.get("menge")) for p in positionen_je_auftrag.get(str(auftrag.get("id")), [])) or 1
            kamera_menge = zahl(systeme) * KAMERAS_PRO_SYSTEM
            if kameras.get(str(auftrag.get("kamera")), 0.0) < kamera_menge:
                return None
        if not liste and not kamera_menge:
            return None  # nichts aus dem Lager (nur Sonderposten o.ae.)
        return abzug, kamera_menge, liste

    def abziehen(auftrag: dict, abzug: dict[str, float], kamera_menge: float) -> None:
        for tid, menge in abzug.items():
            bestand[tid] = bestand.get(tid, 0.0) - menge
        if kamera_menge:
            typ = str(auftrag.get("kamera"))
            kameras[typ] = kameras.get(typ, 0.0) - kamera_menge

    auftraege = [a for a in daten.get("auftraege", []) if a.get("status") in (*OFFEN, GEPACKT)]
    auftraege.sort(
        key=lambda a: (
            a.get("status") != GEPACKT,
            datum(a.get("versand_bis")) or date.max,
            datum(a.get("bestelldatum")) or date.max,
            str(a.get("id")),
        )
    )

    ergebnis: list[Packliste] = []
    for auftrag in auftraege:
        plan = planen(auftrag)
        if auftrag.get("status") == GEPACKT:
            # Liegt schon gepackt da - der Bestand ist also physisch weg.
            if plan:
                abziehen(auftrag, plan[0], plan[1])
            continue
        if plan is None:
            continue
        abzug, kamera_menge, liste = plan
        abziehen(auftrag, abzug, kamera_menge)
        ergebnis.append(
            Packliste(
                order_id=str(auftrag.get("id")),
                kunde=str(auftrag.get("kunde") or "Ohne Namen"),
                versand_bis=datum(auftrag.get("versand_bis")),
                positionen=liste,
                kameras=(str(auftrag.get("kamera")), kamera_menge) if kamera_menge else None,
                sonderposten=[
                    f"{zahl(s.get('menge')):g}× {s.get('beschreibung') or 'Sonderposten'}"
                    for s in sonder_je_auftrag.get(str(auftrag.get("id")), [])
                ],
            )
        )
    return ergebnis


def als_text(p: Packliste) -> str:
    """Packliste fuer eine Benachrichtigung."""
    kopf = f"📦 {p.kunde}"
    if p.versand_bis:
        kopf += f" – Versand bis {p.versand_bis:%d.%m.}"
    zeilen = [kopf]
    for pos in p.positionen:
        wo = f" – {', '.join(pos.orte)}" if pos.orte and not pos.zusammenbauen else ""
        zeilen.append(f"• {pos.menge:g}× {pos.name}{wo}")
        if pos.zusammenbauen:
            fertig = f"{pos.aus_lager:g} fertig im Lager, " if pos.aus_lager else ""
            zeilen.append(f"   {fertig}{pos.menge - pos.aus_lager:g} zusammenbauen aus:")
            for teil, menge, orte in pos.zusammenbauen:
                wo = f" – {', '.join(orte)}" if orte else ""
                zeilen.append(f"   – {menge:g}× {teil}{wo}")
    if p.kameras:
        zeilen.append(f"• {p.kameras[1]:g}× Kamera {p.kameras[0]}")
    for s in p.sonderposten:
        zeilen.append(f"• {s} (Sonderposten, nicht im Lager geprüft)")
    return "\n".join(zeilen)
