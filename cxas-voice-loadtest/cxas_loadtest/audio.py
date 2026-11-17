"""Audio helpers: WAV loading, resampling, G.711 codecs and chunking.

Uses numpy only (``audioop`` was removed in Python 3.13). If scipy is
installed, resampling uses a proper polyphase filter; otherwise it falls back
to linear interpolation, which is fine for load generation but not ideal for
ASR accuracy. Best practice: generate utterances directly at the target rate
(see ``tools/tts_generate.py``).
"""

from __future__ import annotations

import functools
import io
import logging
import struct
import wave
from dataclasses import dataclass
from math import gcd
from typing import List

import numpy as np

log = logging.getLogger(__name__)

BYTES_PER_SAMPLE = {"LINEAR16": 2, "MULAW": 1, "ALAW": 1}


# ---------------------------------------------------------------------------
# G.711 codecs (lookup-table based)
# ---------------------------------------------------------------------------
def _ulaw_decode_table() -> np.ndarray:
    out = np.zeros(256, dtype=np.int16)
    for code in range(256):
        u = ~code & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        mag = (((mantissa << 3) + 0x84) << exponent) - 0x84
        out[code] = -mag if sign else mag
    return out


def _alaw_decode_table() -> np.ndarray:
    out = np.zeros(256, dtype=np.int16)
    for code in range(256):
        a = code ^ 0x55
        sign = a & 0x80
        exponent = (a >> 4) & 0x07
        mantissa = a & 0x0F
        if exponent == 0:
            mag = (mantissa << 4) + 8
        else:
            mag = ((mantissa << 4) + 0x108) << (exponent - 1)
        out[code] = mag if sign else -mag
    return out


@functools.lru_cache(maxsize=None)
def _tables(law: str):
    decode = _ulaw_decode_table() if law == "MULAW" else _alaw_decode_table()
    # Build a 65536-entry encode table by nearest-neighbour search on the
    # decoded values. Equivalent to the reference G.711 quantiser for our use.
    order = np.argsort(decode.astype(np.int32), kind="stable")
    sorted_vals = decode[order].astype(np.int32)
    allvals = np.arange(-32768, 32768, dtype=np.int32)
    idx = np.clip(np.searchsorted(sorted_vals, allvals), 1, 255)
    left, right = sorted_vals[idx - 1], sorted_vals[idx]
    choose_left = (allvals - left) <= (right - allvals)
    nearest = np.where(choose_left, idx - 1, idx)
    encode = order[nearest].astype(np.uint8)
    return encode, decode


def encode_pcm16(samples: np.ndarray, encoding: str) -> bytes:
    samples = samples.astype(np.int16, copy=False)
    if encoding == "LINEAR16":
        return samples.astype("<i2").tobytes()
    encode, _ = _tables(encoding)
    return encode[samples.astype(np.int32) + 32768].tobytes()


def decode_to_pcm16(data: bytes, encoding: str) -> np.ndarray:
    if encoding == "LINEAR16":
        return np.frombuffer(data, dtype="<i2")
    _, decode = _tables(encoding)
    return decode[np.frombuffer(data, dtype=np.uint8)]


def silence_bytes(encoding: str, n_samples: int) -> bytes:
    return encode_pcm16(np.zeros(n_samples, dtype=np.int16), encoding)


def duration_ms(n_bytes: int, encoding: str, sample_rate: int) -> float:
    return 1000.0 * n_bytes / (BYTES_PER_SAMPLE[encoding] * sample_rate)


# ---------------------------------------------------------------------------
# WAV loading / resampling
# ---------------------------------------------------------------------------
def _read_wav_any(path: str):
    """Return (pcm16 mono samples, sample_rate). Handles PCM and G.711 WAVs."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        with wave.open(io.BytesIO(raw)) as w:
            rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.readframes(w.getnframes())
        if width == 2:
            pcm = np.frombuffer(frames, dtype="<i2")
        elif width == 1:
            pcm = ((np.frombuffer(frames, dtype=np.uint8).astype(np.int16) - 128) << 8)
        elif width == 4:
            pcm = (np.frombuffer(frames, dtype="<i4") >> 16).astype(np.int16)
        else:
            raise ValueError(f"Unsupported sample width {width} in {path}")
    except wave.Error:
        # The stdlib wave module rejects G.711 (format 6/7) WAVs - parse manually.
        pcm, rate, channels = _read_g711_wav(raw, path)
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return pcm.astype(np.int16), rate


def _read_g711_wav(raw: bytes, path: str):
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"{path} is not a WAV file")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid, size = raw[pos:pos + 4], struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError(f"{path}: malformed WAV")
    tag, channels, rate = fmt[0], fmt[1], fmt[2]
    law = {6: "ALAW", 7: "MULAW"}.get(tag)
    if law is None:
        raise ValueError(f"{path}: unsupported WAV format tag {tag}")
    return decode_to_pcm16(data, law), rate, channels


def resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or len(pcm) == 0:
        return pcm
    try:
        from scipy.signal import resample_poly  # type: ignore

        g = gcd(src_rate, dst_rate)
        out = resample_poly(pcm.astype(np.float64), dst_rate // g, src_rate // g)
    except ImportError:
        log.warning("scipy not installed: using linear-interpolation resampling %s->%s Hz",
                    src_rate, dst_rate)
        n_out = int(round(len(pcm) * dst_rate / src_rate))
        x_old = np.arange(len(pcm)) / src_rate
        x_new = np.arange(n_out) / dst_rate
        out = np.interp(x_new, x_old, pcm.astype(np.float64))
    return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def trim(pcm: np.ndarray, sample_rate: int, threshold: int = 600, margin_ms: int = 30) -> np.ndarray:
    """Strip leading/trailing silence so 'end of speech' timestamps are exact.

    Latency is measured from the moment the last utterance chunk is sent, so
    trailing silence baked into a WAV would otherwise be counted as agent time.
    """
    loud = np.flatnonzero(np.abs(pcm.astype(np.int32)) > threshold)
    if len(loud) == 0:
        return pcm
    margin = int(sample_rate * margin_ms / 1000)
    return pcm[max(0, loud[0] - margin): min(len(pcm), loud[-1] + margin)]


@dataclass(frozen=True)
class Utterance:
    path: str
    chunks: List[bytes]
    duration_ms: float


class AudioLibrary:
    """Loads and caches utterances pre-encoded and pre-chunked for streaming."""

    def __init__(self, encoding: str, sample_rate: int, chunk_ms: int, trim_silence: bool = True):
        self.trim_silence = trim_silence
        self.encoding = encoding
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.samples_per_chunk = int(sample_rate * chunk_ms / 1000)
        self.silence_chunk = silence_bytes(encoding, self.samples_per_chunk)
        self._cache: dict = {}

    def load(self, path: str) -> Utterance:
        utt = self._cache.get(path)
        if utt is None:
            pcm, rate = _read_wav_any(path)
            pcm = resample(pcm, rate, self.sample_rate)
            if self.trim_silence:
                pcm = trim(pcm, self.sample_rate)
            # pad to a whole number of chunks so every message has equal duration
            rem = len(pcm) % self.samples_per_chunk
            if rem:
                pcm = np.concatenate([pcm, np.zeros(self.samples_per_chunk - rem, dtype=np.int16)])
            encoded = encode_pcm16(pcm, self.encoding)
            step = self.samples_per_chunk * BYTES_PER_SAMPLE[self.encoding]
            chunks = [encoded[i:i + step] for i in range(0, len(encoded), step)]
            utt = Utterance(path, chunks, len(chunks) * self.chunk_ms)
            self._cache[path] = utt
        return utt
