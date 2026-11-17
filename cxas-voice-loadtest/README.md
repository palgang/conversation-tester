# CXAS voice load harness (Locust + BidiRunSession)

API-level load generator for a Google **CX Agent Studio (CXAS)** agent whose
main channel is phone voice. Each Locust user is a simulated caller: it opens a
`SessionService.BidiRunSession` gRPC stream, streams pre-recorded or
TTS-generated caller utterances **in real time** (8 kHz μ-law by default, like
a phone line), listens to the agent's audio, and follows a scripted
conversation. Per-turn latencies are reported as Locust request events.

What it does and does not cover:

* **Covered:** CXAS speech recognition, endpointing, LLM/agent runtime, tools/
  webhooks, TTS, quotas and session concurrency, all driven through the public API.
* **Not covered:** the telephony leg (SIP/PSTN, carrier, CCaaS/IVR media
  gateway). Test that separately with a small number of real calls.

## Layout

```
locustfile.py                 Locust user (one user = one concurrent call, calls back to back)
cxas_loadtest/
  config.py                   all CXAS_* env settings
  voice_session.py            one call: bidi stream, real-time audio sender, receiver, turn timing
  metrics.py                  Locust events + optional per-turn JSONL
  audio.py                    WAV loading, resampling, μ-law/A-law codecs, chunking
  scenarios.py                YAML call scripts
  shapes.py                   stepped concurrency ramp (CXAS_STEPS)
  run_once.py                 single debug call without Locust
scenarios/example.yaml        example call scripts (replace with your caller journeys)
tools/tts_generate.py         make caller WAVs from scenario text with Cloud TTS
tools/make_synthetic_audio.py placeholder noise "speech" for mock testing only
mock_server/mock_cxas.py      local fake BidiRunSession server (real protos), for smoke tests
```

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # fill in CXAS_PROJECT / CXAS_LOCATION / CXAS_APP
gcloud auth application-default login   # or GOOGLE_APPLICATION_CREDENTIALS=sa.json
```

The identity needs permission to run sessions on the CXAS app. Use a
dedicated service account for load tests so its traffic is easy to find in logs.
Confirm the API endpoint for your region. `ces.googleapis.com` is the library
default; set `CXAS_API_ENDPOINT` if your app is served from a regional endpoint.

## 1. Prepare caller audio

Write the scenarios (`scenarios/*.yaml`): each turn has `audio` (WAV path),
optional `text` (what the caller says), or `dtmf`. Then synthesize:

```bash
pip install google-cloud-texttospeech
python tools/tts_generate.py scenarios/example.yaml --audio-dir audio \
    --voices en-US-Neural2-A,en-US-Neural2-D,en-US-Neural2-F --noise-db -40
```

Real recordings also work. Any PCM or G.711 WAV is accepted and converted to
the configured input encoding and rate. Leading and trailing silence is trimmed
(`CXAS_TRIM_SILENCE`) so that "end of speech" is exact.

## 2. Validate one call against the real agent

```bash
python -m cxas_loadtest.run_once --scenario check_order_status
```

The command prints, per turn, the ASR transcript, the agent's text, and the
timings. Check that the transcripts make sense and that the agent follows the
script before you add load. If the agent does not greet first, set
`CXAS_EXPECT_GREETING=false`.

## 3. Run load

```bash
# fixed concurrency: 5 concurrent calls for 10 minutes
locust -f locustfile.py --headless -u 5 -r 1 -t 10m --csv results/c5 --html results/c5.html

# stepped ramp (overrides -u/-r/-t): 5 -> 10 -> 15 concurrent calls
CXAS_STEPS="5:300,10:300,15:600" locust -f locustfile.py --headless --csv results/step

# distributed (audio files + .env must exist on every worker)
locust -f locustfile.py --master --headless -u 60 -r 2 -t 30m --expect-workers 4 --csv results/big
locust -f locustfile.py --worker --master-host <master-ip>      # on each worker
```

**Concurrency is the load knob.** One user is one live call, so `-u` maps
directly to the "concurrent voice streams" quota (15 per project and region by
default). Calls per hour ≈ users × 3600 / (call duration + call gap). Raise the
quota before you run more than 15 users, or the overflow will be reported as
`RESOURCE_EXHAUSTED`. Also watch the LLM tokens/min quota, which multi-turn
calls consume quickly.

A single worker process kept 80 concurrent calls at 40 ms chunks with under
30 ms of send lag against the mock. Check `audio_send_lag_max` on real runs.

## Metrics (Locust rows)

| Type | Name | Meaning |
|---|---|---|
| VOICE | `turn.ttfa` | **End of caller speech → first agent audio byte.** This is the silence the caller hears. It includes endpointing, ASR finalisation, LLM, tools and TTS first byte. Primary SLI. |
| VOICE | `turn.complete` | End of caller speech → `turn_completed` (all agent output for the turn delivered). |
| VOICE | `turn.asr_first_partial` | Start of caller speech → first `RecognitionResult`. Shows whether ASR is keeping up. |
| VOICE | `greeting.ttfa` / `greeting.complete` | Session open → first greeting audio / greeting completed. Includes connection setup. |
| VOICE | `turn.failed` / `greeting.failed` | Failure rows. The error text is the code: `TURN_TIMEOUT`, `GREETING_TIMEOUT`, `NO_AGENT_AUDIO`, `STREAM_CLOSED`, `CONTENT_CHECK ...`, `PREMATURE_ENDPOINT ...` (the agent answered while the caller was still talking). |
| CALL | `session.start` | Stream open → first server message, for accepted sessions. |
| CALL | `session.rejected` | Session failed before any server message. The error is the gRPC code, e.g. **`RESOURCE_EXHAUSTED`** (quota), `UNAVAILABLE`, `PERMISSION_DENIED`. |
| CALL | `call <scenario>` | Whole-call duration. A failure means the call broke mid-way, with the gRPC or harness code. |
| HARNESS | `audio_send_lag_max` | Worst lateness of an outgoing audio chunk in the call. It fails above `CXAS_SEND_LAG_WARN_MS`. If this fails, the load generator is saturated and the latencies are not trustworthy, so add workers. |

Failures are recorded on their own rows so that 0 ms failures never distort the
latency percentiles. Ignore Locust's "Aggregated" row, because it mixes unrelated
timings. With `CXAS_PER_TURN_STATS=true`, every row is also broken down by
`[scenario/turn]`.

`CXAS_RESULTS_JSONL=results/turns-{pid}.jsonl` writes one record per call with
every turn's raw timings (and transcripts with `CXAS_LOG_TRANSCRIPTS=true`).
Use these files for exact percentiles (Locust's percentiles are bucketed) and to
correlate with Cloud Logging and Monitoring by `session_id`.

### How timing works

* Audio is streamed continuously at real-time pace, with silence between
  utterances, as the API's best practices require. Each chunk is sent at the end
  of its capture window, so "caller stopped speaking" is the moment the last
  utterance chunk is sent.
* The caller waits for the agent's audio to finish "playing" (byte duration,
  starting when the audio arrives) plus `pause_ms` of think time before
  speaking. Barge-in is not simulated. An `InterruptionSignal` stops the
  simulated playback and is counted.

## Quota / error handling

* `RESOURCE_EXHAUSTED`: the session is recorded under `session.rejected`, and that
  caller backs off with exponential full-jitter delays (`CXAS_BACKOFF_INITIAL_S`
  up to `CXAS_BACKOFF_MAX_S`), so callers don't retry in lockstep. A success
  resets the backoff. `CXAS_STOP_AFTER_QUOTA_ERRORS=N` stops the run after N
  quota errors in a worker.
* `UNAUTHENTICATED`, `PERMISSION_DENIED` and `NOT_FOUND` stop the test
  immediately, because they are configuration errors and hammering won't help.
* Streams are never retried mid-call. The harness calls the raw gRPC method
  rather than the GAPIC wrapper, which would otherwise apply a retry policy and
  wait for the first response before returning.
* `GoAway` before the script finishes is counted as a call failure (`GO_AWAY`).
* `CXAS_MAX_SESSIONS_PER_PROCESS` adds a local safety cap per worker.

## Smoke test without GCP

```bash
python tools/make_synthetic_audio.py scenarios/example.yaml --audio-dir audio
python mock_server/mock_cxas.py --port 50051 --max-streams 4 --error-rate 0.02 &
export CXAS_API_ENDPOINT=localhost:50051 CXAS_INSECURE=true CXAS_PROJECT=p CXAS_LOCATION=l CXAS_APP=a
python -m cxas_loadtest.run_once
locust -f locustfile.py --headless -u 6 -r 2 -t 60s     # 6 users > 4 streams -> RESOURCE_EXHAUSTED rows
```

The mock only exercises the harness. Its latencies say nothing about CXAS.
Delete the synthetic WAVs before you generate real ones, because the TTS tool
skips files that already exist unless you pass `--overwrite`.

## Assumptions to verify on the real agent

* Audio format: 8 kHz μ-law in and out matches a phone line. Switch to
  `LINEAR16`/16000 if your telephony integration uses wideband audio.
* Whether the agent returns `text` outputs in voice sessions. Content checks
  (`expect_agent_contains`) are skipped when no text arrives.
* Whether the agent speaks first (`CXAS_EXPECT_GREETING`).
* Session variables (`variables:` in a scenario) are sent right after the
  config, in the way a telephony integration would pass ANI or a call id.
* `CXAS_USE_TOOL_FAKES=true` isolates CXAS from backend APIs. Run with real
  tools as well to find backend bottlenecks.
