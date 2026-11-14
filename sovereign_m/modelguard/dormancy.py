"""Hook for a future dormancy measurement. Intentionally not implemented.

Per a November 2026 write-up (see docs/modelguard.md; claims UNVERIFIED): a
weight-space backdoor may target neurons that rarely fire on ordinary
inputs. Confirming that needs activations on a calibration set of benign
prompts, collected from a live model. This package compares weights and
reviews tool commands. It does not invent activation scores.

To wire a real probe later, subclass ``DormancyHook`` and implement
``collect`` plus ``score``. ``collect`` should call a local endpoint on
benign prompts and return per-neuron activation stats. ``score`` should
mark which of the neuron-diff outliers look dormant. Until then, calling
either method raises ``NotImplementedError``.
"""

from __future__ import annotations

from typing import Any


UNAVAILABLE = (
    "Dormancy scoring needs activations from a live model and a "
    "calibration set of benign prompts. modelguard is weights-and-policy "
    "only, so this hook is unimplemented on purpose."
)


class DormancyHook:
    """Extension point. The base class does not fake results."""

    def collect(self, prompts: list[str], *, endpoint: str = "", model: str = "") -> dict[str, Any]:
        raise NotImplementedError(UNAVAILABLE)

    def score(self, activations: dict[str, Any], neuron_ids: list[int]) -> dict[str, Any]:
        raise NotImplementedError(UNAVAILABLE)


def unavailable_reason() -> str:
    return UNAVAILABLE
