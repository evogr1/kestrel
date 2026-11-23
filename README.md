# Kestrel

Your own vidIQ-style research agent for YouTube and Instagram, built for Claude.

Ask in plain English and Kestrel does the digging:

- **"Break down @someone's Instagram and write me a script."** Works for Instagram, YouTube and X. Kestrel reads their recent posts, ranks them by real views against *their own* normal (paid boosts are spotted and skipped), picks the one closest to your niche and audience, transcribes just that one, reads hooks, posting times and frequency, compares them with your page and hands Claude a brief to write your script from. In claude.ai and the Claude desktop app it shows as a live breakdown card in the chat.
- **Asks about you once.** The first time, Claude asks for your page, niche and target audience and saves them, so every breakdown is about what *you* should post.
- **Share any breakdown as an image.** One button turns the card into a clean 1080x1350 image ready to post.
- **"What's new on my list?"** Keep a watchlist of creators on Instagram, YouTube and X. Kestrel shows only what went viral since you last checked, with paid boosts marked.
- **"Why did my last reel flop?"** Your latest reel against your own usual: views, watch time, shares, saves, hook and posting time, then what to change.
- **"What's blowing up in my niche this week?"** Outliers: videos and reels doing many times their channel's usual.
- **"How is my latest video doing right now?"** Live views per hour on your own uploads, compared with your other videos at the same age.
- **Impressions and CTR**, retention dips, best time to post, title and thumbnail ideas from winners, keyword demand, competitors, rising channels, sponsors in a niche, and more. Ask Claude "what can Kestrel do?" for the full list.

X tools need the optional official X API token (`X_BEARER_TOKEN`, billed by X), or fall back to whatever public data X serves, which is limited. LinkedIn isn't supported: it has no API for reading other people's posts.

