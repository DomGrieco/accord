from __future__ import annotations

import sys
from dataclasses import dataclass, field
from threading import RLock
from types import ModuleType
from typing import Any, cast

_RUNTIME_MODULE_KEY = "_hermes_human_gate_process_runtime_v1"


@dataclass
class ProcessRuntimeState:
    gate: Any = None
    x_config: Any = None
    registration_key: tuple[Any, ...] | None = None
    lock: RLock = field(default_factory=RLock)


def get_process_runtime_state() -> ProcessRuntimeState:
    candidate = ModuleType(_RUNTIME_MODULE_KEY)
    candidate.state = ProcessRuntimeState()  # type: ignore[attr-defined]
    holder = sys.modules.setdefault(_RUNTIME_MODULE_KEY, candidate)
    return cast(ProcessRuntimeState, holder.state)


def reset_process_runtime_for_tests() -> None:
    state = get_process_runtime_state()
    with state.lock:
        gate = state.gate
        if gate is not None:
            gate.store.close()
        state.gate = None
        state.x_config = None
        state.registration_key = None
