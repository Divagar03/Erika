# ZenithMotors Long-Form YouTube Automation Pipeline (18-22 Min)

## 1. Overview & Objective
The Long-Form YouTube Pipeline automatically conceives, researches, scripts, voices, animates, renders, quality-checks, and publishes **18 to 22-minute documentary-style videos** every 48 hours to the YouTube channel (`Apex_Rich`).

### Primary Goal
Rapidly accumulate **4,000 public long-form watch hours** required for YouTube Partner Program (YPP) monetization without paying for third-party software, stock footage subscriptions, or voice-cloning APIs.

### Non-Interference Guarantee
The long-form pipeline executes under an isolated concurrency group (`group: youtube-longform`). It **never** locks, delays, or interferes with:
- The 2-hour Instagram Reels automation (`instagram.yml`).
- The daily top-10 YouTube Shorts queue (`youtube_queue.json`).

---

## 2. Pipeline Stages

```
[1. Topic & Format Selection] (Rotation Engine + Anti-Repetition + Analytics Weights)
           │
           ▼
[2. Factual Research Harvester] (Wikipedia REST API + Historical Dossiers)
           │
           ▼
[3. Script Generation & 2-Pass QA] (10 Chapters, ~3,000 words, TF-IDF < 0.50)
           │
           ▼
[4. Neural Voiceover Synthesis] (edge-tts, -14 LUFS Loudnorm, SRT Subtitles)
           │
           ▼
[5. Visual Asset Curation] (Local Sets + Wikimedia Commons CC Attribution)
           │
           ▼
[6. Ambient Soundtrack Preparation] (Royalty-free synth, auto-ducked)
           │
           ▼
[7. Chunk-Based 1080p Video Rendering] (FFmpeg chapter-by-chapter concat)
           │
           ▼
[8. High-Contrast Thumbnail Generator] (Pillow A/B RMS Contrast Selection)
           │
           ▼
[9. Blocking Quality Assurance Gate] (Duration, audio clipping, silence, black frames)
           │
           ▼
[10. YouTube Data API v3 Upload] (Resumable upload + Thumbnail + SRT + Chapters)
           │
           ▼
[11. Post-Upload Promotional Shorts Extractor] (2-3 vertical cuts queued into Shorts feed)
           │
           ▼
[12. Analytics Retention Feedback Loop] (Adapts format & hook weights after 48h)
```

---

## 3. Anti-Repetition & Policy Safety

YouTube strictly demonetizes repetitive, template-like, or slideshow content. To guarantee high retention and monetization safety:

### A. 8 Rotating Episode Formats
No format can be repeated within the last 5 episodes:
1. **Top 10 Countdown** (`top_10_countdown`): Ranked progression with increasing stakes and mid-video #5 pivot.
2. **Brand History & Evolution** (`brand_history_evolution`): Crisis-to-rebirth corporate documentary.
3. **Single-Model Deep Dive** (`model_deep_dive`): Engineering masterclass dissecting powertrain, chassis, and aerodynamics.
4. **Head-to-Head Comparison** (`head_to_head_comparison`): Telemetry and dyno duel between bitter adversaries.
5. **Myths & Facts Exposed** (`myths_and_facts`): Debunking top internet misconceptions with verified telemetry.
6. **Engineering Explained** (`engineering_explained`): Deep dive into mechanical wizardry (e.g. Freevalve, active aero).
7. **What Happened To Discontinued Legends** (`what_happened_to`): Investigative post-mortem into cancelled skunkworks projects.
8. **Buyer's Guide & Real Ownership Costs** (`buyers_guide_ownership`): Realistic breakdown of maintenance, tires, and failure points.

### B. 6 Dynamic Intro Hook Styles
No hook style can repeat within the last 5 episodes:
- **Bold Provocative Claim**: Counter-intuitive assertion within the first 7 seconds.
- **High-Stakes Secret Crisis**: Confidential disaster or crash during prototype testing.
- **Sensory Telemetry Shock**: Direct acoustic, G-force, or turbo boost telemetry opening.
- **Underdog Historical Paradox**: Historical rivalry that forced impossible engineering solutions.
- **Statistical Anomaly**: Shocking cost or performance statistic that defies expectations.
- **Insider Confidential Reveal**: What factory test drivers and confidential service bulletins documented.

### C. TF-IDF Cosine Similarity Gate
Every newly generated script is vectorized and compared against the last 20 published episode scripts using Scikit-Learn TF-IDF cosine similarity. If similarity is **greater than 0.50**, the script is immediately rejected and regenerated with an alternative angle.

### D. Two-Pass Factual Verification
- **Pass 1**: Multi-segment script generation based on factual research.
- **Pass 2**: LLM fact-checking audit that flags or removes unverified numbers, lap times, or speculative claims.

---

## 4. Visuals & Audio Standards

- **Safe Visual Sources**:
  - Local vehicle studio archives (`assets/vehicles/sets/`).
  - High-res Wikimedia Commons photography (public domain, CC-BY, CC-BY-SA).
  - All attribution metadata (author, license, URL) is automatically compiled and embedded in the YouTube video description.
  - **Asset Reuse Rule**: No single asset may appear more than twice per video.
  - Visuals cut every 5 to 7 seconds with dynamic Ken Burns pan/zoom motion.
- **Audio & Voiceover**:
  - `edge-tts` Microsoft Edge neural narrator (`en-US-ChristopherNeural` or alternates).
  - Pacing modulation per chapter (`+2%`, `-2%`, `+0%`).
  - Broadcast loudness normalization to **-14 LUFS** (EBU R128) with true peak <= -1.5 dB.
  - Background music ducked to -18 dB to -22 dB beneath speech.
  - Timed `.srt` subtitles automatically generated and attached to the upload.

---

## 5. Chunk-Based Video Rendering
Rendering a 20-minute 1080p video in a single FFmpeg command risks memory bloat and runner timeout. The renderer builds each 2-minute chapter into an independent chunk (`chunk_01.mp4` ... `chunk_10.mp4`) and concatenates them with the stream copy demuxer. If a render is interrupted, it resumes from the last completed chunk.

---

## 6. Thumbnail Generator (Pillow A/B)
Generates two high-contrast 1280x720 thumbnail designs:
- **Variant A**: High-stakes mystery angle (bold warning badge, yellow/white typography, dark vignette).
- **Variant B**: Engineering & extreme telemetry angle (cyan/gold palette, bold spec callouts).
Evaluates the RMS visual contrast score of both and automatically deploys the higher-impact design.

---

## 7. Blocking Quality Assurance Gate (QA)
Publishing is strictly blocked if any of the following occur:
- Total duration is outside **18 to 22 minutes** (1,080s – 1,320s).
- Any silent gap in the audio exceeds **2.0 seconds**.
- Black frames detected via FFmpeg `blackdetect`.
- Audio peaks exceed **-1.0 dB** (clipping) or fall below **-18.0 dB**.
- Any image asset is used more than twice.
- Script similarity to past episodes exceeds **0.50**.
- Attributions are missing for external media assets.

---

## 8. CLI Usage & Commands

### Running Locally

```bash
# Run dry-run (generates script, audio, renders sample video, generates thumbnail, skips upload)
python main.py youtube_longform --dry-run

# Run full live production upload
python main.py youtube_longform
```

### Running on GitHub Actions
Go to **Actions** -> **YouTube Long-Form Pipeline (Every 48h)** -> **Run workflow**.
- Set `dry_run: true` to test without publishing.
- Optionally specify a brand or format override.

### Windows Scheduled Task (Local 48-Hour Cron)
To run every 48 hours on your local machine:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\longform\install_longform_scheduler.ps1
```
