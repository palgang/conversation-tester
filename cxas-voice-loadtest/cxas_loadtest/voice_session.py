"""One simulated phone call against CXAS BidiRunSession.

Timeline of a call
------------------
* Open the bidi stream, send ``SessionConfig`` (+ optional session variables).
* A sender loop streams audio **continuously in real time** (one chunk every
  ``chunk_ms``, silence when the caller is not talking), exactly like a
  telephony gateway would. Chunks are sent at the *end* of their capture
  window, so "the caller stopped speaking" == "last utterance chunk sent".
* A receiver loop consumes server messages and timestamps them against the
  turn currently being measured.
* The driver (``run``) follows the scenario script: wait for greeting, let the
  agent audio "play" (real-time duration), think, speak the next utterance,
  wait for ``turn_completed``... then hang up (half-close).

Works under plain threads and under gevent (Locust monkey-patches
``threading``/``time`` and the locustfile enables gRPC's gevent integration).
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field, fields
from typing import Callable, Deque, List, Optional

import grpc
from google.api_core import exceptions as gexc
from google.cloud import ces_v1
from google.protobuf import struct_pb2

from .audio import AudioLibrary, duration_ms
from .config import HarnessConfig
from .scenarios import Scenario, Turn

log = logging.getLogger(__name__)


class CallError(Exception):
    """A call failed. ``code`` is a gRPC status name or a harness code
    (TURN_TIMEOUT, GREETING_TIMEOUT, NO_AGENT_AUDIO, GO_AWAY, ...)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


