"""Load shapes. One Locust user == one concurrent phone call (back to back).

CXAS_STEPS="users:seconds[:spawn_rate],..."  e.g. "5:300,10:300,15:600,0:60"
Concurrency is the knob that maps directly onto the 'concurrent voice
streams' quota (default 15 per project+region), so steps are expressed in
users, not RPS.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

from locust import LoadTestShape


def parse_steps(spec: str) -> List[Tuple[int, float, float]]:
    steps = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        users, secs = int(bits[0]), float(bits[1])
        rate = float(bits[2]) if len(bits) > 2 else max(1.0, users / 10.0)
        steps.append((users, secs, rate))
    return steps


class StepLoadShape(LoadTestShape):
    """Stepped ramp: hold each concurrency level for its duration, then stop."""

    def __init__(self):
        super().__init__()
        self.steps = parse_steps(os.environ.get("CXAS_STEPS", ""))

    def tick(self) -> Optional[Tuple[int, float]]:
        t = self.get_run_time()
        elapsed = 0.0
        for users, secs, rate in self.steps:
            elapsed += secs
            if t < elapsed:
                return users, rate
        return None
