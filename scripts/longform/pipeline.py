"""Master orchestrator for the ZenithMotors Long-Form YouTube Pipeline.
Executes Stages 1-13 with 3 retries, checkpoint resume, blocking QA,
and automatic post-upload Shorts generation.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analytics import sync_youtube_retention_metrics
from .db import (
    create_episode,
    get_episode,
    get_recent_scripts,
    init_db,
    update_episode,
)
from .music import prepare_background_music
from .qa import verify_episode_quality
from .renderer import render_full_episode_video
from .research import conduct_research
from .rotation import load_config, select_topic_and_format
from .scriptwriter import generate_episode_script
from .shorts_extractor import extract_promotional_shorts
from .thumbnail import generate_thumbnail
from .uploader import upload_longform_video
from .voice import synthesize_episode_voice

ROOT = Path(__file__).resolve().parent.parent.parent


def run_longform_pipeline(
    env: dict[str, Any],
    dry_run: bool = False,
    force_brand: str | None = None,
    force_model: str | None = None,
    force_format: str | None = None
) -> dict[str, Any]:
    """Execute the complete end-to-end long-form YouTube documentary pipeline."""
    start_time = time.time()
    print("=" * 70)
    print("🎬 ZENITHMOTORS LONG-FORM YOUTUBE DOCUMENTARY PIPELINE (18-22 MIN)")
    print(f"Mode: {'DRY RUN (no upload, test rendering)' if dry_run else 'LIVE PRODUCTION'}")
    print("=" * 70)

    config = load_config()
    init_db()

    # Sync historical analytics
    try:
        sync_youtube_retention_metrics(env)
    except Exception as e:
        print(f"  [Analytics Sync] Note: {e}")

    # -------------------------------------------------------------
    # STAGE 1: TOPIC & FORMAT SELECTION (Rotation Engine)
    # -------------------------------------------------------------
    print("\n--- STAGE 1: TOPIC & FORMAT SELECTION ---")
    topic = select_topic_and_format()
    if force_brand:
        topic["brand"] = force_brand
    if force_model:
        topic["model"] = force_model
    if force_format:
        matched_fmt = [f for f in config["formats"] if f["id"] == force_format]
        if matched_fmt:
            topic["format"] = matched_fmt[0]

    ts_now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    episode_id = f"ep_{ts_now}"
    create_episode(
        topic_brand=topic["brand"],
        topic_model=topic["model"],
        topic_colour=topic.get("colour"),
        format_id=topic["format"]["id"],
        hook_style_id=topic["hook_style"]["id"],
        episode_id=episode_id
    )

    print(f"  Episode ID:    {episode_id}")
    print(f"  Subject:       {topic['brand']} {topic['model']} ({topic.get('colour', 'standard')})")
    print(f"  Format:        {topic['format']['name']}")
    print(f"  Hook Style:    {topic['hook_style']['name']}")

    # -------------------------------------------------------------
    # STAGE 2: FACTUAL RESEARCH (Wikipedia & Archives)
    # -------------------------------------------------------------
    print("\n--- STAGE 2: FACTUAL RESEARCH HARVESTER ---")
    facts = {}
    for attempt in range(1, 4):
        try:
            facts = conduct_research(
                brand=topic["brand"],
                model=topic["model"],
                format_id=topic["format"]["id"],
                episode_id=episode_id
            )
            break
        except Exception as exc:
            print(f"  [Research] Attempt {attempt} failed: {exc}")
            if attempt == 3:
                update_episode(episode_id, status="failed", error_message=f"Research failed: {exc}")
                raise RuntimeError(f"Stage 2 failed after 3 attempts: {exc}")
            time.sleep(2)

    # -------------------------------------------------------------
    # STAGE 3: SCRIPT GENERATION & FACTUALITY QA
    # -------------------------------------------------------------
    print("\n--- STAGE 3: SCRIPT GENERATION & ANTI-REPETITION GATE ---")
    historical_records = get_recent_scripts(limit=config["anti_repetition"]["similarity_history_window"])
    historical_hashes = [r.get("script_hash", "") for r in historical_records]

    script_data = {}
    for attempt in range(1, 4):
        try:
            script_data = generate_episode_script(
                topic=topic,
                facts=facts,
                historical_scripts=historical_hashes,
                env=env,
                episode_id=episode_id
            )
            update_episode(
                episode_id,
                title=f"{topic['brand']} {topic['model']}: The Engineering Masterclass",
                script_hash=script_data.get("script_hash"),
                script_word_count=script_data.get("total_words"),
                status="scripted"
            )
            break
        except Exception as exc:
            print(f"  [Scriptwriter] Attempt {attempt} failed: {exc}")
            if attempt == 3:
                update_episode(episode_id, status="failed", error_message=f"Scriptwriting failed: {exc}")
                raise RuntimeError(f"Stage 3 failed after 3 attempts: {exc}")
            time.sleep(2)

    # -------------------------------------------------------------
    # STAGE 4: VOICE SYNTHESIS & TIMINGS (edge-tts)
    # -------------------------------------------------------------
    print("\n--- STAGE 4: HIGH-FIDELITY VOICE SYNTHESIS ---")
    voice_name = config["voice"]["primary"]
    voice_data = {}
    for attempt in range(1, 4):
        try:
            voice_data = synthesize_episode_voice(
                script_data=script_data,
                episode_id=episode_id,
                voice=voice_name
            )
            update_episode(
                episode_id,
                duration_seconds=voice_data["total_duration_sec"],
                srt_path=voice_data["srt_path"]
            )
            break
        except Exception as exc:
            print(f"  [Voice] Attempt {attempt} failed: {exc}")
            if attempt == 3:
                update_episode(episode_id, status="failed", error_message=f"Voice synthesis failed: {exc}")
                raise RuntimeError(f"Stage 4 failed after 3 attempts: {exc}")
            time.sleep(2)

    # -------------------------------------------------------------
    # STAGE 5: VISUAL CURATION & TIMELINE
    # -------------------------------------------------------------
    print("\n--- STAGE 5: VISUAL CURATION & ATTRIBUTION COMPILATION ---")
    from .visuals import plan_visual_timeline
    visual_plan = plan_visual_timeline(
        brand=topic["brand"],
        model=topic["model"],
        chapter_timings=voice_data["chapter_timings"],
        episode_id=episode_id
    )
    update_episode(
        episode_id,
        assets_used=list(visual_plan.get("asset_usage_counts", {}).keys()),
        attributions=visual_plan.get("attributions", [])
    )

    # -------------------------------------------------------------
    # STAGE 6: BACKGROUND MUSIC & DUCKING
    # -------------------------------------------------------------
    print("\n--- STAGE 6: SOUNDTRACK PREPARATION ---")
    bg_music_path = prepare_background_music(
        total_duration_sec=voice_data["total_duration_sec"],
        episode_id=episode_id
    )

    # -------------------------------------------------------------
    # STAGE 7: CHUNK-BASED VIDEO RENDERING (FFmpeg)
    # -------------------------------------------------------------
    print("\n--- STAGE 7: CHUNK-BASED 1080P VIDEO RENDERING ---")
    rendered_video_path = Path()
    for attempt in range(1, 4):
        try:
            rendered_video_path = render_full_episode_video(
                chapter_timings=voice_data["chapter_timings"],
                planned_shots=visual_plan["shots"],
                bg_music_path=bg_music_path,
                episode_id=episode_id
            )
            update_episode(
                episode_id,
                video_path=str(rendered_video_path),
                status="rendered"
            )
            break
        except Exception as exc:
            print(f"  [Renderer] Attempt {attempt} failed: {exc}")
            if attempt == 3:
                update_episode(episode_id, status="failed", error_message=f"Rendering failed: {exc}")
                raise RuntimeError(f"Stage 7 failed after 3 attempts: {exc}")
            time.sleep(3)

    # -------------------------------------------------------------
    # STAGE 8: THUMBNAIL GENERATION (Pillow High-Contrast A/B)
    # -------------------------------------------------------------
    print("\n--- STAGE 8: THUMBNAIL GENERATOR (PILLOW A/B) ---")
    # Grab first available car still
    sample_car_img = None
    for shot in visual_plan.get("shots", []):
        p_str = shot.get("asset_path")
        if p_str and Path(p_str).exists():
            sample_car_img = Path(p_str)
            break

    thumbnail_path = generate_thumbnail(
        brand=topic["brand"],
        model=topic["model"],
        format_name=topic["format"]["name"],
        car_image_path=sample_car_img,
        episode_id=episode_id
    )
    update_episode(episode_id, thumbnail_path=str(thumbnail_path))

    # -------------------------------------------------------------
    # STAGE 9: BLOCKING QA QUALITY GATE
    # -------------------------------------------------------------
    print("\n--- STAGE 9: BLOCKING QUALITY ASSURANCE GATE ---")
    min_dur = config["qa_gates"]["min_duration_sec"]
    max_dur = config["qa_gates"]["max_duration_sec"]

    passed_qa, qa_failures, qa_report = verify_episode_quality(
        video_path=rendered_video_path,
        script_data=script_data,
        visual_plan=visual_plan,
        min_duration_sec=min_dur,
        max_duration_sec=max_dur,
        is_dry_run=dry_run
    )
    update_episode(episode_id, qa_report=qa_report)

    if not passed_qa and not dry_run:
        update_episode(episode_id, status="failed", error_message="; ".join(qa_failures))
        print("  [QA Gate] ✗ PUBLISHING ABORTED: Video failed mandatory QA gates.")
        return {"status": "qa_failed", "episode_id": episode_id, "reasons": qa_failures, "error": "; ".join(qa_failures)}

    update_episode(episode_id, status="qa_passed")

    # -------------------------------------------------------------
    # STAGE 10: YOUTUBE UPLOAD (Resumable + Thumbnail + Captions)
    # -------------------------------------------------------------
    print("\n--- STAGE 10: YOUTUBE PUBLISHING ENGINE ---")
    yt_id = None
    yt_err = ""
    for attempt in range(1, 4):
        try:
            yt_id, yt_err = upload_longform_video(
                video_path=rendered_video_path,
                thumbnail_path=thumbnail_path,
                srt_path=Path(voice_data["srt_path"]),
                brand=topic["brand"],
                model=topic["model"],
                format_name=topic["format"]["name"],
                chapter_timings=voice_data["chapter_timings"],
                attributions=visual_plan["attributions"],
                env=env,
                is_dry_run=dry_run
            )
            if yt_id:
                break
        except Exception as exc:
            yt_err = str(exc)
            print(f"  [Uploader] Attempt {attempt} upload exception: {exc}")
            if attempt == 3:
                break
            time.sleep(3)

    if not yt_id:
        update_episode(episode_id, status="failed", error_message=yt_err)
        print(f"  [Uploader] ✗ Upload failed: {yt_err}")
        return {"status": "upload_failed", "episode_id": episode_id, "error": yt_err}

    update_episode(
        episode_id,
        youtube_id=yt_id,
        status="published",
        published_at=datetime.now(timezone.utc).isoformat()
    )

    # -------------------------------------------------------------
    # STAGE 11: POST-UPLOAD SHORTS EXTRACTOR (2-3 Vertical Highlights)
    # -------------------------------------------------------------
    print("\n--- STAGE 11: POST-UPLOAD PROMOTIONAL SHORTS EXTRACTOR ---")
    try:
        shorts_count = config["publishing"].get("shorts_to_extract", 2)
        extracted_shorts = extract_promotional_shorts(
            master_video_path=rendered_video_path,
            chapter_timings=voice_data["chapter_timings"],
            brand=topic["brand"],
            model=topic["model"],
            youtube_id=yt_id,
            episode_id=episode_id,
            count=shorts_count
        )
        print(f"  [ShortsExtractor] Generated {len(extracted_shorts)} vertical promotional teasers.")
    except Exception as e:
        print(f"  [ShortsExtractor] Note: {e}")

    elapsed_min = (time.time() - start_time) / 60.0
    print("=" * 70)
    print(f"🎉 LONG-FORM EPISODE COMPLETED SUCCESSFULLY in {elapsed_min:.1f} minutes!")
    print(f"  Video ID:     {yt_id}")
    print(f"  Watch URL:    https://youtube.com/watch?v={yt_id}")
    print(f"  Episode File: {rendered_video_path.name}")
    print("=" * 70)

    return {
        "status": "success",
        "episode_id": episode_id,
        "youtube_id": yt_id,
        "watch_url": f"https://youtube.com/watch?v={yt_id}",
        "video_path": str(rendered_video_path),
        "thumbnail_path": str(thumbnail_path),
        "srt_path": voice_data["srt_path"],
        "duration_min": voice_data["total_duration_sec"] / 60.0,
        "words": script_data.get("total_words", 0)
    }
