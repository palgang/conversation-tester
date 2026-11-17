"""Turn CallResults into Locust request events (+ optional raw JSONL)."""

from __future__ import annotations

import json
import os
import threading
from typing import Optional

from .config import HarnessConfig
from .voice_session import CallResult, TurnResult

# request_type values as they appear in the Locust UI / CSV "Type" column
T_TURN = "VOICE"
T_CALL = "CALL"
T_HARNESS = "HARNESS"


_HARNESS_CODES = {"TURN_TIMEOUT", "GREETING_TIMEOUT", "NO_AGENT_AUDIO", "STREAM_CLOSED", "GO_AWAY"}


class TurnFailure(Exception):
    pass


class MetricsSink:
    def __init__(self, cfg: HarnessConfig, request_event):
        self.cfg = cfg
        self.fire = request_event.fire
        self._jsonl_lock = threading.Lock()
        self._jsonl = None
        if cfg.results_jsonl:
            path = cfg.results_jsonl.replace("{pid}", str(os.getpid()))  # one file per worker
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._jsonl = open(path, "a", encoding="utf-8")

    def close(self):
        if self._jsonl:
            self._jsonl.close()
            self._jsonl = None

    # ------------------------------------------------------------------
    def _emit(self, rtype: str, name: str, ms: float, length: int = 0,
              exc: Optional[BaseException] = None, ctx: Optional[dict] = None):
        self.fire(request_type=rtype, name=name, response_time=max(0.0, ms),
                  response_length=length, exception=exc, context=ctx or {})
        if self.cfg.per_turn_stats and ctx and ctx.get("turn_label"):
            self.fire(request_type=rtype, name=f"{name} [{ctx['scenario']}/{ctx['turn_label']}]",
                      response_time=max(0.0, ms), response_length=length,
                      exception=exc, context=ctx)

    def record_turn(self, t: TurnResult, call_error_code: Optional[str]):
        ctx = {"scenario": t.scenario, "turn_label": t.label}
        prefix = "greeting" if t.kind == "greeting" else "turn"
        if t.error:
            # Failures go to their own row so 0 ms failures never pollute the
            # latency percentiles of the success rows. A gRPC status error is
            # reported once, on the call / session.start row, not again here.
            if t.error in _HARNESS_CODES or t.error != call_error_code:
                self._emit(T_TURN, f"{prefix}.failed", 0, exc=TurnFailure(t.error), ctx=ctx)
            return
        if t.ttfa_ms is not None:
            self._emit(T_TURN, f"{prefix}.ttfa", t.ttfa_ms, t.agent_audio_bytes, ctx=ctx)
        if t.complete_ms is not None:
            self._emit(T_TURN, f"{prefix}.complete", t.complete_ms, ctx=ctx)
        if t.kind == "speech" and t.first_recognition_ms is not None:
            self._emit(T_TURN, "turn.asr_first_partial", t.first_recognition_ms, ctx=ctx)
        if t.check_failed:
            self._emit(T_TURN, "turn.failed", 0, exc=TurnFailure(f"CONTENT_CHECK {t.check_failed}"), ctx=ctx)
        if t.premature_completions:
            self._emit(T_TURN, "turn.failed", 0,
                       exc=TurnFailure("PREMATURE_ENDPOINT agent completed a turn while caller was speaking"),
                       ctx=ctx)

    def record_call(self, r: CallResult):
        ctx = {"scenario": r.scenario}
        if r.connect_ms is None and r.error_code:
            # Never got a single server message: rejected at session start
            # (quota, auth, bad config...). Keeps rejected sessions out of the
            # call-duration statistics.
            self._emit(T_CALL, "session.rejected", 0, exc=TurnFailure(r.error_code), ctx=ctx)
            self._write_jsonl(r)
            return
        if r.connect_ms is not None:
            self._emit(T_CALL, "session.start", r.connect_ms, ctx=ctx)
        for t in r.turns:
            self.record_turn(t, r.error_code)
        exc = TurnFailure(r.error_code) if r.error_code else None
        self._emit(T_CALL, f"call {r.scenario}", r.duration_ms, r.completed_turns, exc=exc, ctx=ctx)
        lag_exc = None
        if r.max_send_lag_ms > self.cfg.send_lag_warn_ms:
            lag_exc = TurnFailure("LOAD_GENERATOR_LAG cannot keep real-time audio pacing")
        self._emit(T_HARNESS, "audio_send_lag_max", r.max_send_lag_ms, exc=lag_exc)
        self._write_jsonl(r)

    def _write_jsonl(self, r: CallResult):
        if not self._jsonl:
            return
        rec = {
            "scenario": r.scenario, "session_id": r.session_id, "started_at": r.started_at,
            "duration_ms": round(r.duration_ms, 1), "ended_by": r.ended_by,
            "error_code": r.error_code, "error": r.error,
            "max_send_lag_ms": round(r.max_send_lag_ms, 1), "completed_turns": r.completed_turns,
            "connect_ms": None if r.connect_ms is None else round(r.connect_ms, 1),
            "turns": [],
        }
        for t in r.turns:
            d = t.to_dict()
            if not self.cfg.transcripts:
                d.pop("transcript", None)
                d.pop("agent_text", None)
            rec["turns"].append({k: (round(v, 1) if isinstance(v, float) else v) for k, v in d.items()})
        line = json.dumps(rec)
        with self._jsonl_lock:
            self._jsonl.write(line + "\n")
            self._jsonl.flush()
