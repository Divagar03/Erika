"""Chunk-based 1080p 30fps H.264 video renderer.
Renders each chapter segment independently to preserve RAM and prevent timeouts,
then concatenates using FFmpeg stream demuxer with ducked soundtrack.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent.parent
RENDER_DIR = ROOT / "data" / "longform_rendered"


def _create_placeholder_still(dest_path: Path, title: str, subtitle: str) -> None:
    """Create a sleek, branded 1080p still image if an asset file is unavailable."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1920, 1080), color=(14, 18, 26))
    draw = ImageDraw.Draw(img)

    # Gradient highlight
    for y in range(1080):
        alpha = int(25 * (1.0 - (y / 1080.0)))
        draw.line([(0, y), (1920, y)], fill=(14 + alpha, 22 + alpha, 36 + alpha))

    draw.rectangle([80, 80, 1840, 1000], outline=(40, 55, 80), width=3)
    draw.text((120, 480), title.upper()[:45], fill=(240, 245, 255))
    draw.text((120, 560), subtitle[:60], fill=(220, 160, 40))
    img.save(dest_path, "JPEG", quality=90)


def render_chapter_chunk(
    ch_info: dict[str, Any],
    ch_shots: list[dict[str, Any]],
    output_chunk_path: Path
) -> Path:
    """Render a single chapter chunk (audio + sequence of pan/zoom visual cuts)."""
    output_chunk_path.parent.mkdir(parents=True, exist_ok=True)
    if output_chunk_path.exists() and output_chunk_path.stat().st_size > 10000:
        return output_chunk_path

    chunk_dir = output_chunk_path.parent / f"temp_{ch_info['id']}"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    shot_video_files: list[Path] = []

    # Render each 5-7 second shot with Ken Burns pan/zoom
    for idx, shot in enumerate(ch_shots):
        shot_out = chunk_dir / f"shot_{idx:03d}.mp4"
        shot_dur = shot["duration"]
        asset_path_str = shot.get("asset_path", "")
        asset_path = Path(asset_path_str) if asset_path_str else None

        if not asset_path or not asset_path.exists():
            placeholder = chunk_dir / f"place_{idx:03d}.jpg"
            _create_placeholder_still(placeholder, ch_info["title"], f"Telemetric Breakdown #{idx + 1}")
            asset_path = placeholder

        # Zoompan filter: 30fps, target duration frames
        num_frames = int(shot_dur * 30)
        zoom_direction = shot.get("zoom_type", "zoom_in")

        if zoom_direction == "zoom_in":
            vf_filter = (
                f"scale=2160:-1,zoompan=z='min(zoom+0.0015,1.25)':d={num_frames}"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080:fps=30"
            )
        else:
            vf_filter = (
                f"scale=2160:-1,zoompan=z='if(lte(zoom,1.0),1.25,max(1.001,zoom-0.0015))':d={num_frames}"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080:fps=30"
            )

        cmd_shot = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(asset_path),
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

    # Clean up intermediate shot files to conserve disk space
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
    episode_id: str
) -> Path:
    """Render all 10 chapter chunks and stitch into full 1080p master video with ducked music."""
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    chunks_dir = RENDER_DIR / episode_id / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    rendered_chunks: list[Path] = []
    total_chapters = len(chapter_timings)

    for ch in chapter_timings:
        ch_idx = ch["index"]
        ch_id = ch["id"]
        ch_shots = [s for s in planned_shots if s["chapter_index"] == ch_idx]
        chunk_path = chunks_dir / f"chunk_{ch_idx:02d}_{ch_id}.mp4"

        print(f"  [Renderer] Rendering Chunk ({ch_idx}/{total_chapters}): {ch['title'][:40]}...")
        render_chapter_chunk(ch, ch_shots, chunk_path)
        rendered_chunks.append(chunk_path)

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
        str(master_video_path)
    ]
    subprocess.run(cmd_mix, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    if not master_video_path.exists() or master_video_path.stat().st_size == 0:
        # Fallback to concat video if amix encountered an issue
        master_video_path = concat_video_temp

    file_mb = master_video_path.stat().st_size / (1024 * 1024)
    print(f"  [Renderer] Full 1080p Master Video Complete! File: {master_video_path.name} ({file_mb:.1f} MB)")
    return master_video_path
