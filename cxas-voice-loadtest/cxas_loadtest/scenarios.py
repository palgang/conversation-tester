"""Scenario (call script) definitions loaded from YAML."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class Turn:
    audio: Optional[str] = None          # WAV path (relative to audio_dir)
    dtmf: Optional[str] = None           # DTMF digits instead of speech
    text: Optional[str] = None           # what the caller says (used for TTS generation + logs)
    pause_ms: Optional[int] = None       # think time before speaking (overrides default)
    expect_agent_contains: List[str] = field(default_factory=list)
    timeout_s: Optional[float] = None
    label: Optional[str] = None


@dataclass
class Scenario:
    name: str
    turns: List[Turn]
    weight: int = 1
    expect_greeting: Optional[bool] = None
    variables: Dict[str, Any] = field(default_factory=dict)
    trailing_silence_ms: Optional[int] = None


def load_scenarios(path: str, audio_dir: str, only: Optional[str] = None) -> List[Scenario]:
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    wanted = {s.strip() for s in only.split(",")} if only else None
    result: List[Scenario] = []
    for raw in doc.get("scenarios", []):
        if wanted and raw["name"] not in wanted:
            continue
        turns = []
        for i, t in enumerate(raw.get("turns", []), start=1):
            turn = Turn(
                audio=os.path.join(audio_dir, t["audio"]) if t.get("audio") else None,
                dtmf=str(t["dtmf"]) if t.get("dtmf") is not None else None,
                text=t.get("text"),
                pause_ms=t.get("pause_ms"),
                expect_agent_contains=list(t.get("expect_agent_contains", []) or []),
                timeout_s=t.get("timeout_s"),
                label=t.get("label") or f"t{i}",
            )
            if not turn.audio and not turn.dtmf:
                raise ValueError(f"Scenario {raw['name']} turn {i}: needs 'audio' or 'dtmf'")
            turns.append(turn)
        result.append(Scenario(
            name=raw["name"],
            turns=turns,
            weight=int(raw.get("weight", 1)),
            expect_greeting=raw.get("expect_greeting"),
            variables=raw.get("variables") or {},
            trailing_silence_ms=raw.get("trailing_silence_ms"),
        ))
    if not result:
        raise ValueError(f"No scenarios loaded from {path} (filter={only!r})")
    return result


def pick(scenarios: List[Scenario]) -> Scenario:
    return random.choices(scenarios, weights=[s.weight for s in scenarios], k=1)[0]
