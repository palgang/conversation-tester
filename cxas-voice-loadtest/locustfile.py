"""Locust entry point: API-level voice load test of a Google CXAS agent.

One Locust user == one simulated caller placing calls back to back, so the
Locust user count is the number of concurrent voice streams.

    locust -f locustfile.py --headless -u 5 -r 1 -t 10m --csv results/run1
    CXAS_STEPS="5:300,10:300,15:600" locust -f locustfile.py --headless --csv results/step

See README.md for all CXAS_* settings.
"""

# gRPC must cooperate with gevent (Locust's concurrency model) BEFORE any
# channel is created.
import grpc.experimental.gevent as grpc_gevent

grpc_gevent.init_gevent()

import logging  # noqa: E402
import os  # noqa: E402
import random  # noqa: E402
import uuid  # noqa: E402

import gevent  # noqa: E402
from gevent.lock import BoundedSemaphore  # noqa: E402
from locust import User, between, events, task  # noqa: E402

try:  # optional .env support
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except ImportError:
    pass

from cxas_loadtest.audio import AudioLibrary  # noqa: E402
from cxas_loadtest.client import ClientPool  # noqa: E402
from cxas_loadtest.config import HarnessConfig  # noqa: E402
from cxas_loadtest.metrics import MetricsSink  # noqa: E402
from cxas_loadtest.scenarios import load_scenarios, pick  # noqa: E402
from cxas_loadtest.voice_session import VoiceCall  # noqa: E402

log = logging.getLogger("cxas")

CFG = HarnessConfig.from_env()
CFG.validate()
SCENARIOS = load_scenarios(CFG.scenarios_file, CFG.audio_dir, CFG.scenario_filter)
AUDIO = AudioLibrary(CFG.input_encoding, CFG.input_sample_rate, CFG.chunk_ms, CFG.trim_silence)
for _s in SCENARIOS:  # fail fast on missing / unreadable audio, and warm the cache
    for _t in _s.turns:
        if _t.audio:
            AUDIO.load(_t.audio)

SEM = BoundedSemaphore(CFG.max_sessions_per_process) if CFG.max_sessions_per_process > 0 else None
METRICS: MetricsSink = None  # type: ignore[assignment]
QUOTA_CONCURRENCY = int(os.environ.get("CXAS_QUOTA_CONCURRENT_STREAMS", "15"))
FATAL_CODES = {"UNAUTHENTICATED", "PERMISSION_DENIED", "NOT_FOUND"}
_quota_errors = 0


@events.init.add_listener
def _on_init(environment, **_kw):
    global METRICS
    METRICS = MetricsSink(CFG, environment.events.request)
    log.info("CXAS harness: app=%s endpoint=%s scenarios=%s audio=%s/%sHz chunk=%sms",
             CFG.app_name, CFG.api_endpoint, [s.name for s in SCENARIOS],
             CFG.input_encoding, CFG.input_sample_rate, CFG.chunk_ms)


@events.test_start.add_listener
def _on_test_start(environment, **_kw):
    target = getattr(environment.parsed_options, "num_users", None) if environment.parsed_options else None
    if target and target > QUOTA_CONCURRENCY:
        log.warning("Requested %s concurrent calls but the voice-stream quota is %s "
                    "(CXAS_QUOTA_CONCURRENT_STREAMS). Expect RESOURCE_EXHAUSTED unless the "
                    "quota was raised.", target, QUOTA_CONCURRENCY)


@events.quitting.add_listener
def _on_quit(**_kw):
    if METRICS:
        METRICS.close()


class CxasVoiceUser(User):
    wait_time = between(float(os.environ.get("CXAS_CALL_GAP_MIN_S", "1")),
                        float(os.environ.get("CXAS_CALL_GAP_MAX_S", "3")))

    def on_start(self):
        self.client = ClientPool.get(CFG)
        self._backoff = CFG.backoff_initial_s

    @task
    def place_call(self):
        global _quota_errors
        scenario = pick(SCENARIOS)
        session_id = f"{CFG.session_prefix}-{uuid.uuid4().hex[:20]}"
        if SEM is not None:
            SEM.acquire()
        try:
            result = VoiceCall(CFG, self.client, AUDIO, scenario, session_id).run()
        finally:
            if SEM is not None:
                SEM.release()
        METRICS.record_call(result)

        code = result.error_code
        if code == "RESOURCE_EXHAUSTED":
            _quota_errors += 1
            if CFG.stop_after_quota_errors and _quota_errors >= CFG.stop_after_quota_errors:
                log.error("Stopping: %s RESOURCE_EXHAUSTED errors (CXAS_STOP_AFTER_QUOTA_ERRORS)",
                          _quota_errors)
                self.environment.runner.quit()
                return
            # Exponential backoff with full jitter so callers don't retry in lockstep.
            delay = random.uniform(0, self._backoff)
            self._backoff = min(self._backoff * 2, CFG.backoff_max_s)
            gevent.sleep(delay)
        elif code in FATAL_CODES:
            log.error("Fatal error %s (%s): check CXAS_PROJECT/LOCATION/APP and credentials. "
                      "Stopping the test.", code, result.error)
            self.environment.runner.quit()
        else:
            self._backoff = CFG.backoff_initial_s


if os.environ.get("CXAS_STEPS"):
    from cxas_loadtest import shapes as _shapes

    class CallStepShape(_shapes.StepLoadShape):
        """Active only when CXAS_STEPS is set."""
