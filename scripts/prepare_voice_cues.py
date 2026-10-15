"""Build trimmed PCM playback copies; keep the source MP3 recordings untouched.

Run with: .venv/bin/python scripts/prepare_voice_cues.py
"""
from pathlib import Path
import numpy as np
import soundfile as sf

root = Path(__file__).resolve().parents[1] / 'assets' / 'melanie11'
target = root / 'playback'
target.mkdir(exist_ok=True)
for name in ('ya', 'uhhuh', 'yes', 'hey', 'let-me-see', 'let-me-think',
             'one-moment', 'on-it', 'okay', 'sure', 'say-that-again',
             'i-lost-the-connection'):
    audio, rate = sf.read(root / f'{name}.mp3', always_2d=True)
    mono = audio.mean(axis=1)
    active = np.flatnonzero(np.abs(mono) > max(0.001, np.max(np.abs(mono)) * 0.006))
    if len(active):
        audio = audio[max(0, active[0] - int(.04 * rate)):
                      min(len(audio), active[-1] + int(.09 * rate))]
    sf.write(target / f'{name}.wav', audio, rate, subtype='PCM_16')
    print(f'{name}: {len(audio) / rate:.2f}s')
