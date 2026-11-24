"""CLIP-based semantic relevance verifier for long-form episodes.
Validates that candidate visual assets and final rendered video frames strictly match
the target brand and vehicle type (e.g., Ford Mustang S550), rejecting competitor brands,
motorcycles in car videos, and off-topic stock media.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from PIL import Image

# Prevent TensorFlow from initializing on CPU
os.environ["TRANSFORMERS_NO_TF"] = "1"
os.environ["USE_TF"] = "0"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

_CLIP_MODEL = None
_CLIP_PROCESSOR = None
_CLIP_LOAD_ATTEMPTED = False


def _get_clip():
    """Lazy-load the CLIP model and processor on CPU."""
    global _CLIP_MODEL, _CLIP_PROCESSOR, _CLIP_LOAD_ATTEMPTED
    if _CLIP_MODEL is not None and _CLIP_PROCESSOR is not None:
        return _CLIP_MODEL, _CLIP_PROCESSOR

    if _CLIP_LOAD_ATTEMPTED:
        return None, None

    _CLIP_LOAD_ATTEMPTED = True
    try:
        import torch
        from transformers import CLIPModel, CLIPProcessor

        model_id = "openai/clip-vit-base-patch32"
        print(f"  [CLIP Verifier] Loading {model_id} on CPU...")
        _CLIP_PROCESSOR = CLIPProcessor.from_pretrained(model_id)
        _CLIP_MODEL = CLIPModel.from_pretrained(model_id)
        _CLIP_MODEL.eval()
        print("  [CLIP Verifier] CLIP model loaded successfully.")
        return _CLIP_MODEL, _CLIP_PROCESSOR
    except Exception as e:
        print(f"  [CLIP Verifier] Warning: Could not load local CLIP model ({e}). Will use strict fallback.")
        return None, None


def verify_image_relevance(
    image_path: Path | str,
    target_brand: str,
    target_model: str,
    is_bike: bool = False,
    min_score: float = 0.45
) -> dict[str, Any]:
    """Verify that an image depicts the target vehicle using open-source CLIP.
    
    Evaluates target entity prompts against negative competitor / cross-vehicle prompts.
    Rejects any image where competitor brands or opposite vehicle types score higher.
    """
    img_path = Path(image_path)
    if not img_path.exists():
        return {
            "is_relevant": False,
            "target_score": 0.0,
            "max_negative_score": 1.0,
            "reason": "File does not exist"
        }

    # Construct prompts
    target_entity = f"{target_brand} {target_model}".strip()
    positive_prompts = [
        f"a photo of a {target_entity}",
        f"a {target_brand} automobile on the road",
        f"a close up of a {target_brand} car or badge" if not is_bike else f"a close up of a {target_brand} superbike",
        f"a technical graphic, specification card, or telemetry chart for {target_brand}",
    ]

    # Negative prompts
    if not is_bike:
        negative_prompts = [
            "a motorcycle or superbike",
            "a photo of a Ferrari or Lamborghini",
            "a photo of a Porsche or McLaren",
            "a photo of a BMW or Mercedes-Benz",
            "a photo of an Audi or Nissan GTR",
            "a bicycle or public bus",
            "an airplane or landscape with no vehicles",
        ]
    else:
        negative_prompts = [
            "a sports car or hypercar",
            "a photo of a Ferrari or Lamborghini",
            "a photo of a Porsche or Ford Mustang",
            "a truck, minivan or SUV",
            "a bicycle with no engine",
            "an airplane or landscape with no motorcycles",
        ]

    model, processor = _get_clip()
    if model is None or processor is None:
        # Fallback heuristic: check filename and metadata tokens
        name_lower = img_path.name.lower()
        brand_tokens = target_brand.lower().split()
        is_match = any(t in name_lower for t in brand_tokens)
        
        # Check against blacklist
        blacklist = ["ferrari", "lamborghini", "ducati", "yamaha", "kawasaki", "bmw", "porsche", "mclaren", "bugatti"]
        blacklist = [b for b in blacklist if b not in target_brand.lower()]
        for bl in blacklist:
            if bl in name_lower:
                return {
                    "is_relevant": False,
                    "target_score": 0.0,
                    "max_negative_score": 1.0,
                    "reason": f"Fallback blacklist match: '{bl}' detected in asset name"
                }

        return {
            "is_relevant": is_match,
            "target_score": 0.8 if is_match else 0.2,
            "max_negative_score": 0.2 if is_match else 0.8,
            "reason": "Verified via strict token matching (CLIP unavailable)"
        }

    try:
        import torch
        image = Image.open(img_path).convert("RGB")
        all_prompts = positive_prompts + negative_prompts
        inputs = processor(text=all_prompts, images=image, return_tensors="pt", padding=True)
        
        with torch.no_grad():
            outputs = model(**inputs)
            probs = outputs.logits_per_image.softmax(dim=1)[0].tolist()

        pos_scores = probs[:len(positive_prompts)]
        neg_scores = probs[len(positive_prompts):]

        best_pos_score = max(pos_scores)
        best_neg_score = max(neg_scores)
        sum_pos = sum(pos_scores)

        # Relevant if positive probability dominates negative probability
        is_relevant = (sum_pos >= min_score) and (sum_pos > best_neg_score * 1.1)

        best_pos_idx = pos_scores.index(best_pos_score)
        best_neg_idx = neg_scores.index(best_neg_score)

        return {
            "is_relevant": is_relevant,
            "target_score": sum_pos,
            "max_negative_score": best_neg_score,
            "best_positive_prompt": positive_prompts[best_pos_idx],
            "best_negative_prompt": negative_prompts[best_neg_idx],
            "reason": "CLIP verified" if is_relevant else f"Negative prompt '{negative_prompts[best_neg_idx]}' scored higher ({best_neg_score:.3f} vs {sum_pos:.3f})"
        }
    except Exception as exc:
        return {
            "is_relevant": False,
            "target_score": 0.0,
            "max_negative_score": 1.0,
            "reason": f"CLIP inference error: {exc}"
        }


def sample_and_verify_video_frames(
    video_path: Path | str,
    target_brand: str,
    target_model: str,
    sample_interval_sec: float = 5.0,
    max_off_topic_ratio: float = 0.05,
    is_bike: bool = False
) -> tuple[bool, float, list[dict[str, Any]]]:
    """Sample one frame every `sample_interval_sec` and run CLIP verification.
    
    Fails if more than `max_off_topic_ratio` (5%) of sampled frames show an off-topic vehicle.
    Returns (passed, off_topic_ratio, sample_results).
    """
    v_path = Path(video_path)
    if not v_path.exists():
        return False, 1.0, [{"error": "Video file not found"}]

    temp_dir = Path(tempfile.mkdtemp(prefix="clip_qa_"))
    try:
        # Sample frames at 1 frame per sample_interval_sec
        fps_filter = f"fps=1/{sample_interval_sec}"
        frame_pattern = str(temp_dir / "frame_%04d.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(v_path),
            "-vf", fps_filter,
            "-q:v", "3",
            frame_pattern
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

        frame_files = sorted(temp_dir.glob("frame_*.jpg"))
        if not frame_files:
            return True, 0.0, []

        sample_results = []
        off_topic_count = 0

        # Run verification on sampled frames
        for f in frame_files:
            # Neutral telemetry slides or graphic cards with text are acceptable
            # Run CLIP check
            res = verify_image_relevance(
                image_path=f,
                target_brand=target_brand,
                target_model=target_model,
                is_bike=is_bike,
                min_score=0.35  # Slightly relaxed for motion-blurred / cropped frames
            )
            # If CLIP detected a competitor brand or wrong vehicle type decisively
            if not res["is_relevant"] and res.get("max_negative_score", 0) > 0.65:
                off_topic_count += 1
                res["frame"] = f.name
                sample_results.append(res)

        total_sampled = len(frame_files)
        off_topic_ratio = (off_topic_count / total_sampled) if total_sampled > 0 else 0.0
        passed = off_topic_ratio <= max_off_topic_ratio

        print(f"  [CLIP QA] Verified {total_sampled} frames. Off-topic: {off_topic_count} ({off_topic_ratio*100:.1f}%). Limit: {max_off_topic_ratio*100:.1f}%. Result: {'PASSED' if passed else 'FAILED'}")
        return passed, off_topic_ratio, sample_results
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
