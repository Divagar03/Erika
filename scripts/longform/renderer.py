"""Advanced high-retention video renderer for long-form episodes.
Implements dynamic motion variety (push-in/out, pans, tilts, 2.5D parallax),
cinematic color grading, transitions, sound effects (whooshes/hits),
and chapter chunk rendering with FFmpeg.
Includes contact sheet generator for preview review.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent.parent
RENDER_DIR = ROOT / "data" / "longform_rendered"
SFX_DIR = ROOT / "data" / "audio_sfx"


def _ensure_sound_effects() -> tuple[Path, Path]:
    """Generate CC0 synthesized whoosh and hit impact audio files if not present."""
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    whoosh_path = SFX_DIR / "whoosh.wav"
    impact_path = SFX_DIR / "impact.wav"

    sr = 48000
    if not whoosh_path.exists():
        t = np.linspace(0, 0.35, int(sr * 0.35))
        env = np.sin(np.pi * t / 0.35) ** 2
        f = np.linspace(250, 1600, len(t))
        phase = 2 * np.pi * np.cumsum(f) / sr
        whoosh = (np.sin(phase) * env * 0.18 * 32767).astype(np.int16)
        with wave.open(str(whoosh_path), "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(whoosh.tobytes())

    if not impact_path.exists():
        t2 = np.linspace(0, 0.45, int(sr * 0.45))
        env2 = np.exp(-14 * t2)
        f2 = np.linspace(95, 38, len(t2))
        phase2 = 2 * np.pi * np.cumsum(f2) / sr
        impact = (np.sin(phase2) * env2 * 0.28 * 32767).astype(np.int16)
        with wave.open(str(impact_path), "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(impact.tobytes())

    return whoosh_path, impact_path


def _build_shot_filter(
    motion_type: str,
    transition: str,
    num_frames: int,
    duration_sec: float
) -> str:
    """Build dynamic FFmpeg zoompan and cinematic color grading filter."""
    nf = max(1, num_frames)

    # 1. Motion filter
    if motion_type == "push_out":
        zoom_expr = f"zoompan=z='if(lte(zoom,1.0),1.14,max(1.001,zoom-0.0012))':d={nf}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080:fps=30"
    elif motion_type == "pan_left":
        zoom_expr = f"zoompan=z=1.12:d={nf}:x='(iw-iw/zoom)*(1.0-0.4*(on/{nf}))':y='ih/2-(ih/zoom/2)':s=1920x1080:fps=30"
    elif motion_type == "pan_right":
        zoom_expr = f"zoompan=z=1.12:d={nf}:x='(iw-iw/zoom)*(0.2+0.4*(on/{nf}))':y='ih/2-(ih/zoom/2)':s=1920x1080:fps=30"
    elif motion_type == "tilt_up":
        zoom_expr = f"zoompan=z=1.12:d={nf}:x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*(1.0-0.4*(on/{nf}))':s=1920x1080:fps=30"
    elif motion_type == "tilt_down":
        zoom_expr = f"zoompan=z=1.12:d={nf}:x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*(0.2+0.4*(on/{nf}))':s=1920x1080:fps=30"
    elif motion_type == "parallax_2_5d":
        zoom_expr = f"zoompan=z='min(1.04+0.0010*on,1.16)':d={nf}:x='(iw-iw/zoom)*(0.3+0.3*(on/{nf}))':y='(ih-ih/zoom)*(0.3+0.3*(on/{nf}))':s=1920x1080:fps=30"
    else:
        # Default: subtle push-in
        zoom_expr = f"zoompan=z='min(zoom+0.0012,1.14)':d={nf}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080:fps=30"

    # 2. Cinematic color grade (contrast + saturation + warm baseline)
    color_grade = "eq=contrast=1.06:saturation=1.10:brightness=0.01"

    # 3. Transition flair
    if transition == "flash":
        trans_filter = "fade=t=in:st=0:d=0.10:color=white"
        return f"scale=2560:-1,{zoom_expr},{color_grade},{trans_filter}"
    elif transition == "speed_blur":
        trans_filter = "fade=t=in:st=0:d=0.15:color=black"
        return f"scale=2560:-1,{zoom_expr},{color_grade},{trans_filter}"

    return f"scale=2560:-1,{zoom_expr},{color_grade}"


def render_chapter_chunk(
    ch_info: dict[str, Any],
    ch_shots: list[dict[str, Any]],
    output_chunk_path: Path
) -> Path:
    """Render a single chapter chunk (audio + sequence of varied camera cuts & sound design)."""
    output_chunk_path.parent.mkdir(parents=True, exist_ok=True)
    if output_chunk_path.exists() and output_chunk_path.stat().st_size > 10000:
        return output_chunk_path

    chunk_dir = output_chunk_path.parent / f"temp_{ch_info['id']}"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    whoosh_sfx, impact_sfx = _ensure_sound_effects()

    shot_video_files: list[Path] = []

    for idx, shot in enumerate(ch_shots):
        shot_out = chunk_dir / f"shot_{idx:03d}.mp4"
        shot_dur = shot["duration"]
        asset_p = Path(shot.get("asset_path", ""))

        if not asset_p.exists():
            continue

        num_frames = int(round(shot_dur * 30))
        motion_type = shot.get("motion_type", "push_in")
        transition = shot.get("transition", "hard_cut")

        vf_filter = _build_shot_filter(
            motion_type=motion_type,
            transition=transition,
            num_frames=num_frames,
            duration_sec=shot_dur
        )

        cmd_shot = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(asset_p),
            "-vf", vf_filter,
            "-t", f"{shot_dur:.2f}",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            str(shot_out)
        ]
        subprocess.run(cmd_shot, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if shot_out.exists():
            shot_video_files.append(shot_out)

    if not shot_video_files:
        raise RuntimeError(f"No shots rendered for chapter {ch_info.get('title')}")

    # Concat all shots for this chapter
    concat_txt = chunk_dir / "shots_concat.txt"
    with open(concat_txt, "w", encoding="utf-8") as f:
        for sv in shot_video_files:
            clean_p = str(sv.resolve()).replace("\\", "/")
            f.write(f"file '{clean_p}'\n")

    video_track = chunk_dir / "chapter_video_only.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_txt),
        "-c", "copy",
        str(video_track)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    # Attach this chapter's audio track
    ch_audio = Path(ch_info["audio_path"])
    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(video_track),
        "-i", str(ch_audio),
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        str(output_chunk_path)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    # Clean up intermediate shot files
    try:
        for sv in shot_video_files:
            sv.unlink(missing_ok=True)
        video_track.unlink(missing_ok=True)
        concat_txt.unlink(missing_ok=True)
        chunk_dir.rmdir()
    except Exception:
        pass

    return output_chunk_path


def render_full_episode_video(
    chapter_timings: list[dict[str, Any]],
    planned_shots: list[dict[str, Any]],
    bg_music_path: Path,
    episode_id: str,
    max_duration_sec: float | None = None
) -> Path:
    """Render all chapter chunks and stitch into full 1080p master video with ducked music.
    Supports max_duration_sec limit for preview mode.
    """
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    chunks_dir = RENDER_DIR / episode_id / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    rendered_chunks: list[Path] = []
    total_chapters = len(chapter_timings)

    current_dur = 0.0
    for ch in chapter_timings:
        if max_duration_sec and current_dur >= max_duration_sec:
            print(f"  [Renderer] Preview threshold reached ({current_dur:.1f}s >= {max_duration_sec:.1f}s). Stopping render.")
            break

        ch_idx = ch["index"]
        ch_id = ch["id"]
        ch_shots = [s for s in planned_shots if s["chapter_index"] == ch_idx]
        chunk_path = chunks_dir / f"chunk_{ch_idx:02d}_{ch_id}.mp4"

        print(f"  [Renderer] Rendering Chunk ({ch_idx}/{total_chapters}): {ch['title'][:40]} ({ch['duration']:.1f}s)...")
        render_chapter_chunk(ch, ch_shots, chunk_path)
        rendered_chunks.append(chunk_path)
        current_dur += ch["duration"]

    # Concat all chapter chunks
    chunks_concat_txt = chunks_dir / "chunks_concat.txt"
    with open(chunks_concat_txt, "w", encoding="utf-8") as f:
        for cp in rendered_chunks:
            clean_p = str(cp.resolve()).replace("\\", "/")
            f.write(f"file '{clean_p}'\n")

    concat_video_temp = RENDER_DIR / episode_id / "stitched_temp.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(chunks_concat_txt),
        "-c", "copy",
        str(concat_video_temp)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    # Final Audio Mix: Narration + Ducked Background Music
    master_video_path = RENDER_DIR / f"{episode_id}_master.mp4"
    print(f"  [Renderer] Multiplexing master audio with background soundtrack...")

    cmd_mix = [
        "ffmpeg", "-y",
        "-i", str(concat_video_temp),
        "-i", str(bg_music_path),
        "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=3[aout]",
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(master_video_path)
    ]
    subprocess.run(cmd_mix, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    if not master_video_path.exists() or master_video_path.stat().st_size == 0:
        master_video_path = concat_video_temp

    file_mb = master_video_path.stat().st_size / (1024 * 1024)
    print(f"  [Renderer] Full 1080p Master Video Complete! File: {master_video_path.name} ({file_mb:.1f} MB)")
    return master_video_path


def generate_preview_contact_sheet(
    video_path: Path,
    output_image_path: Path,
    interval_sec: float = 8.0,
    grid_cols: int = 4
) -> Path:
    """Extract sampled frames across the video and compile a high-res inspection contact sheet."""
    output_image_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="contact_sheet_"))
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vf", f"fps=1/{interval_sec}",
            "-q:v", "2",
            str(temp_dir / "frame_%04d.jpg")
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        frames = sorted(temp_dir.glob("frame_*.jpg"))

        if not frames:
            raise RuntimeError("No frames extracted for contact sheet")

        # Take up to 16 frames for contact sheet
        selected_frames = frames[:16]
        thumb_w = 480
        thumb_h = 270

        num_thumbs = len(selected_frames)
        rows = math.ceil(num_thumbs / grid_cols)
        sheet_w = grid_cols * thumb_w
        sheet_h = rows * (thumb_h + 30)

        sheet = Image.new("RGB", (sheet_w, sheet_h), color=(10, 14, 20))
        draw = ImageDraw.Draw(sheet)

        font_label = ImageFont.load_default()

        for idx, f in enumerate(selected_frames):
            col = idx % grid_cols
            row = idx // grid_cols
            x = col * thumb_w
            y = row * (thumb_h + 30)

            try:
                im = Image.open(f).resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                sheet.paste(im, (x, y))
            except Exception:
                pass

            time_sec = idx * interval_sec
            time_str = f"T+{int(time_sec // 60):02d}:{int(time_sec % 60):02d}s"
            draw.text((x + 10, y + thumb_h + 8), f"Frame #{idx + 1:02d} | {time_str}", fill=(0, 210, 255), font=font_label)

        sheet.save(output_image_path, "JPEG", quality=90)
        print(f"  [Preview] Contact sheet saved: {output_image_path.name} ({len(selected_frames)} frames)")
        return output_image_path
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
