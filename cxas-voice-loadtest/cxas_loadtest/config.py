"""Environment-driven configuration for the CXAS voice load harness.

Every setting can be supplied as an environment variable (or in a ``.env``
file loaded by the locustfile). Nothing project-specific is hard-coded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return default if value is None else int(value)


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    return default if value is None else float(value)


@dataclass
class HarnessConfig:
    # --- Target agent -----------------------------------------------------
    project: str = ""
    location: str = ""
    app: str = ""
    deployment: Optional[str] = None   # short id or full resource name
    entry_agent: Optional[str] = None  # short id or full resource name
    time_zone: Optional[str] = None
    use_tool_fakes: bool = False
    session_prefix: str = "loadtest"

    # --- Transport / auth ---------------------------------------------------
    api_endpoint: str = "ces.googleapis.com"
    insecure: bool = False              # plaintext gRPC (mock server only)
    quota_project: Optional[str] = None
    share_channel: bool = True          # one gRPC channel per process vs per user
    channels_per_process: int = 1       # >1 spreads streams over several HTTP/2 connections

    # --- Audio ----------------------------------------------------------------
    input_encoding: str = "MULAW"       # MULAW | ALAW | LINEAR16
    input_sample_rate: int = 8000
    output_encoding: str = "MULAW"
    output_sample_rate: int = 8000
    chunk_ms: int = 60                  # 40-120ms recommended by the API docs
    noise_suppression: Optional[str] = None
    audio_dir: str = "audio"
    trim_silence: bool = True

    # --- Conversation behaviour ---------------------------------------------
    scenarios_file: str = "scenarios/example.yaml"
    scenario_filter: Optional[str] = None   # comma-separated scenario names
    expect_greeting: bool = True
    listen_to_agent_audio: bool = True      # caller waits for agent audio to "play" before speaking
    default_pause_ms: int = 600             # caller think-time after agent finishes
    turn_timeout_s: float = 30.0
    greeting_timeout_s: float = 20.0
    session_max_s: float = 600.0
    trailing_silence_ms: int = 1500         # silence streamed after the last turn before half-close

    # --- Locust / metrics -----------------------------------------------------
    per_turn_stats: bool = False            # also emit per scenario/turn named entries
    results_jsonl: Optional[str] = None     # raw per-turn records for offline analysis
    transcripts: bool = False               # include ASR / agent text in the JSONL
    send_lag_warn_ms: float = 40.0          # harness self-check: audio pacing drift

    # --- Quota / error handling -------------------------------------------
    max_sessions_per_process: int = 0       # 0 = unlimited; local safety cap
    backoff_initial_s: float = 2.0
    backoff_max_s: float = 60.0
    stop_after_quota_errors: int = 0        # 0 = never stop the run on 429s

    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> "HarnessConfig":
        return cls(
            project=_env("CXAS_PROJECT", ""),
            location=_env("CXAS_LOCATION", ""),
            app=_env("CXAS_APP", ""),
            deployment=_env("CXAS_DEPLOYMENT"),
            entry_agent=_env("CXAS_ENTRY_AGENT"),
            time_zone=_env("CXAS_TIME_ZONE"),
            use_tool_fakes=_env_bool("CXAS_USE_TOOL_FAKES", False),
            session_prefix=_env("CXAS_SESSION_PREFIX", "loadtest"),
            api_endpoint=_env("CXAS_API_ENDPOINT", "ces.googleapis.com"),
            insecure=_env_bool("CXAS_INSECURE", False),
            quota_project=_env("CXAS_QUOTA_PROJECT"),
            share_channel=_env_bool("CXAS_SHARE_CHANNEL", True),
            channels_per_process=_env_int("CXAS_CHANNELS_PER_PROCESS", 1),
            input_encoding=_env("CXAS_INPUT_ENCODING", "MULAW").upper(),
            input_sample_rate=_env_int("CXAS_INPUT_SAMPLE_RATE", 8000),
            output_encoding=_env("CXAS_OUTPUT_ENCODING", "MULAW").upper(),
            output_sample_rate=_env_int("CXAS_OUTPUT_SAMPLE_RATE", 8000),
            chunk_ms=_env_int("CXAS_CHUNK_MS", 60),
            noise_suppression=_env("CXAS_NOISE_SUPPRESSION"),
            audio_dir=_env("CXAS_AUDIO_DIR", "audio"),
            trim_silence=_env_bool("CXAS_TRIM_SILENCE", True),
            scenarios_file=_env("CXAS_SCENARIOS", "scenarios/example.yaml"),
            scenario_filter=_env("CXAS_SCENARIO_FILTER"),
            expect_greeting=_env_bool("CXAS_EXPECT_GREETING", True),
            listen_to_agent_audio=_env_bool("CXAS_LISTEN_TO_AGENT_AUDIO", True),
            default_pause_ms=_env_int("CXAS_DEFAULT_PAUSE_MS", 600),
            turn_timeout_s=_env_float("CXAS_TURN_TIMEOUT_S", 30.0),
            greeting_timeout_s=_env_float("CXAS_GREETING_TIMEOUT_S", 20.0),
            session_max_s=_env_float("CXAS_SESSION_MAX_S", 600.0),
            trailing_silence_ms=_env_int("CXAS_TRAILING_SILENCE_MS", 1500),
            per_turn_stats=_env_bool("CXAS_PER_TURN_STATS", False),
            results_jsonl=_env("CXAS_RESULTS_JSONL"),
            transcripts=_env_bool("CXAS_LOG_TRANSCRIPTS", False),
            send_lag_warn_ms=_env_float("CXAS_SEND_LAG_WARN_MS", 40.0),
            max_sessions_per_process=_env_int("CXAS_MAX_SESSIONS_PER_PROCESS", 0),
            backoff_initial_s=_env_float("CXAS_BACKOFF_INITIAL_S", 2.0),
            backoff_max_s=_env_float("CXAS_BACKOFF_MAX_S", 60.0),
            stop_after_quota_errors=_env_int("CXAS_STOP_AFTER_QUOTA_ERRORS", 0),
        )

    # ------------------------------------------------------------------------
    def validate(self) -> None:
        missing = [n for n, v in (("CXAS_PROJECT", self.project),
                                  ("CXAS_LOCATION", self.location),
                                  ("CXAS_APP", self.app)) if not v]
        if missing:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")
        if not 10 <= self.chunk_ms <= 500:
            raise ValueError("CXAS_CHUNK_MS should be between 10 and 500 (API recommends 40-120)")
        for enc in (self.input_encoding, self.output_encoding):
            if enc not in ("MULAW", "ALAW", "LINEAR16"):
                raise ValueError(f"Unsupported audio encoding {enc}")

    @property
    def app_name(self) -> str:
        return f"projects/{self.project}/locations/{self.location}/apps/{self.app}"

    def session_name(self, session_id: str) -> str:
        return f"{self.app_name}/sessions/{session_id}"

    def _qualify(self, value: Optional[str], kind: str) -> Optional[str]:
        if not value:
            return None
        return value if value.startswith("projects/") else f"{self.app_name}/{kind}/{value}"

    @property
    def deployment_name(self) -> Optional[str]:
        return self._qualify(self.deployment, "deployments")

    @property
    def entry_agent_name(self) -> Optional[str]:
        return self._qualify(self.entry_agent, "agents")
