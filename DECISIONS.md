# DECISIONS.md — Architecture & Design Decisions

## 2026-11-11 — Full AGENT_SPEC Implementation & YouTube Pipeline

### 1. YouTube Posting Strategy: 10 Daily Shorts from Yesterday's 12 Reels
- **Decision:** As specified by the owner:
  - Instagram generates **12 Reels per day** (one every 2 hours).
  - YouTube Data API allows up to **10 Shorts per calendar day** (10 uploads * 1,600 units = 16,000 quota units).
  - Each day, the system evaluates the **12 Instagram Reels published on the previous day (yesterday)** and selects the **top 10**.
  - These 10 are queued in `data/youtube_queue.json` and posted throughout the day to YouTube as individual Shorts.
  - The cycle continues automatically: today's 12 Instagram Reels become tomorrow's 10 YouTube Shorts.
- **Reason:** Keeps YouTube active with 10 high-performing Shorts every single day while guaranteeing the channel never exceeds Google Cloud's YouTube Data API quota.

### 2. Registry Format & Storage Persistence
- **Decision:** Use a structured JSON database (`data/registry.json`) committed to Git and tracked in repository history.
- **Reason:** In GitHub Actions runners, local SQLite databases would be wiped upon runner termination unless explicitly committed or uploaded as artifacts. JSON committed along with `log.json` and state files is durable, portable, and transparent across CI environments.

### 3. Slot Idempotency & Duplicate Prevention
- **Decision:** Enforce slot idempotency by checking if a post was already confirmed within the same 2-hour UTC window.
- **Reason:** Prevents race conditions or accidental double-posts if GitHub Actions triggers multiple runs for the same slot.

### 4. Vehicle Model & Music Cooldowns
- **Decision:** Enforce a 7-day cooldown for vehicle models and a 3-day cooldown for music tracks in `scripts/registry.py`. When candidate sets are selected, the system filters out any set that was published within the cooldown window.
- **Reason:** Satisfies Section 3 of `AGENT_SPEC.md` to prevent viewer fatigue and ensure rich rotation.

### 5. Single-Car Reels vs Multi-Car Comparisons
- **Decision:** Single-car reels (including deep-dive showcases) are 100% clean full-screen 1080x1920 visuals with zero on-screen text or cards. Split-screen (top-half image, bottom-half specs) is reserved strictly for 3-car comparison battles.
- **Reason:** Eliminates confusing "Contender 1 of 4" badges on single vehicles while maintaining high-production aesthetic.

### 6. Media Retention Window (48 Hours)
- **Decision:** Retain generated `media/reel_*.mp4` files for at least 48 hours before any cleanup.
- **Reason:** Yesterday's Instagram Reels must remain accessible on disk so they can be uploaded to YouTube Shorts on the following day.

### 7. Viral Automotive Video Edit Engine (AGENT_SPEC §5 & §7)
- **Decision:** Replace slow static photo slideshows with the high-velocity `scripts/reel_engine.py`:
  - **Hook in First 1–2 Seconds:** Explosive action (launch, rev limiter pops, high-speed flyby) placed at second 0.
  - **Beat-Synced Cuts:** 8 to 12 rapid cuts (0.6s–1.4s per cut) with dynamic camera zooms, whip pans, and white flash transitions synced to the beat.
  - **6 Edit Templates in Active Rotation:** `cinematic_slow_mo`, `speed_ramp`, `phonk_bass_drop`, `night_city`, `rolling_shots`, `engine_sound_showcase`.
  - **Dual-Stem Audio Architecture:** Authentic high-rpm engine scream (V12, V10, V8, Superbike) mixed with aggressive drift phonk (808 bass, cowbell rhythm).
  - **Loudness Normalization:** Integrated EBU R128 (`loudnorm`) targeting -14 LUFS, TP -1.0 dB so the audio is loud, punchy, and never muted on phone speakers.
  - **Automated Quality Gates:** Strict pre-publish checks for 1080x1920 9:16 aspect ratio, 8–14s duration, stereo AAC audio present, and loudness thresholds.
  - **Full Supercar Focus:** Eliminated study journey text overlays and static single photo post crons (`0 13 * * *`). Automated pipeline runs 100% viral supercar/superbike Reels and Shorts.

### 8. Strict 24-Hour Brand Cooldown & 7-Day Model Exclusion
- **Decision:** Enforce two-tier vehicle selection rules in [`scripts/registry.py`](file:///D:/Erika/scripts/registry.py) and [`main.py`](file:///D:/Erika/main.py):
  - **Same-Day / 24-Hour Brand Block (`is_brand_on_cooldown`):** Once a brand posts today (e.g., BMW), no other post from that brand can be scheduled until the next day.
  - **7-Day Model Lockout (`is_model_on_cooldown`):** Even when the brand becomes eligible again on a subsequent day, the previously posted model (e.g., BMW M4) remains locked for 7 days, forcing the system to choose a distinct model (e.g., BMW S1000RR, Ferrari F80 vs SF90, Lamborghini Diablo vs Revuelto).
