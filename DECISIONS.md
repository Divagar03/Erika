# DECISIONS.md — Architecture & Design Decisions

## 2026-11-11 — Full AGENT_SPEC Implementation

### 1. Registry Format & Storage Persistence
- **Decision:** Use a structured JSON database (`data/registry.json`) committed to Git and tracked in repository history.
- **Reason:** In GitHub Actions runners, local SQLite databases would be wiped upon runner termination unless explicitly committed or uploaded as artifacts. JSON committed along with `log.json` and state files is durable, portable, and transparent across CI environments.

### 2. YouTube Posting Strategy: Daily Top 10 Short
- **Decision:** As specified in Section 2 & 6 of `AGENT_SPEC.md`, YouTube Shorts will now receive **one daily "Top 10" compilation Short** compiled from the previous day's best Instagram Reels (ranked by views, shares, saves, and likes), rather than posting on every 2-hour Instagram reel slot.
- **Reason:** The previous strategy of uploading to YouTube on every 2-hour reel slot quickly hit Google's YouTube Data API v3 daily free quota (10,000 units/day, where each video costs 1,600 units = max 6 uploads/day). Posting one curated Top 10 Short per day at 18:00 UTC strictly satisfies both the 10,000 quota limit and the spec's target runtime of 2–3 minutes.

### 3. Slot Idempotency & Duplicate Prevention
- **Decision:** Enforce slot idempotency by checking if a post was already confirmed within the same 2-hour UTC window.
- **Reason:** Prevents race conditions or accidental double-posts if GitHub Actions triggers multiple runs for the same slot.

### 4. Vehicle Model & Music Cooldowns
- **Decision:** Enforce a 7-day cooldown for vehicle models and a 3-day cooldown for music tracks in `scripts/registry.py`. When candidate sets are selected, the system filters out any set that was published within the cooldown window.
- **Reason:** Satisfies Section 3 of `AGENT_SPEC.md` to prevent viewer fatigue and ensure rich rotation.

### 5. Single-Car Reels vs Multi-Car Comparisons
- **Decision:** Single-car reels (including deep-dive showcases) are 100% clean full-screen 1080x1920 visuals with zero on-screen text or cards. Split-screen (top-half image, bottom-half specs) is reserved strictly for 3-car comparison battles.
- **Reason:** Eliminates confusing "Contender 1 of 4" badges on single vehicles while maintaining high-production aesthetic.

### 6. Edit Template Rotation
- **Decision:** Rotate through the 6 edit templates defined in the spec (`cinematic_slow_mo`, `speed_ramp`, `phonk_bass_drop`, `night_city`, `rolling_shots`, `engine_sound_showcase`).
- **Reason:** Guarantees dynamic pacing, visual diversity, and genuine car-creator human feel.
