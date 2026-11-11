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
