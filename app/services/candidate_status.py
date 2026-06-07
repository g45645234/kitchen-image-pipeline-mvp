from __future__ import annotations

APPROVED_FINAL = "approved_final"
APPROVED_REFERENCE = "approved_reference"
REFERENCE_ONLY_ROLE = "reference_only"

LEGACY_APPROVED_FINAL = "approved"
LEGACY_APPROVED_REFERENCE = "reference_only"


def candidate_status_filter_values(status: str) -> list[str]:
    if status in {APPROVED_FINAL, LEGACY_APPROVED_FINAL}:
        return [APPROVED_FINAL, LEGACY_APPROVED_FINAL]
    if status in {APPROVED_REFERENCE, LEGACY_APPROVED_REFERENCE}:
        return [APPROVED_REFERENCE, LEGACY_APPROVED_REFERENCE]
    return [status]


def is_approved_final_status(status: str | None) -> bool:
    return status in {APPROVED_FINAL, LEGACY_APPROVED_FINAL}
