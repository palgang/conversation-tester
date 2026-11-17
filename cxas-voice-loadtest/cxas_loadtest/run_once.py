"""Place a single call without Locust and print what happened, turn by turn.

Use this first against the real agent to validate config, auth, audio format
and the scenario script before starting a load run.

    python -m cxas_loadtest.run_once --scenario check_order_status
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid

from .audio import AudioLibrary
from .client import build_client
from .config import HarnessConfig
from .scenarios import load_scenarios
from .voice_session import VoiceCall


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", help="scenario name (default: first in file)")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except ImportError:
        pass

    cfg = HarnessConfig.from_env()
    cfg.validate()
    scenarios = load_scenarios(cfg.scenarios_file, cfg.audio_dir, a.scenario)
    audio = AudioLibrary(cfg.input_encoding, cfg.input_sample_rate, cfg.chunk_ms, cfg.trim_silence)
    sid = f"{cfg.session_prefix}-debug-{uuid.uuid4().hex[:12]}"
    print(f"session: {cfg.session_name(sid)}")
    r = VoiceCall(cfg, build_client(cfg), audio, scenarios[0], sid).run()

    if a.json:
        out = {k: v for k, v in r.__dict__.items() if k != "turns"}
        out["turns"] = [t.to_dict() for t in r.turns]
        print(json.dumps(out, indent=2, default=str))
        return
    fmt = lambda v: f"{'-':>8}" if v is None else f"{v:8.0f}"  # noqa: E731
    print(f"\n{'turn':<14}{'speech':>8}{'asr1st':>8}{'ttfa':>8}{'done':>8}{'audio':>8}  transcript / agent")
    for t in r.turns:
        print(f"{t.label:<14}{fmt(t.speech_ms)}{fmt(t.first_recognition_ms)}{fmt(t.ttfa_ms)}"
              f"{fmt(t.complete_ms)}{fmt(t.agent_audio_ms)}  {t.transcript!r} -> {t.agent_text!r}"
              + (f"  ERROR {t.error}" if t.error else "")
              + (f"  CHECK {t.check_failed}" if t.check_failed else ""))
    print(f"\ncall: {r.duration_ms / 1000:.1f}s ended_by={r.ended_by} turns={r.completed_turns}/"
          f"{len(scenarios[0].turns)} max_send_lag={r.max_send_lag_ms:.0f}ms"
          + (f" ERROR {r.error}" if r.error else ""))


if __name__ == "__main__":
    main()
