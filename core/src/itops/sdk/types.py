"""Record types for modules (ADR 0008): definitions and value validation.

    from itops.sdk.types import FieldDef, TypeDef, TypeProblem, validate_values

``credential_ref(value, label)`` checks a 1Password reference outside record types (e.g. a
document's logins) with the same rule as credential fields.

Use ``module.types`` at runtime (list, get, validate). Show fields with the core
``ui.fields(...)`` component.
"""

from itops.core.types import (
    FIELD_KINDS,
    FieldDef,
    FieldKind,
    TypeDef,
    TypeProblem,
    TypeVersion,
    TypeView,
    credential_ref,
    validate_values,
)

__all__ = [
    "FIELD_KINDS",
    "FieldDef",
    "FieldKind",
    "TypeDef",
    "TypeProblem",
    "TypeVersion",
    "TypeView",
    "credential_ref",
    "validate_values",
]
