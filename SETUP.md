# ig-influencer · Setup Guide

A complete step-by-step guide to get your automated AI Instagram account running in under an hour.

---

## Overview

This system runs entirely for free using:
- **GitHub Actions** – scheduled automation (2 photo posts/day, 1 weekly reel)
- **GitHub Pages** – your web dashboard
- **Gemini API (free tier)** – captions, scenes, hashtags
- **Pollinations.ai (free, no key)** – AI image generation
- **Instagram Graph API** – publishing to your Instagram account

---

## Step 1: Create the GitHub Repository

1. Go to [github.com/new](https://github.com/new).
2. Create a **public** repository named `ig-influencer` (public is required for free GitHub Pages and raw file hosting).
3. Do **not** initialize it with a README (you already have all the files).
4. Copy the repository URL (e.g. `https://github.com/YOUR_USERNAME/ig-influencer.git`).

Now push the code:

```bash
cd ig-influencer          # the folder you cloned or downloaded
git init
git add .
git commit -m "feat: initial ig-influencer setup"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/ig-influencer.git
git push -u origin main
```

---

## Step 2: Set Up the Instagram Account

### 2a. Convert to a Professional Account
1. Open the Instagram app → **Profile** → ≡ menu → **Settings and privacy**.
2. Tap **Account type and tools** → **Switch to Professional Account**.
3. Choose **Creator** or **Business** (either works; Business gives more analytics).
4. Complete the onboarding steps.

### 2b. Write the AI Disclosure Bio
1. On your profile, tap **Edit profile**.
2. In the **Bio** field, add (on the first line): `AI character`
3. You can add more lines below, but the first line must clearly disclose the AI nature.

### 2c. Link to a Facebook Page
Instagram's Graph API requires a linked Facebook Page:
1. Go to [facebook.com/pages/create](https://www.facebook.com/pages/create) and create a **free Page** (any category).
2. In Instagram: **Settings** → **Account** → **Linked accounts** → **Facebook** → connect your page.

---

## Step 3: Create a Meta Developer App

### 3a. Create the App
1. Go to [developers.facebook.com/apps](https://developers.facebook.com/apps) → **Create App**.
2. Choose **Business** as the app type. Click **Next**.
3. Give it any name (e.g. `ig-influencer-bot`). Enter your email. Click **Create App**.

### 3b. Add Instagram Product
1. In your app dashboard, under **Add products to your app**, find **Instagram** → click **Set up**.
2. In the left sidebar click **Instagram** → **API setup with Instagram Business Login**.
3. Under **Generate access tokens**, click **Add Instagram accounts** and connect your Instagram account.

### 3c. Get Required Permissions
Your app needs these permissions (they are available in **Development mode** for your own account):
- `instagram_basic`
- `instagram_content_publish`
- `pages_show_list`
- `pages_read_engagement`

In Development mode these are already available for accounts you control — no App Review needed.

### 3d. Get a Long-Lived (Non-Expiring) Page Access Token

Instagram user tokens expire in 60 days, but Page access tokens linked to a Page can be made permanent:

1. Go to [developers.facebook.com/tools/explorer](https://developers.facebook.com/tools/explorer).
2. Select your app from the top-right dropdown.
3. Click **Generate Access Token** → log in and grant all permissions listed above.
4. Copy the short-lived User Token.
5. **Exchange for a Long-Lived Token** (valid 60 days):
   ```
   GET https://graph.facebook.com/v21.0/oauth/access_token
     ?grant_type=fb_exchange_token
     &client_id=YOUR_APP_ID
     &client_secret=YOUR_APP_SECRET
     &fb_exchange_token=SHORT_LIVED_TOKEN
   ```
   You can run this in your browser URL bar or with curl.
6. **Get a Page Access Token** (never expires):
   ```
   GET https://graph.facebook.com/v21.0/me/accounts
     ?access_token=LONG_LIVED_USER_TOKEN
   ```
   In the response, find your Page. Its `access_token` field is a **non-expiring** Page token. Copy it.
7. **Get the Instagram User ID** linked to the Page:
   ```
   GET https://graph.facebook.com/v21.0/PAGE_ID?fields=instagram_business_account&access_token=PAGE_TOKEN
   ```
   The `instagram_business_account.id` value is your `IG_USER_ID`.

> **Tip:** You can do all of the above URL requests directly in your browser or use [Graph API Explorer](https://developers.facebook.com/tools/explorer/).

---

## Step 4: Get a Free Gemini API Key

1. Visit [aistudio.google.com](https://aistudio.google.com).
2. Sign in with your Google account.
3. Click **Get API Key** → **Create API key**.
4. Copy the key (it starts with `AIza…`).

The free tier allows 1,500 requests/day with Gemini 2.5 Flash — more than enough for 2 posts/day + 1 reel/week.

---

## Step 5: Add Secrets to GitHub

1. Go to your GitHub repo → **Settings** → **Secrets and variables** → **Actions**.
2. Click **New repository secret** and add each of the following:

| Secret name | Value |
|---|---|
| `IG_USER_ID` | Your Instagram Business account ID (the number from Step 3d) |
| `IG_ACCESS_TOKEN` | Your non-expiring Page access token (from Step 3d) |
| `GEMINI_API_KEY` | Your Gemini API key (from Step 4) |

3. *(Optional)* To override the Gemini model, go to **Variables** tab and add a variable `GEMINI_MODEL` with a value like `gemini-2.0-flash`.

---

## Step 6: Enable GitHub Pages (Dashboard)

1. Go to your repo → **Settings** → **Pages** (in the left sidebar).
2. Under **Source**, select **Deploy from a branch**.
3. Set **Branch** to `main` and **Folder** to `/docs`.
4. Click **Save**.
5. After about 2 minutes, your dashboard will be live at:
   ```
   https://YOUR_USERNAME.github.io/ig-influencer/
   ```

### Create the Dashboard Token

The dashboard needs a GitHub token to call the API from your browser:

1. Go to [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new) (fine-grained PAT).
2. Under **Resource owner**, select your account.
3. Under **Repository access**, choose **Only select repositories** → select `ig-influencer`.
4. Under **Permissions**, expand and set:
   - **Contents**: Read and write
   - **Actions**: Read and write
5. Set an expiration (maximum is 1 year; you'll need to rotate it annually).
6. Click **Generate token** and copy the value.
7. Open your dashboard URL, click ⚙ **Settings**, paste the token, enter your repo (`username/ig-influencer`), and click **Save**.

---

## Step 7: First Test

1. Go to your GitHub repo → **Actions** tab.
2. Click **Instagram Automation** in the left sidebar.
3. Click **Run workflow** → leave mode as `post` → click **Run workflow**.
4. Watch the run. It should take 2–5 minutes and complete successfully.
5. Check your dashboard — the **Recent Activity** table should show a new entry.
6. Check Instagram — a new photo should be posted!

---

## Step 8: (Optional) Add Royalty-Free Music for Reels

Drop `.mp3` files into the `assets/music/` folder and commit them. The weekly reel builder will randomly pick one. Good free sources:
- [pixabay.com/music](https://pixabay.com/music) (CC0)
- [soundcloud.com](https://soundcloud.com) (filter by Creative Commons)

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| `Error validating access token` | Token expired or wrong permissions | Regenerate the Page access token (Step 3d) |
| `(#10) Application does not have permission` | Missing API permission scope | Re-grant permissions in Graph API Explorer |
| `media_not_ready` / container stays in `IN_PROGRESS` | Image URL not yet accessible | The script waits up to 120s automatically; if it times out, increase `MEDIA_WAIT_TIMEOUT` in `main.py` |
| `Pollinations.ai failed after 5 attempts` | Image service temporarily down | Re-run the workflow manually; service usually recovers within minutes |
| Scheduled runs stop after ~60 days | GitHub disables cron workflows on inactive repos | Go to repo → Actions → re-enable workflows, OR push a small commit (edit a comment in any file) |
| Dashboard shows "Unable to load" | Token not set or expired | Open ⚙ Settings in dashboard and re-enter your fine-grained PAT |
| `git push` fails in Actions | Concurrent workflow runs conflict | The `concurrency` group in the workflow prevents this; if it happens, re-run the failed job |
| FFmpeg not found | Actions runner issue | Already handled: the workflow installs FFmpeg via `apt-get` before running `main.py` |
| Video published without audio | No `.mp3` in `assets/music/` | Add royalty-free .mp3 files to `assets/music/` and commit |

---

## Known Limitations

- **Face consistency**: Pollinations.ai uses a fixed seed per persona to keep the AI character recognizable across posts, but results are similar, not pixel-identical. This is an inherent limitation of free image generation.
- **No trending audio via API**: Instagram's API does not support attaching trending audio to Reels. The weekly reel uses your own royalty-free music from `assets/music/`. Audio must be uploaded with the video.
- **Professional account required**: Instagram posting via the Graph API requires a Business or Creator account linked to a Facebook Page. Personal accounts cannot use the Publishing API.
- **Development mode**: In Meta's Development mode, the app can only post to accounts that have been explicitly added as testers. This is fine for posting to your own account.
- **Free tier Gemini limits**: 1,500 requests/day. At 2 posts/day + 1 reel/week, you'll use ~3 requests/day — well within limits.
- **GitHub file size limit**: Uploading reels via the dashboard uses the GitHub Contents API with base64 encoding, which has a practical limit of ~50 MB. Larger files should be committed directly via git.