@dataclass
class TurnResult:
    scenario: str
    label: str
    index: int                       # 0 = greeting, 1..n caller turns
    kind: str                        # greeting | speech | dtmf
    speech_ms: float = 0.0           # duration of the caller utterance
    # latencies in ms, measured from end of caller speech (or config sent for greeting)
    first_recognition_ms: Optional[float] = None   # relative to speech START
    first_output_ms: Optional[float] = None
    first_text_ms: Optional[float] = None
    ttfa_ms: Optional[float] = None                 # time to first agent audio
    complete_ms: Optional[float] = None             # time to turn_completed
    agent_audio_bytes: int = 0
    agent_audio_ms: float = 0.0
    interruptions: int = 0
    premature_completions: int = 0   # turn_completed received while caller still talking
    transcript: str = ""
    agent_text: str = ""
    check_failed: Optional[str] = None
    error: Optional[str] = None

    # internal timestamps (monotonic seconds) - not serialised
    _speech_start: Optional[float] = field(default=None, repr=False)
    _speech_end: Optional[float] = field(default=None, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        # not dataclasses.asdict(): it deep-copies the threading.Event
        return {f.name: getattr(self, f.name) for f in fields(self) if not f.name.startswith("_")}


@dataclass
class CallResult:
    scenario: str
    session_id: str
    started_at: float                  # wall clock epoch seconds
    duration_ms: float = 0.0
    turns: List[TurnResult] = field(default_factory=list)
    ended_by: str = "hangup"           # hangup | end_session | go_away | error
    error_code: Optional[str] = None
    error: Optional[str] = None
    max_send_lag_ms: float = 0.0
    completed_turns: int = 0
    connect_ms: Optional[float] = None  # stream open -> first server message


@dataclass
class _OutItem:
    kind: str                          # audio | dtmf
    data: object
    on_sent: Optional[Callable[[float], None]] = None


class VoiceCall:
    def __init__(self, cfg: HarnessConfig, client: ces_v1.SessionServiceClient,
                 audio: AudioLibrary, scenario: Scenario, session_id: str):
        self.cfg = cfg
        self.client = client
        self.audio = audio
        self.scenario = scenario
        self.session_id = session_id
        self.result = CallResult(scenario.name, session_id, time.time())

        self._outbox: Deque[_OutItem] = collections.deque()
        self._stop = threading.Event()
        self._ended = threading.Event()          # agent ended the session / go-away / stream closed
        self._lock = threading.Lock()
        self._current: Optional[TurnResult] = None
        self._playback_end = 0.0                 # monotonic time when agent audio finishes "playing"
        self._stream_error: Optional[BaseException] = None
        self._responses = None
        self._out_chunk_s = 0.0

    # ------------------------------------------------------------------ requests
    def _session_config(self) -> ces_v1.SessionConfig:
        cfg = self.cfg
        sc = ces_v1.SessionConfig(
            session=cfg.session_name(self.session_id),
            input_audio_config=ces_v1.InputAudioConfig(
                audio_encoding=ces_v1.AudioEncoding[cfg.input_encoding],
                sample_rate_hertz=cfg.input_sample_rate,
            ),
            output_audio_config=ces_v1.OutputAudioConfig(
                audio_encoding=ces_v1.AudioEncoding[cfg.output_encoding],
                sample_rate_hertz=cfg.output_sample_rate,
            ),
        )
        if cfg.noise_suppression:
            sc.input_audio_config.noise_suppression_level = cfg.noise_suppression
        if cfg.deployment_name:
            sc.deployment = cfg.deployment_name
        if cfg.entry_agent_name:
            sc.entry_agent = cfg.entry_agent_name
        if cfg.time_zone:
            sc.time_zone = cfg.time_zone
        if cfg.use_tool_fakes:
            sc.use_tool_fakes = True
        return sc

    def _requests(self):
        Msg, Inp = ces_v1.BidiSessionClientMessage, ces_v1.SessionInput
        yield Msg(config=self._session_config())
        self._config_sent = time.monotonic()
        if self.scenario.variables:
            s = struct_pb2.Struct()
            s.update(self.scenario.variables)
            yield Msg(realtime_input=Inp(variables=s))

        chunk_s = self.cfg.chunk_ms / 1000.0
        silence = self.audio.silence_chunk
        next_t = time.monotonic() + chunk_s
        while not self._stop.is_set():
            item = self._outbox.popleft() if self._outbox else None
            if item is not None and item.kind == "dtmf":
                yield Msg(realtime_input=Inp(dtmf=item.data))
                if item.on_sent:
                    item.on_sent(time.monotonic())
                continue
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if self._stop.is_set():
                break
            sent_at = time.monotonic()
            # How late this chunk leaves vs. its real-time schedule: a load
            # generator health signal (CPU-starved workers distort latency).
            lag_ms = (sent_at - next_t) * 1000.0
            if lag_ms > self.result.max_send_lag_ms:
                self.result.max_send_lag_ms = lag_ms
            if lag_ms > 1000:  # badly starved: resync instead of bursting to catch up
                next_t = sent_at
            yield Msg(realtime_input=Inp(audio=item.data if item else silence))
            if item is not None and item.on_sent:
                item.on_sent(sent_at)
            next_t += chunk_s
        # returning half-closes the request stream

    # ------------------------------------------------------------------ responses
    def _receive_loop(self):
        try:
            for msg in self._responses:
                self._on_message(msg, time.monotonic())
                if self._stop.is_set():
                    break
        except grpc.RpcError as e:  # raw gRPC errors
            if not self._stop.is_set():
                self._stream_error = e
        except gexc.GoogleAPICallError as e:  # api-core wrapped errors
            if not self._stop.is_set():
                self._stream_error = e
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                self._stream_error = e
        finally:
            self._ended.set()
            with self._lock:
                if self._current is not None:
                    self._current._done.set()

    def _on_message(self, msg: ces_v1.BidiSessionServerMessage, now: float):
        if self.result.connect_ms is None:
            self.result.connect_ms = (now - self._t0) * 1000
        which = ces_v1.BidiSessionServerMessage.pb(msg).WhichOneof("message_type")
        with self._lock:
            turn = self._current
            if which == "recognition_result":
                if turn is not None and turn.kind != "greeting":
                    if turn.first_recognition_ms is None and turn._speech_start is not None:
                        turn.first_recognition_ms = (now - turn._speech_start) * 1000
                    turn.transcript = msg.recognition_result.transcript
            elif which == "interruption_signal":
                self._playback_end = now  # stop "playing" agent audio
                if turn is not None:
                    turn.interruptions += 1
            elif which == "end_session":
                self._mark_ended("end_session", turn, now)
            elif which == "go_away":
                self._mark_ended("go_away", turn, now)
            elif which == "session_output":
                self._on_output(msg.session_output, turn, now)

    def _on_output(self, out: ces_v1.SessionOutput, turn: Optional[TurnResult], now: float):
        kind = ces_v1.SessionOutput.pb(out).WhichOneof("output_type")
        if kind == "audio":
            n = len(out.audio)
            dur_s = duration_ms(n, self.cfg.output_encoding, self.cfg.output_sample_rate) / 1000
            self._playback_end = max(self._playback_end, now) + dur_s
        if kind == "end_session":
            self._mark_ended("end_session", turn, now)

        if turn is None or turn._done.is_set():
            return
        if turn._speech_end is None:
            # Agent output while the caller is still talking (endpointing fired mid-utterance).
            if out.turn_completed:
                turn.premature_completions += 1
            return
        rel = (now - turn._speech_end) * 1000
        if kind is not None and turn.first_output_ms is None:
            turn.first_output_ms = rel
        if kind == "audio":
            if turn.ttfa_ms is None:
                turn.ttfa_ms = rel
            turn.agent_audio_bytes += len(out.audio)
            turn.agent_audio_ms += duration_ms(len(out.audio), self.cfg.output_encoding,
                                               self.cfg.output_sample_rate)
        elif kind == "text":
            if turn.first_text_ms is None:
                turn.first_text_ms = rel
            turn.agent_text = (turn.agent_text + " " + out.text).strip()
        if out.turn_completed:
            turn.complete_ms = rel
            turn._done.set()

    def _mark_ended(self, how: str, turn: Optional[TurnResult], now: float):
        if self.result.ended_by == "hangup":
            self.result.ended_by = how
        self._ended.set()
        if turn is not None and not turn._done.is_set() and turn._speech_end is not None:
            if turn.complete_ms is None:
                turn.complete_ms = (now - turn._speech_end) * 1000
            turn._done.set()

    # ------------------------------------------------------------------ driver
    def run(self) -> CallResult:
        t0 = self._t0 = time.monotonic()
        # Use the raw gRPC multicallable, not client.bidi_run_session(): the
        # api-core wrapper (a) blocks until the FIRST server message arrives,
        # which would skew greeting timing and deadlock agents that wait for
        # the caller to speak first, and (b) carries a default retry policy
        # that must never replay a half-consumed audio stream.
        rpc = self.client.transport.bidi_run_session
        self._responses = rpc(self._requests(), timeout=self.cfg.session_max_s)
        rx = threading.Thread(target=self._receive_loop, name=f"rx-{self.session_id}", daemon=True)
        rx.start()
        try:
            self._script()
            if self.result.ended_by == "go_away" and self.result.completed_turns < len(self.scenario.turns):
                raise CallError("GO_AWAY", "server sent GoAway before the script finished")
            self._raise_stream_error()
        except CallError as e:
            self.result.error_code, self.result.error = e.code, str(e)
            if self.result.ended_by == "hangup":
                self.result.ended_by = "error"
        finally:
            self._stop.set()
            rx.join(timeout=5)
            if rx.is_alive() and self._responses is not None:
                try:
                    self._responses.cancel()
                except Exception:  # noqa: BLE001
                    pass
                rx.join(timeout=2)
            self.result.duration_ms = (time.monotonic() - t0) * 1000
        return self.result

    def _script(self):
        expect_greeting = (self.scenario.expect_greeting
                           if self.scenario.expect_greeting is not None else self.cfg.expect_greeting)
        if expect_greeting:
            g = TurnResult(self.scenario.name, "greeting", 0, "greeting")
            with self._lock:
                g._speech_start = g._speech_end = time.monotonic()
                self._current = g
            self._await(g, self.cfg.greeting_timeout_s, "GREETING_TIMEOUT")

        for i, turn in enumerate(self.scenario.turns, start=1):
            if self._ended.is_set():
                break
            self._listen_and_think(turn)
            if self._ended.is_set():
                break
            tr = self._speak(turn, i)
            self._await(tr, turn.timeout_s or self.cfg.turn_timeout_s, "TURN_TIMEOUT")
            self._check(turn, tr)
            self.result.completed_turns += 1

        # Hang up: let the last agent prompt play, stream a little silence, half-close.
        if not self._ended.is_set():
            self._wait_playback()
            trailing = self.scenario.trailing_silence_ms
            trailing = self.cfg.trailing_silence_ms if trailing is None else trailing
            self._ended.wait(trailing / 1000.0)

    def _listen_and_think(self, turn: Turn):
        self._wait_playback()
        pause = turn.pause_ms if turn.pause_ms is not None else self.cfg.default_pause_ms
        if pause > 0:
            self._ended.wait(pause / 1000.0)

    def _wait_playback(self):
        if not self.cfg.listen_to_agent_audio:
            return
        while not self._ended.is_set():
            with self._lock:
                remaining = self._playback_end - time.monotonic()
            if remaining <= 0:
                return
            self._ended.wait(min(remaining, 0.25))

    def _speak(self, turn: Turn, index: int) -> TurnResult:
        tr = TurnResult(self.scenario.name, turn.label or f"t{index}", index,
                        "dtmf" if turn.dtmf else "speech")

        def started(ts: float):
            with self._lock:
                if tr._speech_start is None:
                    tr._speech_start = ts

        def finished(ts: float):
            with self._lock:
                if tr._speech_start is None:
                    tr._speech_start = ts
                tr._speech_end = ts

        with self._lock:
            self._current = tr
        if turn.dtmf:
            self._outbox.append(_OutItem("dtmf", turn.dtmf, finished))
        else:
            utt = self.audio.load(turn.audio)
            tr.speech_ms = utt.duration_ms
            n = len(utt.chunks)
            for j, chunk in enumerate(utt.chunks):
                cb = finished if j == n - 1 else (started if j == 0 else None)
                self._outbox.append(_OutItem("audio", chunk, cb))
        return tr

    def _await(self, tr: TurnResult, timeout_s: float, timeout_code: str):
        self.result.turns.append(tr)
        # The deadline counts from now; speaking time is added for speech turns.
        deadline = time.monotonic() + timeout_s + tr.speech_ms / 1000.0
        while not tr._done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                tr.error = timeout_code
                raise CallError(timeout_code, f"{tr.label}: no turn_completed within {timeout_s:.0f}s")
            tr._done.wait(min(remaining, 0.5))
            if self._ended.is_set() and not tr._done.is_set():
                break
        self._raise_stream_error(tr)
        if not tr._done.is_set():
            # stream closed without completing this turn
            if self.result.ended_by in ("end_session",) and tr.kind != "greeting":
                return
            tr.error = "STREAM_CLOSED"
            raise CallError("STREAM_CLOSED", f"{tr.label}: stream ended before turn completed")
        if tr.ttfa_ms is None and self.result.ended_by == "hangup":
            tr.error = "NO_AGENT_AUDIO"
            raise CallError("NO_AGENT_AUDIO", f"{tr.label}: turn completed without agent audio")

    def _check(self, turn: Turn, tr: TurnResult):
        if not turn.expect_agent_contains:
            return
        text = tr.agent_text.lower()
        if not text:
            return  # agent text not returned for this session type: nothing to check
        missing = [w for w in turn.expect_agent_contains if w.lower() not in text]
        if missing:
            tr.check_failed = f"agent response missing {missing}"

    def _raise_stream_error(self, tr: Optional[TurnResult] = None):
        err = self._stream_error
        if err is None:
            return
        code = _status_name(err)
        if tr is not None:
            tr.error = code
        raise CallError(code, _short(err))


def _status_name(err: BaseException) -> str:
    if isinstance(err, grpc.RpcError) and hasattr(err, "code"):
        try:
            return err.code().name
        except Exception:  # noqa: BLE001
            pass
    if isinstance(err, gexc.GoogleAPICallError) and err.grpc_status_code is not None:
        return err.grpc_status_code.name
    return type(err).__name__


def _short(err: BaseException) -> str:
    if isinstance(err, grpc.RpcError) and hasattr(err, "details"):
        try:
            return (err.details() or "")[:200]
        except Exception:  # noqa: BLE001
            pass
    if isinstance(err, gexc.GoogleAPICallError):
        return (err.message or "")[:200]
    return str(err)[:200]