Kestrel is **free**: no credits, no paid tier. The only limits are YouTube's and Meta's own free quotas, and Kestrel caches answers and keeps its own daily budget so it stays inside them. When the YouTube budget runs out it falls back to `yt-dlp` (check that against YouTube's terms for your use). It runs entirely on **your own** API keys and your own machine or server. Nothing goes through anyone else's account, so your usage only ever counts against your own quotas.

---

## Connect Kestrel to your AI

For the hosted Kestrel server, share **https://scout.solodac.com/connect/**. Each person connects their own Instagram professional account and receives a unique, revocable MCP URL. They do not need to clone this repository or install anything locally. They still need to approve adding the connector in their own ChatGPT or Claude account.

The old shared `SCOUT_MCP_SECRET` connector route is disabled by default. Do not enable `SCOUT_ALLOW_SHARED_MCP` for a multi-user deployment.

## Pick how you run it

### Option A: Claude Code plugin

Needs Python 3.9+ on your machine (no packages to install).

```
/plugin marketplace add evogr1/kestrel
/plugin install kestrel@kestrel
```

Then say **"set up Kestrel"**. Claude checks what's working and walks you through anything missing, one step at a time, with links. Everything it needs is free (a YouTube key takes about 2 minutes, an Instagram token about 10).

Claude Code also asks for your keys when you enable it. Keys marked sensitive go to your system keychain, not to any file. Change them later in `/config`. Then just ask: *"Use Kestrel to find outlier Shorts about home repair from the last 30 days."*

### Option B: your own server, for claude.ai

```
git clone https://github.com/evogr1/kestrel kestrel && cd kestrel
cp .env.example .env        # fill it in
docker build -t kestrel .   # add --build-arg TRANSCRIBE=1 for reel transcription
docker run -d --name kestrel --restart unless-stopped -p 127.0.0.1:8799:8799 --env-file .env -v kestrel-data:/data kestrel
```

No Docker? `set -a; . ./.env; set +a; python3 scout_server.py` works too (Python 3.9+, standard library only; `pip install yt-dlp` is recommended).

Then put it behind HTTPS (a Cloudflare Tunnel is the easiest free option) and add a custom connector in claude.ai (Settings, Connectors, Add custom connector) with the URL:

```
https://<your-domain>/mcp/<your SCOUT_MCP_SECRET>
```

That URL is the key to your server. Don't post it, screenshot it or put it in a repo. If it leaks, change `SCOUT_MCP_SECRET` and restart.

**ChatGPT works too**, through the same server. In ChatGPT on the web: Settings, Apps and Connectors, Advanced settings, turn on Developer mode. Then create a connector, paste the same `https://<your-domain>/mcp/<your SCOUT_MCP_SECRET>` URL and choose no authentication. Every tool works, and the breakdown shows as the same live card (ChatGPT supports the MCP Apps standard Kestrel uses).

Optional cron jobs on the host (these use Docker; without Docker, run them from the repo folder with your `.env` loaded the same way as the server):

```
0 3 * * *     docker exec kestrel python3 scout_jobs.py daily     # outliers for your niches, hooks, changes
*/15 * * * *  docker exec kestrel python3 scout_jobs.py live      # live stats snapshots for views per hour
0 */6 * * *   docker exec kestrel python3 scout_jobs.py reach     # impressions and CTR reports
```

---

## Getting your keys

### YouTube Data API key (required, free)

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a project.
2. APIs and Services, Library: enable **YouTube Data API v3**.
3. APIs and Services, Credentials, Create credentials, **API key**. Restrict it to the YouTube Data API.

The free quota is 10,000 units a day. Kestrel caches answers, keeps its own daily budget (`SCOUT_YT_DAILY_UNITS`, default 8000) and falls back to `yt-dlp` when the budget runs out. For heavier use, add more keys from other projects as `YOUTUBE_API_KEY_2` and up.

### Instagram: each user's own account (OAuth)

The hosted multi-user server uses **Instagram Business Login**. Every person connects their own Instagram Business or Creator account at `/connect/`; Kestrel stores a separate encrypted long-lived token and issues a separate revocable MCP URL. A user's private Instagram token is never shared with another Kestrel user.

Configure these environment variables on the server:

- `SCOUT_IG_CLIENT_ID`: the **Instagram App ID** shown under the Instagram product's API setup (not necessarily the generic Meta App ID).
- `SCOUT_IG_CLIENT_SECRET`: the matching Instagram App Secret. Server-side only; never put it in the browser or repository.
- `SCOUT_IG_REDIRECT_URI`: exactly `https://scout.solodac.com/auth/instagram/callback` (also add this exact URI in Meta's Valid OAuth Redirect URIs).
- `SCOUT_PUBLIC_URL=https://scout.solodac.com`
- `SCOUT_TOKEN_ENCRYPTION_KEY`: generate once with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Keep it stable and back it up securely; losing it makes stored Instagram tokens unreadable.

The Docker image installs `cryptography`. Keep the persistent `/data` volume: it stores the per-user auth database. Do not rotate the encryption key unless you first re-encrypt every stored token.

Kestrel requests `instagram_business_basic`, `instagram_business_manage_insights`, `instagram_business_content_publish`, `instagram_business_manage_comments` and `instagram_business_manage_messages`. These cover profile/media, insights, publishing, comments and messaging. Meta must grant the relevant access level and App Review approval before external customers can use restricted permissions. An Instagram Login token is used with `graph.instagram.com`; it is not interchangeable with the older Facebook Login token on `graph.facebook.com`. The current Kestrel tools actively use profile/media reads and insights; publishing, comment-management and messaging tools are not exposed yet, so those additional scopes are requested in preparation for the next feature phase and must be justified with matching product functionality in Meta App Review.

The per-user auth key is embedded in that user's MCP URL. It is shown once after connection, stored only as a hash, and can be revoked by reconnecting (rotates the key) or disconnecting.

### Public Instagram creator research (Business Discovery)

Kestrel's public creator breakdown still uses the separately configured shared Business Discovery token pool. Business Discovery is a distinct API path from Instagram Login. Configure `SCOUT_IG_TOKEN` and `SCOUT_IG_USER_ID` or `SCOUT_IG_POOL` with an eligible Business/Creator account and the Facebook Login permissions required by Meta's Business Discovery API. Do not use a user's Instagram Login token for public Business Discovery.

### Your own YouTube channel (optional, for live stats, retention, impressions and CTR)

1. In the same Google Cloud project, enable **YouTube Analytics API** and **YouTube Reporting API**.
2. Credentials, Create credentials, **OAuth client ID**, type **Desktop app**. Download the JSON and save it as `~/.scout/youtube_oauth/youtube_client.json` (on your own machine).
3. Run `python3 kestrel_connect_youtube.py mychannel` on a machine with a browser (clone the repo), sign in and pick the channel. Repeat for each channel, each with its own short name. Running a server? Copy the resulting `youtube_client.json` and `youtube_token_*.json` files into `/data/.scout/youtube_oauth/` in the container.

Kestrel only asks for read-only scopes. It never uploads, edits or deletes anything.

---

## Reel transcription (off by default)

Creator breakdowns can transcribe the picked reel word for word, which makes the script much better. It downloads the reel's audio with `yt-dlp` and runs Whisper on your machine. Instagram's terms don't allow downloading, and it usually needs a logged-in `cookies.txt`, so this is **off unless you turn it on** (`SCOUT_TRANSCRIBE=1`, plus `pip install faster-whisper`, plus `SCOUT_IG_COOKIES`). If you do, use a spare account for the cookies. Without it, Kestrel works from the caption and hook.

---

## Settings

Everything is an environment variable (the `SCOUT_` prefix is from the project's earlier name); `.env.example` lists the common ones.

| Variable | What it does |
|---|---|
| `YOUTUBE_API_KEY`, `YOUTUBE_API_KEY_2..9` | YouTube Data API keys, each with its own daily budget |
| `SCOUT_YT_DAILY_UNITS` | Units Kestrel may spend per key per day (default 8000) |
| `SCOUT_YT_CALLER_PCT` | Most of the total one caller (job, chat) may use (default 30) |
| `SCOUT_IG_CLIENT_ID`, `SCOUT_IG_CLIENT_SECRET`, `SCOUT_IG_REDIRECT_URI` | Instagram Business Login OAuth credentials and callback |
| `SCOUT_IG_GRAPH_VERSION` | Graph API version for Instagram Login (default v27.0) |
| `SCOUT_TOKEN_ENCRYPTION_KEY`, `SCOUT_AUTH_DB` | Encryption key and per-user auth database |
| `SCOUT_IG_TOKEN`, `SCOUT_IG_USER_ID` | Legacy/shared Business Discovery token and account id |
| `SCOUT_IG_POOL` | JSON file with several tokens: `[{"label": "app1", "token": "...", "ig_user_id": "..."}]` |
| `SCOUT_IG_PER_HOUR` | Instagram calls per hour per Meta app (default 120). Per app: `SCOUT_IG_PER_HOUR_<LABEL>` |
| `SCOUT_SHOWCASE_ME`, `SCOUT_SHOWCASE_CTA` | Your Instagram to compare with, and the handle on the breakdown's end card |
| `SCOUT_TRANSCRIBE`, `SCOUT_IG_COOKIES`, `SCOUT_WHISPER_MODEL` | Reel transcription, see above |
| `SCOUT_PAGE_RULES` | Per-page filters for the daily brief (see `page_rules.example.json`) |
| `SCOUT_TRELLO_KEY`, `SCOUT_TRELLO_TOKEN`, `SCOUT_TRELLO_LISTS` | Push the daily brief to Trello lists |
| `SCOUT_API_KEY`, `SCOUT_MCP_SECRET`, `SCOUT_HOST`, `SCOUT_PORT` | Server lock, claude.ai connector path, bind address |
| `SCOUT_DB`, `SCOUT_YT_OAUTH_DIR` | Where the cache and your channel sign-ins live |
| `SCOUT_TZ` | Your time zone for posting-time advice, e.g. `Asia/Kolkata`, `America/New_York` (default UTC) |
| `X_BEARER_TOKEN` | Optional. Official X API token for X tools. X bills this one (pay per use); everything else is free |

---

## REST API

The server also answers plain HTTP with the `X-Scout-Key: <SCOUT_API_KEY>` header, for scripts and other agents. `GET /v1/tools` lists every route with its parameters. Examples:

```
curl -H "X-Scout-Key: $SCOUT_API_KEY" "http://127.0.0.1:8799/v1/outliers?query=home+repair&kind=shorts"
curl -H "X-Scout-Key: $SCOUT_API_KEY" "http://127.0.0.1:8799/v1/ig/breakdown?user=somecreator&me=myaccount"
```

`GET /breakdown?user=<handle>` renders the animated breakdown as a full page, 9:16, ready to screen-record (press R to replay). It needs the key header like every route, so use it from a tool that can send headers, or put the server behind an access proxy you trust and set `SCOUT_DASHBOARD_TRUST_CF=1` (only do that if the port is reachable through that proxy alone).

## Tests

```
python3 test_scout.py
```

No network needed; every API is faked.

## Credits and license

Made by [@harshilrekhi](https://instagram.com/harshilrekhi). MIT license, see `LICENSE`. Not affiliated with YouTube, Instagram, Meta, Google, vidIQ or Anthropic. You are responsible for using your keys within each platform's terms.
