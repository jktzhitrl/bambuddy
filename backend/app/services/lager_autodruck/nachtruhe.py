"""Nachtruhe: kein Druck soll fertig werden, waehrend man schlaeft.

Ein Druck darf starten, wenn sein Ende (Start + Druckdauer + Puffer) nicht in
die Nacht faellt - also vor dem Schlafengehen oder nach dem Aufstehen. Wuerde
er in der Nacht fertig, wird der Start so weit verschoben, dass er genau zur
Aufstehzeit fertig ist.

Alle Zeiten rein und raus als naive UTC-Zeiten (wie Bambuddys DateTime-
Spalten); gerechnet wird in der Ortszeit (TZ).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from backend.app.utils.local_time import local_zone


@dataclass
class Nachtruhe:
    schlafen: time
    aufstehen: time
    puffer: timedelta

    @classmethod
    def aus_text(cls, schlafen: str, aufstehen: str, puffer_minuten: int) -> Nachtruhe | None:
        s, a = uhrzeit(schlafen), uhrzeit(aufstehen)
        if s is None or a is None or s == a:
            return None
        return cls(s, a, timedelta(minutes=max(0, puffer_minuten)))


def uhrzeit(text: str | None) -> time | None:
    if not text:
        return None
    try:
        stunde, minute = text.split(":")
        return time(int(stunde), int(minute))
    except ValueError:
        return None


def _naechte(ruhe: Nachtruhe, ab: datetime, bis: datetime) -> list[tuple[datetime, datetime]]:
    """Alle Naechte (Beginn, Ende) als lokale Zeiten, die [ab, bis] beruehren koennen."""
    zone = ab.tzinfo
    tage = math.ceil((bis - ab) / timedelta(days=1)) + 2
    ergebnis = []
    for k in range(-1, tage + 1):
        tag = ab.date() + timedelta(days=k)
        beginn = datetime.combine(tag, ruhe.schlafen, tzinfo=zone)
        ende_tag = tag if ruhe.aufstehen > ruhe.schlafen else tag + timedelta(days=1)
        ende = datetime.combine(ende_tag, ruhe.aufstehen, tzinfo=zone)
        ergebnis.append((beginn, ende))
    return ergebnis


def _lokal(utc_naiv: datetime) -> datetime:
    return utc_naiv.replace(tzinfo=timezone.utc).astimezone(local_zone())


def _utc_naiv(lokal: datetime) -> datetime:
    return lokal.astimezone(timezone.utc).replace(tzinfo=None)


def fruehester_start(ruhe: Nachtruhe, jetzt: datetime, dauer: timedelta) -> datetime | None:
    """None = darf jetzt starten; sonst fruehester erlaubter Start (naive UTC)."""
    start = _lokal(jetzt)
    gesamt = dauer + ruhe.puffer
    ende = start + gesamt
    for beginn, aufstehen in _naechte(ruhe, start, ende):
        if beginn < ende < aufstehen:
            return _utc_naiv(aufstehen - gesamt)
    return None


def naechste_grenze(ruhe: Nachtruhe, jetzt: datetime, dauer: timedelta) -> datetime | None:
    """Ab wann ein jetzt erlaubter Start nicht mehr erlaubt waere (naive UTC)."""
    start = _lokal(jetzt)
    gesamt = dauer + ruhe.puffer
    ende = start + gesamt
    kommende = [beginn for beginn, _ in _naechte(ruhe, start, ende + timedelta(days=1)) if beginn >= ende]
    if not kommende:
        return None
    return _utc_naiv(min(kommende) - gesamt)
