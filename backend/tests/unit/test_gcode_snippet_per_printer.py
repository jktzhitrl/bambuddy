"""A ``printer:<id>`` G-code snippet overrides the model snippet for that printer only."""

from backend.app.services.print_scheduler import _resolve_gcode_snippets

SNIPPETS = {
    "P1S": {"start_gcode": "", "end_gcode": "; model"},
    "printer:7": {"start_gcode": "", "end_gcode": "; eject"},
}


def test_printer_entry_wins_over_model():
    assert _resolve_gcode_snippets(SNIPPETS, 7, "P1S")["end_gcode"] == "; eject"


def test_other_printer_of_same_model_uses_model_entry():
    assert _resolve_gcode_snippets(SNIPPETS, 8, "P1S")["end_gcode"] == "; model"


def test_no_entry_returns_empty():
    assert _resolve_gcode_snippets(SNIPPETS, 9, "X1C") == {}
    assert _resolve_gcode_snippets({}, 7, None) == {}
