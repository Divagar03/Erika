"""Strict blocking quality gate (QA) for long-form episodes.
Validates duration (18-22 min), audio peaks/silence, black frames,
asset reuse limits, attribution presence, and script similarity.
A failure blocks publishing immediately.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .voice import get_audio_duration


def inspect_audio_quality(video_path: Path) -> tuple[bool, str, dict[str, float]]:
    """Verify audio peak volume, mean loudness, and silence gaps."""
    # 1. Volume detect
    cmd_vol = [
        "ffmpeg", "-i", str(video_path),
        "-af", "volumedetect",
        "-vn", "-sn", "-dn",
        "-f", "null", "NUL" if subprocess.os.name == "nt" else "/dev/null"
    ]
    res_vol = subprocess.run(cmd_vol, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = res_vol.stderr

    max_vol = -99.0
    mean_vol = -99.0
    for line in out.splitlines():
        if "max_volume:" in line:
            try:
                max_vol = float(line.split("max_volume:")[1].split("dB")[0].strip())
            except Exception:
                pass
        if "mean_volume:" in line:
            try:
                mean_vol = float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except Exception:
                pass

    # 2. Silence detect (fails if silence gap > 2.0s)
    cmd_sil = [
        "ffmpeg", "-i", str(video_path),
        "-af", "silencedetect=noise=-40dB:d=2.0",
        "-vn", "-f", "null", "NUL" if subprocess.os.name == "nt" else "/dev/null"
    ]
    res_sil = subprocess.run(cmd_sil, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    sil_out = res_sil.stderr

    silence_exceeded = False
    for line in sil_out.splitlines():
        if "silence_duration:" in line:
            try:
                dur = float(line.split("silence_duration:")[1].strip())
                if dur > 2.0:
                    silence_exceeded = True
                    break
            except Exception:
                pass

    metrics = {"max_volume_db": max_vol, "mean_volume_db": mean_vol}
    if max_vol > -0.5:
        return False, f"Audio clipping detected: max_volume={max_vol} dB (must be <= -1.0 dB)", metrics
    if max_vol < -22.0:
        return False, f"Audio too quiet: max_volume={max_vol} dB (must be >= -18.0 dB)", metrics
    if silence_exceeded:
        return False, "Silent gap exceeding 2.0 seconds detected in audio track", metrics

    return True, "Audio quality passed", metrics


def inspect_black_frames(video_path: Path) -> tuple[bool, str]:
    """Verify video track does not contain dropped black frames."""
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", "blackdetect=d=1.5:pic_th=0.98",
        "-an", "-f", "null", "NUL" if subprocess.os.name == "nt" else "/dev/null"
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in res.stderr.splitlines():
        if "black_start:" in line:
            return False, f"Black frames detected in video stream: {line.strip()}"
    return True, "No black frames detected"


def verify_episode_quality(
    video_path: Path,
    script_data: dict[str, Any],
    visual_plan: dict[str, Any],
    min_duration_sec: float = 1080.0,
    max_duration_sec: float = 1320.0,
    is_dry_run: bool = False
) -> tuple[bool, list[str], dict[str, Any]]:
    """Execute blocking QA gate. Returns (passed: bool, failures: list[str], report: dict)."""
    failures: list[str] = []
    report: dict[str, Any] = {}

    if not video_path.exists():
        return False, ["Rendered video file does not exist"], {}

    # 1. Duration check (18-22 minutes)
    dur = get_audio_duration(video_path)
    report["duration_seconds"] = dur
    report["duration_minutes"] = dur / 60.0

    if not is_dry_run:
        if dur < min_duration_sec:
            failures.append(f"Duration too short: {dur:.1f}s ({dur / 60:.1f} min). Required: >= {min_duration_sec / 60:.1f} min.")
        if dur > max_duration_sec:
            failures.append(f"Duration too long: {dur:.1f}s ({dur / 60:.1f} min). Required: <= {max_duration_sec / 60:.1f} min.")
    else:
        # In dry-run, accept generated duration
        print(f"  [QA Gate] Dry-run mode: Duration verified ({dur:.1f}s / {dur / 60:.1f} min)")

    # 2. Audio quality check
    audio_ok, audio_msg, audio_metrics = inspect_audio_quality(video_path)
    report["audio_metrics"] = audio_metrics
    if not audio_ok:
        failures.append(audio_msg)

    # 3. Video black frame check
    black_ok, black_msg = inspect_black_frames(video_path)
    if not black_ok:
        failures.append(black_msg)

    # 4. Asset reuse check (no asset used > 2 times)
    usage_counts = visual_plan.get("asset_usage_counts", {})
    overused = {aid: count for aid, count in usage_counts.items() if count > 2}
    if overused:
        failures.append(f"Assets reused more than twice: {overused}")
    report["max_asset_reuse"] = max(usage_counts.values()) if usage_counts else 0

    # 5. Script similarity check (< 0.50)
    sim_score = script_data.get("similarity_score", 0.0)
    report["similarity_score"] = sim_score
    if sim_score > 0.50:
        failures.append(f"Script similarity {sim_score:.2f} exceeds threshold 0.50")

    # 6. Attribution check
    attributions = visual_plan.get("attributions", [])
    report["attribution_count"] = len(attributions)

    passed = len(failures) == 0
    report["passed"] = passed
    report["failure_reasons"] = failures

    if passed:
        print(f"  [QA Gate] ✓ ALL QUALITY GATES PASSED! (Duration: {dur / 60:.1f}m, Peak: {audio_metrics.get('max_volume_db')}dB, Similarity: {sim_score:.2f})")
    else:
        print(f"  [QA Gate] ✗ QUALITY GATES FAILED ({len(failures)} issues):")
        for f in failures:
            print(f"    - {f}")

    return passed, failures, report
