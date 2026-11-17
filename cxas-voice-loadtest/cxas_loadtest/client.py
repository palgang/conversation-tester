"""gRPC client factory for the CXAS SessionService.

Auth uses Application Default Credentials (``gcloud auth application-default
login`` locally, or ``GOOGLE_APPLICATION_CREDENTIALS`` / workload identity on
load-generator VMs). The service account needs permission to run sessions on
the app (e.g. the CES/Agent Studio "client" or "user" role).
"""

from __future__ import annotations

import itertools
import threading
from typing import List, Optional

import grpc
from google.cloud import ces_v1
from google.cloud.ces_v1.services.session_service.transports import SessionServiceGrpcTransport

from .config import HarnessConfig

_CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.max_receive_message_length", 32 * 1024 * 1024),
]


def build_client(cfg: HarnessConfig) -> ces_v1.SessionServiceClient:
    if cfg.insecure:
        channel = grpc.insecure_channel(cfg.api_endpoint, options=_CHANNEL_OPTIONS)
        return ces_v1.SessionServiceClient(transport=SessionServiceGrpcTransport(channel=channel))

    host = cfg.api_endpoint if ":" in cfg.api_endpoint else f"{cfg.api_endpoint}:443"
    channel = SessionServiceGrpcTransport.create_channel(
        host=host,
        quota_project_id=cfg.quota_project,
        options=_CHANNEL_OPTIONS,
    )
    return ces_v1.SessionServiceClient(
        transport=SessionServiceGrpcTransport(host=host, channel=channel),
    )


class ClientPool:
    """Process-wide pool. gRPC multiplexes many bidi streams over one HTTP/2
    connection; use CXAS_CHANNELS_PER_PROCESS > 1 if you run >~80 concurrent
    calls per worker (HTTP/2 max-concurrent-streams is usually 100)."""

    _lock = threading.Lock()
    _clients: List[ces_v1.SessionServiceClient] = []
    _cycle: Optional[itertools.cycle] = None

    @classmethod
    def get(cls, cfg: HarnessConfig) -> ces_v1.SessionServiceClient:
        if not cfg.share_channel:
            return build_client(cfg)
        with cls._lock:
            if cls._cycle is None:
                cls._clients = [build_client(cfg) for _ in range(max(1, cfg.channels_per_process))]
                cls._cycle = itertools.cycle(cls._clients)
            return next(cls._cycle)
