from __future__ import annotations


class EffectUncertainError(RuntimeError):
    """The provider may have accepted the effect, so automatic retry is unsafe."""


class EffectDefinitiveFailureError(RuntimeError):
    """The effect failed before dispatch or was definitively rejected."""
