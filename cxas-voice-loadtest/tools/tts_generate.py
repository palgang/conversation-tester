"""Generate caller utterances with Google Cloud Text-to-Speech.

For every turn with `text` and `audio`, synthesises the text and writes a
mono 16-bit PCM WAV at the telephony rate (default 8 kHz), so the harness
streams it without resampling. Voices are rotated to add speaker variety.

    pip install google-cloud-texttospeech
    python tools/tts_generate.py scenarios/example.yaml --audio-dir audio \
        --language en-US --voices en-US-Neural2-A,en-US-Neural2-D,en-US-Neural2-F

Optionally mix in line noise for realism with --noise-db (e.g. -35).
"""

import argparse
import io
import itertools
import os
import wave

import numpy as np
import yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios")
    ap.add_argument("--audio-dir", default="audio")
    ap.add_argument("--language", default="en-US")
    ap.add_argument("--voices", default="", help="comma-separated voice names; empty = default voice")
    ap.add_argument("--rate", type=int, default=8000)
    ap.add_argument("--speaking-rate", type=float, default=1.0)
    ap.add_argument("--noise-db", type=float, default=None, help="add white noise at this dBFS")
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args()

    from google.cloud import texttospeech as tts  # imported lazily: optional dependency

    client = tts.TextToSpeechClient()
    voices = itertools.cycle([v.strip() for v in a.voices.split(",") if v.strip()] or [None])
    doc = yaml.safe_load(open(a.scenarios))
    for sc in doc.get("scenarios", []):
        for turn in sc.get("turns", []):
            if not (turn.get("audio") and turn.get("text")):
                continue
            path = os.path.join(a.audio_dir, turn["audio"])
            if os.path.exists(path) and not a.overwrite:
                print("skip (exists)", path)
                continue
            voice_name = next(voices)
            voice = tts.VoiceSelectionParams(language_code=a.language, name=voice_name or None)
            cfg = tts.AudioConfig(audio_encoding=tts.AudioEncoding.LINEAR16,
                                  sample_rate_hertz=a.rate, speaking_rate=a.speaking_rate,
                                  effects_profile_id=["telephony-class-application"])
            resp = client.synthesize_speech(input=tts.SynthesisInput(text=turn["text"]),
                                            voice=voice, audio_config=cfg)
            with wave.open(io.BytesIO(resp.audio_content)) as w:  # response is a WAV
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
            if a.noise_db is not None:
                pcm += np.random.normal(0, 32767 * 10 ** (a.noise_db / 20), len(pcm))
            pcm = np.clip(pcm, -32768, 32767).astype(np.int16)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(a.rate)
                w.writeframes(pcm.tobytes())
            print(f"wrote {path} ({voice_name or 'default voice'})")


if __name__ == "__main__":
    main()
