"""One immutable account interval; local estimates never replace remote facts."""

from dataclasses import dataclass

USD = 10**12
CENT = USD // 100


@dataclass(frozen=True, slots=True)
class Settlement:
    recharge: int
    consumption: int
    adjustment: int
    candidate: int
    unresolved: int
    method: str


def _automatic_recharge(candidate: int) -> int:
    # Apply the existing recognition threshold before rounding. Keep all pico
    # digits until integer division, including values beyond float precision.
    return (candidate + USD - 1) // USD * USD if candidate >= USD else 0


def settle(
    b0: int, b1: int, estimate: int, *, used: int | None, manual_r: int = 0, manual_a: int = 0
) -> Settlement:
    if used is not None:
        if used < 0:
            raise ValueError("A reset requires a new baseline")
        inflow = b1 - b0 + used - manual_a
        # Negative net inflow is a separate account adjustment, never a negative recharge.
        total_r = max(manual_r, _automatic_recharge(inflow))
        # Reliable U remains the consumption authority. A rounding difference
        # is explicit account adjustment/evidence, never fabricated usage.
        gap = inflow - total_r
        adjustment = gap if abs(gap) >= CENT else 0
        consumption = used if used >= CENT else 0
        return Settlement(
            total_r - manual_r,
            consumption,
            adjustment,
            inflow,
            gap - adjustment - (used - consumption),
            "remote_usage",
        )
    candidate = b1 - b0 + estimate - manual_a
    inferred = _automatic_recharge(candidate)
    recharge = max(0, inferred - manual_r)
    consumption = b0 + manual_r + recharge + manual_a - b1
    if consumption < 0:
        # Keep the equation explicit: C remains the estimate and A explains the gap.
        gap = b1 - (b0 + manual_r + recharge - estimate + manual_a)
        adjustment = gap if abs(gap) >= CENT else 0
        reported = estimate if abs(estimate) >= CENT else 0
        return Settlement(
            recharge, reported, adjustment, candidate, gap + reported - estimate, "balance_unresolved"
        )
    reported = consumption if consumption >= CENT else 0
    return Settlement(recharge, reported, 0, candidate, reported - consumption, "balance_estimate")
