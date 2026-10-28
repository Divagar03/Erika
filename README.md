# ig-influencer

**An automated AI Instagram influencer account, built entirely on free-tier services.**

> 🤖 *Lyra Voss* — a disclosed AI character. Her bio says "AI character". She is not a real person.

---

## What This Does

| Feature | Details |
|---|---|
| 📸 **2 photo posts/day** | 03:30 & 13:30 UTC via GitHub Actions cron |
| 🎬 **Weekly Reel** | Every Saturday 12:00 UTC — 5 AI images + FFmpeg video |
| 📤 **User Reel uploads** | Drop `.mp4` in `uploads/` → auto-published on push |
| 🧠 **AI captions** | Gemini 2.5 Flash generates scene, caption, hashtags |
| 🖼️ **AI images** | Pollinations.ai (free, no key) with fixed seed for face consistency |
| 📊 **Web Dashboard** | GitHub Pages · stat cards, chart, activity log, controls |
| ⏸ **Pause/Resume** | One click in the dashboard |
| 📝 **Logging** | `data/log.json` — last 500 entries |

## Cost

**Zero.** Everything uses free tiers:
- GitHub Actions: 2,000 free minutes/month (this uses ~6 min/day = ~180 min/month)
- GitHub Pages: free for public repos
- Gemini API: 1,500 requests/day free
- Pollinations.ai: completely free, no account needed
- Instagram Graph API: free (requires a Professional account + Facebook Page)

## Quick Start

→ See **[SETUP.md](SETUP.md)** for full step-by-step instructions.

## File Structure

```
ig-influencer/
├── .github/workflows/instagram.yml  # Automation workflow
├── assets/music/                    # Drop .mp3 files here for reel audio
├── data/
│   ├── config.json                  # {"paused": false} — edit to pause
│   └── log.json                     # Activity log (auto-managed)
├── docs/index.html                  # Dashboard (GitHub Pages)
├── media/                           # Published images/videos (auto-committed)
├── uploads/                         # Drop .mp4 here to trigger reel publish
│   └── done/                        # Processed uploads moved here
├── main.py                          # Core automation script
├── persona.json                     # AI character definition
└── requirements.txt
```

## Dry Run

Test content generation without posting anything:

```bash
GEMINI_API_KEY=your_key python main.py post --dry-run
GEMINI_API_KEY=your_key python main.py weekly_reel --dry-run
```

## Customise the Persona

Edit `persona.json` to change:
- `name` / `handle` / `bio` — character identity
- `voice` — writing style for captions
- `look` — physical appearance for image generation
- `style` — photography/visual style
- `seed` — fixed number for face consistency (change to reset the look)
- `themes` — list of content topics to rotate through

## Uploading a Reel Manually

**Via the dashboard:** Use the "Upload a Reel" section — attach your `.mp4` and optional caption.

**Via git:** Drop your `.mp4` into `uploads/` and push. If you want a custom caption, create a `.txt` file with the same name (e.g. `uploads/myreel.txt`). A GitHub Actions run will start within seconds and publish the reel.

## Pausing Automation

In the dashboard, click **"⏸ Pause Automation"**. This edits `data/config.json` via the GitHub API. All scheduled runs check this flag and exit immediately if `paused` is `true`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Scheduled posts stopped | Re-enable Actions (GitHub disables crons after 60 days of inactivity) |
| `token expired` error | Regenerate Page access token — see SETUP.md Step 3d |
| Image generation fails | Pollinations.ai is temporarily down; re-run the workflow manually |
| Dashboard shows blank | Open ⚙ Settings, verify token and repo name |
| Reel stuck at IN_PROGRESS | Instagram processing is slow; it polls for up to 3 minutes |

## Known Limitations

- Face is **consistent, not identical** — free image generation with a fixed seed gives similar but not pixel-perfect results across posts.
- **No trending audio** — Instagram's API does not support adding trending sounds. Reels use royalty-free music from `assets/music/`.
- **Professional account required** — personal Instagram accounts cannot use the Publishing API.
- **Meta Development mode** — the app can only post to accounts you own/control. Full publishing to any account requires App Review (not needed for self-use).
