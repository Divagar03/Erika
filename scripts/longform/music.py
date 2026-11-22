"""Background music selector and automated audio ducker for long-form episodes.
Loops royalty-free ambient synth tracks and ducks music levels under speech.
"""

from __future__ import annotations

import random
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
MUSIC_DIR = ROOT / "assets" / "music"
MUSIC_CACHE_DIR = ROOT / "data" / "longform_music"


def prepare_background_music(total_duration_sec: float, episode_id: str) -> Path:
    """Prepare a looped background soundtrack matching total_duration_sec."""
    MUSIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_music_path = MUSIC_CACHE_DIR / f"{episode_id}_bg_music.wav"

    available_tracks = []
    if MUSIC_DIR.exists():
        for f in MUSIC_DIR.glob("*.mp3"):
            # Prefer ambient synth tracks for long documentaries
            if "synth" in f.name.lower() or "district" in f.name.lower() or "night" in f.name.lower():
                available_tracks.append(f)
        if not available_tracks:
            available_tracks = list(MUSIC_DIR.glob("*.mp3"))

    if available_tracks:
        chosen_track = random.choice(available_tracks)
        print(f"  [Music] Selected royalty-free soundtrack: {chosen_track.name}")
        # Loop track to total_duration_sec with smooth 3s fadeout at the end
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", str(chosen_track),
            "-t", str(total_duration_sec),
            "-af", f"volume=0.12,afade=t=out:st={max(0, total_duration_sec - 4)}:d=4",
            "-ar", "48000", "-ac", "2",
            str(out_music_path)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        # Procedural cinematic low-frequency ambient drone via FFmpeg sine generator
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=65:duration={total_duration_sec}",
            "-af", f"volume=0.03,afade=t=out:st={max(0, total_duration_sec - 4)}:d=4",
            "-ar", "48000", "-ac", "2",
            str(out_music_path)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    return out_music_path
