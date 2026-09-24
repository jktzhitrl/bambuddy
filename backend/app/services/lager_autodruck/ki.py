"""KI-Einschaetzung der Dringlichkeit (Claude).

Bewertet jeden Nachdruck-Kandidaten mit niedrig/mittel/hoch und einer kurzen
Begruendung. Die Dringlichkeit steuert zwei Dinge:
- Reihenfolge: "hoch" kommt in der Warteschlange nach vorn.
- Regeln im Modus "ki": nur "niedrig" druckt ohne Freigabe.

Faellt die KI aus (kein Schluessel, Netz, Fehler), gilt die feste Regel aus
bedarf.regel_dringlichkeit - der Autodruck laeuft also immer weiter.
"""

from __future__ import annotations

import json
import logging

import anthropic

from backend.app.services.lager_autodruck.bedarf import Kandidat

logger = logging.getLogger(__name__)

STANDARD_MODELL = "claude-haiku-4-5"
DRINGLICHKEITEN = ("niedrig", "mittel", "hoch")

_SYSTEM = (
    "Du unterstuetzt die Lagerhaltung einer kleinen 3D-Druck-Werkstatt. "
    "Fuer jede aufgefuehrte Baugruppe schaetzt du ein, wie dringend ein Nachdruck ist, "
    "und begruendest das in ein bis zwei Saetzen auf Deutsch, konkret und knapp "
    "(Bestand gegenueber Mindestbestand, schon laufende Drucke, offene Auftraege, Kategorie, "
    "fruehester Versandtermin der Auftraege). "
    "Die Stueckzahl steht bereits fest, die aenderst du nicht. "
    "Je nach Einstellung startet 'niedrig' einen Druck ohne menschliche Freigabe - "
    "vergib das nur, wenn es wirklich unkritisch ist. 'hoch' wird in der Warteschlange vorgezogen."
)

_WERKZEUG = {
    "name": "einschaetzungen_abgeben",
    "description": "Traegt fuer jede aufgefuehrte Baugruppe eine Dringlichkeit und eine kurze Begruendung ein.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "einschaetzungen": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "part_id": {"type": "string"},
                        "dringlichkeit": {"type": "string", "enum": list(DRINGLICHKEITEN)},
                        "begruendung": {"type": "string"},
                    },
                    "required": ["part_id", "dringlichkeit", "begruendung"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["einschaetzungen"],
        "additionalProperties": False,
    },
}


async def einschaetzen(
    kandidaten: list[Kandidat], *, api_key: str | None, modell: str | None = None
) -> dict[str, tuple[str, str]]:
    """Liefert part_id -> (dringlichkeit, begruendung); leer, wenn die KI nicht verfuegbar ist."""
    if not api_key or not kandidaten:
        return {}

    kontext = [
        {
            "part_id": k.part_id,
            "name": k.name,
            "kategorie": k.kategorie,
            "bestand": k.bestand,
            "mindestbestand": k.mindestbestand,
            "schon_in_arbeit": k.in_arbeit,
            "bedarf_aus_offenen_auftraegen": k.nachfrage,
            "fruehester_versand": k.termin.isoformat() if k.termin else None,
            "geplante_stueckzahl": k.stueck,
        }
        for k in kandidaten
    ]

    try:
        async with anthropic.AsyncAnthropic(api_key=api_key, timeout=60.0, max_retries=2) as client:
            antwort = await client.messages.create(
                model=modell or STANDARD_MODELL,
                max_tokens=4096,
                system=_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(kontext, ensure_ascii=False)}],
                tools=[_WERKZEUG],
                tool_choice={"type": "tool", "name": _WERKZEUG["name"]},
            )
    except anthropic.AuthenticationError:
        logger.error("Lager-Autodruck: Anthropic-API-Schluessel ungueltig - nutze feste Regeln")
        return {}
    except anthropic.RateLimitError:
        logger.warning("Lager-Autodruck: KI-Ratenlimit erreicht - nutze feste Regeln")
        return {}
    except anthropic.APIStatusError as e:
        logger.warning("Lager-Autodruck: KI antwortete %s - nutze feste Regeln", e.status_code)
        return {}
    except anthropic.APIConnectionError as e:
        logger.warning("Lager-Autodruck: KI nicht erreichbar (%s) - nutze feste Regeln", e)
        return {}

    ergebnis: dict[str, tuple[str, str]] = {}
    for block in antwort.content:
        if block.type != "tool_use":
            continue
        for e in (block.input or {}).get("einschaetzungen", []):
            if not isinstance(e, dict):
                continue
            part_id = str(e.get("part_id") or "")
            dringlichkeit = e.get("dringlichkeit")
            if part_id and dringlichkeit in DRINGLICHKEITEN:
                ergebnis[part_id] = (dringlichkeit, str(e.get("begruendung") or ""))
    return ergebnis
