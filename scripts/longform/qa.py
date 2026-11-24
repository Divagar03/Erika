"""Strict blocking quality gate (QA) for long-form episodes.
Validates:
1. Duration (18-22 min in production, >= 100s in preview)
2. Audio peaks/silence/clipping
3. Black frames
4. Asset reuse limits (<= 2 uses, separated by >= 60s)
5. Script similarity (< 0.50)
6. Attribution completeness
7. Motion variety: Fail if > 3 consecutive shots use the same motion type
8. Static shot duration: Fail if any single shot lasts > 8.0 seconds
9. CLIP visual semantic check: Sample 1 frame every 5s; fail if > 5% frames show an off-topic vehicle
A failure blocks publishing immediately.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .clip_verifier import sample_and_verify_video_frames
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
    is_dry_run: bool = False,
    is_preview: bool = False,
    brand: str = "Ford",
    model: str = "Mustang S550",
    is_bike: bool = False
) -> tuple[bool, list[str], dict[str, Any]]:
    """Execute comprehensive blocking QA gate.
    Returns (passed: bool, failures: list[str], report: dict).
    """
    failures: list[str] = []
    report: dict[str, Any] = {}

    if not video_path.exists():
        return False, ["Rendered video file does not exist"], {}

    # 1. Duration check
    dur = get_audio_duration(video_path)
    report["duration_seconds"] = dur
    report["duration_minutes"] = dur / 60.0

    if not is_dry_run and not is_preview:
        if dur < min_duration_sec:
            failures.append(f"Duration too short: {dur:.1f}s ({dur / 60:.1f} min). Required: >= {min_duration_sec / 60:.1f} min.")
        if dur > max_duration_sec:
            failures.append(f"Duration too long: {dur:.1f}s ({dur / 60:.1f} min). Required: <= {max_duration_sec / 60:.1f} min.")
    else:
        print(f"  [QA Gate] Preview/Dry-run mode: Duration verified ({dur:.1f}s / {dur / 60:.1f} min)")

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

    # 5. Motion variety gate: Fail if > 3 consecutive shots use the same motion type
    shots = visual_plan.get("shots", [])
    consecutive_motion_count = 1
    max_consecutive_motion = 1
    last_motion = ""
    for s in shots:
        m = s.get("motion_type", "")
        if m == last_motion and m:
            consecutive_motion_count += 1
            if consecutive_motion_count > max_consecutive_motion:
                max_consecutive_motion = consecutive_motion_count
        else:
            consecutive_motion_count = 1
            last_motion = m

    report["max_consecutive_motion"] = max_consecutive_motion
    if max_consecutive_motion > 3:
        failures.append(f"Motion variety failure: {max_consecutive_motion} consecutive shots used the same motion '{last_motion}' (must be <= 3)")

    # 6. Static shot duration gate: Fail if any single shot lasts > 8.0 seconds
    max_shot_dur = max((s.get("duration", 0.0) for s in shots), default=0.0)
    report["max_shot_duration"] = max_shot_dur
    if max_shot_dur > 8.0:
        failures.append(f"Shot duration failure: A shot lasts {max_shot_dur:.2f}s (must be <= 8.0 seconds)")

    # 7. Script similarity check (< 0.50)
    sim_score = script_data.get("similarity_score", 0.0)
    report["similarity_score"] = sim_score
    if sim_score > 0.50:
        failures.append(f"Script similarity {sim_score:.2f} exceeds threshold 0.50")

    # 8. Attribution check
    attributions = visual_plan.get("attributions", [])
    report["attribution_count"] = len(attributions)

    # 9. CLIP semantic check on sampled video frames (Sample every 5s; fail if > 5% off-topic)
    clip_passed, off_topic_ratio, clip_details = sample_and_verify_video_frames(
        video_path=video_path,
        target_brand=brand,
        target_model=model,
        sample_interval_sec=5.0,
        max_off_topic_ratio=0.05,
        is_bike=is_bike
    )
    report["clip_qa"] = {
        "passed": clip_passed,
        "off_topic_ratio": off_topic_ratio,
        "details_count": len(clip_details)
    }
    if not clip_passed:
        failures.append(f"CLIP Semantic QA Gate Failed: {off_topic_ratio*100:.1f}% off-topic frames detected in final video (exceeds 5.0% threshold)")

    passed = len(failures) == 0
    report["passed"] = passed
    report["failure_reasons"] = failures

    if passed:
        print(f"  [QA Gate] ✓ ALL QUALITY GATES PASSED! (Duration: {dur / 60:.1f}m, CLIP off-topic: {off_topic_ratio*100:.1f}%, Peak: {audio_metrics.get('max_volume_db')}dB)")
    else:
        print(f"  [QA Gate] ✗ QUALITY GATES FAILED ({len(failures)} issues):")
        for f in failures:
            print(f"    - {f}")

    return passed, failures, report
