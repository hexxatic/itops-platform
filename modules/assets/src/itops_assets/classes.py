"""Computer classes: Standard or Engineering, laptop or desktop. Pure, never AI.

A computer's class is the tier its model is on (admins keep two lists of model patterns,
Engineering and Standard) and its form factor: the Device kind (Laptop or Workstation), unless
the model's name says it's a laptop (a docked ProBook scanned as a desktop is still a laptop). A
person can set a computer's *Class* field to override it. A model on neither list has no
class until an admin adds it, so a new model is noticed instead of guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from itops_assets import readiness as rd

TIERS = ("Engineering", "Standard")  # checked in this order: Engineering first
FORMS = {"Laptop": "Laptop", "Workstation": "Desktop"}  # Device kind -> form factor
CLASSES = tuple(f"{t} {f}" for t in TIERS for f in ("Laptop", "Desktop"))
# Model names that say "laptop" whatever the scan's chassis type said.
LAPTOP_NAMES = re.compile(
    r"notebook|laptop|mobile workstation|zbook|probook|elitebook|latitude|thinkpad", re.I
)
# A starting point (vendors' product lines); admins change them on the classes page.
PRESETS = {
    "Engineering": "\n".join(
        [
            "# Mobile workstations",
            "ZBook",
            r"Precision [57]\d{3}",
            "# Desktop workstations",
            r"^HP Z\d",
            r"Precision \d+ Tower",
            r"Precision 34\d\d",
            "ThinkStation",
        ]
    ),
    "Standard": "\n".join(
        [
            "ProBook",
            "EliteBook",
            "Latitude",
            "Surface",
            r"Precision 35\d\d  # entry mobile workstations issued as standard laptops",
            "OptiPlex",
            r"Elite ?Mini",
            "EliteDesk",
            "ProDesk",
            "ThinkPad",
            "ThinkCentre",
        ]
    ),
}


@dataclass
class Rules:
    """Model patterns per tier (regular expressions, matched anywhere, ignoring case)."""

    patterns: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    @classmethod
    def from_text(cls, texts: dict[str, str]) -> Rules:
        return cls({t: rd.pattern_lines(texts.get(t, "")) for t in TIERS})


def tier_of(model: str | None, rules: Rules) -> str | None:
    """The tier whose patterns match the model first (Engineering before Standard)."""
    name = " ".join(str(model or "").split())
    if not name:
        return None
    for tier in TIERS:
        for p in rules.patterns.get(tier, []):
            try:
                if re.search(p, name, re.I):
                    return tier
            except re.error:
                continue
    return None


def form_of(values: dict[str, Any]) -> str | None:
    """Laptop or Desktop: the kind, unless the model's name says laptop; None otherwise."""
    form = FORMS.get(str(values.get("kind") or ""))
    if form and LAPTOP_NAMES.search(str(values.get("model") or "")):
        return "Laptop"
    return form


def classify(values: dict[str, Any], rules: Rules) -> tuple[str | None, str]:
    """(class, basis): basis "set" (a person chose it), "model" (the lists), or "" with no
    class (not a laptop or desktop, or a model on neither list)."""
    chosen = values.get("computer_class")
    if chosen in CLASSES:
        return str(chosen), "set"
    form = form_of(values)
    tier = tier_of(values.get("model"), rules) if form else None
    if form is None or tier is None:
        return None, ""
    return f"{tier} {form}", "model"
