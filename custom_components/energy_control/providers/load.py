"""Household load forecast from history. Pure Python."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from math import isfinite


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
    """Build a profile from (hour_start, mean_kW) samples.

    Each hour uses a trimmed mean: the highest and lowest 10 % of days are
    dropped, so a one-off EV session or defrost cycle does not raise that hour
    for weeks, while regular behaviour still counts in full.
    """
    values: dict[tuple[bool, int], list[float]] = defaultdict(list)
    all_values: list[float] = []
    for start, kw in hourly_mean_kw:
        if kw is None or not isfinite(kw) or kw < 0:
            continue
        local = start.astimezone(tz)
        key = (local.weekday() >= 5 if split_weekend else False, local.hour)
        values[key].append(kw)
        all_values.append(kw)
    profile = {key: trimmed_mean(samples) for key, samples in values.items()}
    if not split_weekend:
        profile.update({(True, h): v for (_, h), v in list(profile.items())})
    mean = trimmed_mean(all_values) if all_values else fallback_kw
    return LoadProfile(tz=tz, kw=profile, fallback_kw=mean)


def trimmed_mean(samples: list[float], fraction: float = 0.1) -> float:
    """Mean without the top and bottom `fraction` (only with 5+ samples)."""
    ordered = sorted(samples)
    cut = int(len(ordered) * fraction) if len(ordered) >= 5 else 0
    kept = ordered[cut : len(ordered) - cut] or ordered
    return sum(kept) / len(kept)
