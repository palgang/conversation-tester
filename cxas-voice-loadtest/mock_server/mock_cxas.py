"""Local mock of CXAS SessionService.BidiRunSession, for harness smoke tests.

It speaks the real wire protocol (google.cloud.ces.v1 protos), does a crude
energy-based endpointing on the incoming audio, and answers each caller turn
with a recognition result, a text output, a tone as "agent audio" and
turn_completed. It also enforces a concurrent-stream limit so you can see
how the harness reports RESOURCE_EXHAUSTED.

    python mock_server/mock_cxas.py --port 50051 --max-streams 15
    CXAS_API_ENDPOINT=localhost:50051 CXAS_INSECURE=true CXAS_PROJECT=p \
      CXAS_LOCATION=l CXAS_APP=a locust -f locustfile.py ...

This mock is NOT a performance model of CXAS; it only exercises the harness.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import random
import sys
import threading
import time
from concurrent import futures

import grpc
import numpy as np
from google.cloud import ces_v1

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cxas_loadtest.audio import decode_to_pcm16, encode_pcm16  # noqa: E402

log = logging.getLogger("mock_cxas")
METHOD = "/google.cloud.ces.v1.SessionService/BidiRunSession"


class Settings:
    max_streams = 15
    latency_ms = 900.0
    endpoint_silence_ms = 500
    response_audio_ms = 2500
    error_rate = 0.0
    speech_rms = 400.0


class MockService:
    def __init__(self, s: Settings):
        self.s = s
        self.active = 0
        self.lock = threading.Lock()
        self.total = 0

    def bidi(self, request_iterator, context):
        with self.lock:
            if self.active >= self.s.max_streams:
                context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED,
                              "Quota exceeded for concurrent voice sessions (mock)")
            self.active += 1
            self.total += 1
        try:
            yield from self._session(request_iterator, context)
        finally:
            with self.lock:
                self.active -= 1

    def _session(self, request_iterator, context):
        out: "queue.Queue" = queue.Queue()
        state = {"enc": "MULAW", "rate": 8000, "out_enc": "MULAW", "out_rate": 8000}
        S = ces_v1.BidiSessionServerMessage
        turn_counter = [0]

        def respond(transcript: str):
            turn_counter[0] += 1
            n = turn_counter[0]
            delay = random.lognormvariate(np.log(self.s.latency_ms / 1000.0), 0.35)
            if transcript:
                out.put((0.0, S(recognition_result=ces_v1.RecognitionResult(transcript=transcript))))
            if random.random() < self.s.error_rate:
                out.put((delay, "ERROR"))
                return
            out.put((delay, S(session_output=ces_v1.SessionOutput(
                text=f"Mock agent response {n}.", turn_index=n))))
            # tone as agent audio, streamed in 100ms chunks faster than real time
            rate = state["out_rate"]
            t = np.arange(int(rate * self.s.response_audio_ms / 1000)) / rate
            pcm = (6000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
            data = encode_pcm16(pcm, state["out_enc"])
            step = len(data) // max(1, self.s.response_audio_ms // 100)
            for i in range(0, len(data), step):
                out.put((0.02, S(session_output=ces_v1.SessionOutput(
                    audio=data[i:i + step], turn_index=n))))
            out.put((0.01, S(session_output=ces_v1.SessionOutput(turn_index=n, turn_completed=True))))

        def reader():
            speaking, silence_ms, speech_ms = False, 0.0, 0.0
            try:
                for req in request_iterator:
                    which = ces_v1.BidiSessionClientMessage.pb(req).WhichOneof("message_type")
                    if which == "config":
                        c = req.config
                        if not c.session:
                            out.put((0, "INVALID"))
                            return
                        state["enc"] = c.input_audio_config.audio_encoding.name
                        state["rate"] = c.input_audio_config.sample_rate_hertz or 8000
                        state["out_enc"] = c.output_audio_config.audio_encoding.name
                        state["out_rate"] = c.output_audio_config.sample_rate_hertz or 8000
                        respond("")  # greeting
                        continue
                    inp = req.realtime_input
                    kind = ces_v1.SessionInput.pb(inp).WhichOneof("input_type")
                    if kind == "dtmf":
                        respond(f"<dtmf {inp.dtmf}>")
                    elif kind == "text":
                        respond(inp.text)
                    elif kind == "audio":
                        pcm = decode_to_pcm16(inp.audio, state["enc"])
                        ms = 1000.0 * len(pcm) / state["rate"]
                        rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))) if len(pcm) else 0
                        if rms > self.s.speech_rms:
                            if not speaking:
                                speaking, speech_ms = True, 0.0
                            silence_ms = 0.0
                            speech_ms += ms
                            if int(speech_ms) % 300 < ms:
                                out.put((0.0, S(recognition_result=ces_v1.RecognitionResult(
                                    transcript=f"<speech {speech_ms / 1000:.1f}s>"))))
                        elif speaking:
                            silence_ms += ms
                            if silence_ms >= self.s.endpoint_silence_ms:
                                speaking = False
                                respond(f"<utterance {speech_ms / 1000:.1f}s>")
            except Exception:  # noqa: BLE001 - client went away
                pass
            finally:
                out.put((0, None))

        threading.Thread(target=reader, daemon=True).start()
        while True:
            delay, msg = out.get()
            if delay:
                time.sleep(delay)
            if msg is None:
                return
            if msg == "ERROR":
                context.abort(grpc.StatusCode.INTERNAL, "mock injected error")
            if msg == "INVALID":
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "first message must carry config.session")
            if not context.is_active():
                return
            yield msg


def serve(port: int, s: Settings):
    svc = MockService(s)
    handler = grpc.method_handlers_generic_handler("google.cloud.ces.v1.SessionService", {
        "BidiRunSession": grpc.stream_stream_rpc_method_handler(
            svc.bidi,
            request_deserializer=ces_v1.BidiSessionClientMessage.deserialize,
            response_serializer=ces_v1.BidiSessionServerMessage.serialize,
        )
    })
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max(64, s.max_streams * 3)))
    server.add_generic_rpc_handlers((handler,))
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    log.info("mock CXAS listening on :%s (max_streams=%s latency=%sms)", port, s.max_streams, s.latency_ms)
    try:
        while True:
            time.sleep(10)
            log.info("active=%s total=%s", svc.active, svc.total)
    except KeyboardInterrupt:
        server.stop(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=50051)
    ap.add_argument("--max-streams", type=int, default=Settings.max_streams)
    ap.add_argument("--latency-ms", type=float, default=Settings.latency_ms)
    ap.add_argument("--endpoint-silence-ms", type=int, default=Settings.endpoint_silence_ms)
    ap.add_argument("--response-audio-ms", type=int, default=Settings.response_audio_ms)
    ap.add_argument("--error-rate", type=float, default=Settings.error_rate)
    a = ap.parse_args()
    st = Settings()
    st.max_streams, st.latency_ms = a.max_streams, a.latency_ms
    st.endpoint_silence_ms, st.response_audio_ms, st.error_rate = (
        a.endpoint_silence_ms, a.response_audio_ms, a.error_rate)
    serve(a.port, st)
