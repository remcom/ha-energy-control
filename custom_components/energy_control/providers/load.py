"""Household load forecast from history. Pure Python."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo


@dataclass(slots=True)
class LoadProfile:
    """Average load (kW) per (is_weekend, hour-of-day) in local time."""

    tz: tzinfo
    kw: dict[tuple[bool, int], float] = field(default_factory=dict)
    fallback_kw: float = 0.4

    def at(self, when: datetime) -> float:
        """Expected load in kW at `when`."""
        local = when.astimezone(self.tz)
        key = (local.weekday() >= 5, local.hour)
        if key in self.kw:
            return self.kw[key]
        # Fall back to the same hour on any day type, then the global mean.
        other = (not key[0], key[1])
        if other in self.kw:
            return self.kw[other]
        return self.fallback_kw


def build_load_profile(
    hourly_mean_kw: Iterable[tuple[datetime, float]],
    tz: tzinfo,
    fallback_kw: float = 0.4,
    split_weekend: bool = True,
) -> LoadProfile:
    """Build a profile from (hour_start, mean_kW) samples."""
    sums: dict[tuple[bool, int], float] = defaultdict(float)
    counts: dict[tuple[bool, int], int] = defaultdict(int)
    all_values: list[float] = []
    for start, kw in hourly_mean_kw:
        if kw is None or kw < 0:
            continue
        local = start.astimezone(tz)
        key = (local.weekday() >= 5 if split_weekend else False, local.hour)
        sums[key] += kw
        counts[key] += 1
        all_values.append(kw)
    profile = {key: sums[key] / counts[key] for key in sums}
    if not split_weekend:
        profile.update({(True, h): v for (_, h), v in list(profile.items())})
    mean = sum(all_values) / len(all_values) if all_values else fallback_kw
    return LoadProfile(tz=tz, kw=profile, fallback_kw=mean)
