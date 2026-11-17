"""Create placeholder 'speech' WAVs (modulated noise) for every audio file a
scenario references, so the harness can be smoke-tested against the mock
server before real recordings / TTS exist. NOT for use against a real agent:
ASR will hear noise. Use tools/tts_generate.py for real utterances.

    python tools/make_synthetic_audio.py scenarios/example.yaml --audio-dir audio
"""

import argparse
import os
import wave

import numpy as np
import yaml


def synth(path: str, seconds: float, rate: int = 8000):
    n = int(seconds * rate)
    t = np.arange(n) / rate
    rng = np.random.default_rng(abs(hash(path)) % 2**32)
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * t)  # ~syllable rate
    signal = rng.normal(0, 1, n) * envelope + 0.6 * np.sin(2 * np.pi * 180 * t)
    pcm = np.clip(signal / np.max(np.abs(signal)) * 9000, -32768, 32767).astype(np.int16)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios")
    ap.add_argument("--audio-dir", default="audio")
    ap.add_argument("--rate", type=int, default=8000)
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args()
    doc = yaml.safe_load(open(a.scenarios))
    for sc in doc.get("scenarios", []):
        for turn in sc.get("turns", []):
            if not turn.get("audio"):
                continue
            path = os.path.join(a.audio_dir, turn["audio"])
            if os.path.exists(path) and not a.overwrite:
                continue
            words = len((turn.get("text") or "a b c d e").split())
            synth(path, max(0.8, words * 0.35), a.rate)
            print("wrote", path)


if __name__ == "__main__":
    main()
