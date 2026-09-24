from typing import Literal

from tesla_common.errors import DomainError

Actor = Literal["PASSENGER", "DRIVER", "SYSTEM"]

TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("REQUESTED", "MATCHED"): frozenset({"SYSTEM", "DRIVER"}),
    ("REQUESTED", "CANCELLED"): frozenset({"PASSENGER", "SYSTEM"}),
    ("MATCHED", "DRIVER_ARRIVED"): frozenset({"DRIVER"}),
    ("MATCHED", "CANCELLED"): frozenset({"PASSENGER", "DRIVER"}),
    ("DRIVER_ARRIVED", "STARTED"): frozenset({"DRIVER"}),
    ("DRIVER_ARRIVED", "CANCELLED"): frozenset({"DRIVER"}),
    ("STARTED", "COMPLETED"): frozenset({"DRIVER"}),
}


def assert_transition(current: str, target: str, actor: Actor) -> None:
    if actor not in TRANSITIONS.get((current, target), frozenset()):
        raise DomainError("INVALID_TRANSITION", f"{current} → {target} is not allowed for {actor}", 409)
