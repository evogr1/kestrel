#!/usr/bin/env python3
"""Kestrel MCP server (stdio).

Two modes:
  Local (no SCOUT_URL set): runs Kestrel in this process with your own keys from the environment. Nothing else to host.
      YOUTUBE_API_KEY=...  python3 scout_mcp.py
  Remote (SCOUT_URL set): a thin wrapper around a Kestrel server you host yourself.
      SCOUT_URL=https://kestrel.example.com  SCOUT_API_KEY=...  python3 scout_mcp.py
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("SCOUT_URL", "http://127.0.0.1:8799").rstrip("/")
KEY = os.environ.get("SCOUT_API_KEY", "")
CALLER_NAME = os.environ.get("SCOUT_CALLER", "mcp")   # sent as X-Scout-Caller so the YouTube share is per job
MAX_CHARS = 60000

S = lambda d, **kw: {"type": "string", "description": d, **kw}
I = lambda d: {"type": "integer", "description": d}
N = lambda d: {"type": "number", "description": d}
B = lambda d: {"type": "boolean", "description": d}

TOOLS = [
    ("scout_outliers", "/v1/outliers",
     "Find YouTube videos (long or Shorts) that got many times their own channel's median views. Use a query (niche keywords) and or a list of channels.",
     {"query": S("Niche keywords, for example 'fix shower drain'"),
      "channels": S("Comma separated channel handles, ids or URLs to scan directly"),
      "kind": S("shorts, long or any", enum=["shorts", "long", "any"]),
      "max_age_days": I("Only videos newer than this. Default 90"),
      "min_multiple": N("Minimum views divided by channel median. Default 2"),
      "min_subs": I("Smallest channel size (needs YOUTUBE_API_KEY)"), "max_subs": I("Largest channel size (needs YOUTUBE_API_KEY)"),
      "on_topic": B("Only keep videos whose title matches the query. Default true"),
      "limit": I("Max results. Default 30")}),
    ("scout_channel_stats", "/v1/channel",
     "Stats for one YouTube channel: subscribers, median and best views, upload cadence, hit rate, top and latest videos. Shows what works there and how consistent it is.",
     {"ref": S("Handle, channel id or URL"), "kind": S("shorts, long or any", enum=["shorts", "long", "any"]), "n": I("How many recent uploads to sample. Default 30")}),
    ("scout_keyword_demand", "/v1/keywords",
     "Demand against supply for a YouTube keyword and its autocomplete variants. Estimates, comparable between terms. Higher opportunity means more views per day for fewer competing videos.",
     {"seed": S("Seed keyword"), "terms": I("How many terms to check, seed included. Default 6"), "kind": S("shorts, long or any", enum=["shorts", "long", "any"])}),
    ("scout_video", "/v1/video",
     "One video's stats, thumbnail URLs (max, high, three frame stills) and optionally its transcript.",
     {"ref": S("Video URL or id"), "with_transcript": B("Include the transcript"), "lang": S("Caption language. Default en")}),
    ("scout_transcript", "/v1/transcript",
     "Timed transcript of a YouTube video, with the first 30 seconds split out as the hook and words per minute.",
     {"ref": S("Video URL or id"), "lang": S("Caption language. Default en")}),
    ("scout_ig_outliers", "/v1/ig/outliers",
     "Instagram Reels that beat their own account's median likes, from Business or Creator accounts. Give users or a seeded niche.",
     {"users": S("Comma separated Instagram usernames"), "niche": S("A niche name from the seeds file"),
      "min_multiple": N("Minimum likes divided by account median. Default 1.5"), "max_age_days": I("Default 60"), "limit": I("Default 30")}),
    ("scout_ig_profile", "/v1/ig/profile",
     "One Instagram Business or Creator account: followers, median reel likes, best and top reels. For 'scrape / break down / what's working for' a creator, use scout_creator_breakdown instead.",
     {"user": S("Instagram username")}),
    ("scout_usage", "/v1/usage", "Today's YouTube API units used and Instagram token health. Check before a big run.", {}),
    ("scout_feed", "/v1/feed",
     "Today's research feed: new outliers per saved niche, breakout channels and competitor alerts. Start here every morning.",
     {"niche": S("Only this niche"), "only_new": B("Only items not seen on earlier days"), "kind": S("youtube, youtube_shorts or instagram"),
      "day": S("YYYY-MM-DD, default latest"), "limit": I("Default 50")}),
    ("scout_niches", "/v1/niches", "List the niches the daily feed watches.", {}),
    ("scout_niche_add", "/v1/niches/add",
     "Save a niche for the daily feed. A niche can be a search query, a fixed list of YouTube channels, Instagram accounts and X handles, or a mix. Saving again with the same name replaces it.",
     {"name": S("Short name, for example demo"), "query": S("Optional search query for this niche"),
      "channels": S("Comma separated YouTube channels (handles, ids or URLs), up to 40"),
      "ig_users": S("Comma separated Instagram usernames, up to 30 (Business or Creator accounts)"), "kind": S("shorts, long or any", enum=["shorts", "long", "any"]),
      "max_age_days": I("Default 30"), "min_multiple": N("Default 3"), "breakout_max_subs": I("Largest channel counted as a breakout. Default 100000"),
      "ig_niche": S("Optional Instagram seed niche name to include"),
      "x_users": S("Optional comma separated X handles (read from your X scroll bot)"),
      "linkedin_users": S("Optional comma separated LinkedIn profile or company names (from your LinkedIn bot)"),
      "subreddits": S("Optional comma separated subreddits (from your Reddit bot)")}),
    ("scout_niche_remove", "/v1/niches/remove", "Stop watching a niche.", {"name": S("Niche name")}),
    ("scout_breakouts", "/v1/breakouts",
     "Small channels whose recent videos get far more views than their subscriber count. Good for spotting formats YouTube is pushing.",
     {"query": S("Niche keywords"), "kind": S("shorts, long or any", enum=["shorts", "long", "any"]), "max_subs": I("Default 100000"),
      "min_median_views": I("Default 1000"), "max_age_days": I("Default 90"), "limit": I("Default 15")}),
    ("scout_competitors", "/v1/competitors",
     "Tracked channels with subscriber and view changes (1, 7, 30 days) and their new outliers.", {"days": I("Alert window. Default 7")}),
    ("scout_track_add", "/v1/track/add", "Start tracking a competitor channel.", {"ref": S("Handle, channel id or URL")}),
    ("scout_track_remove", "/v1/track/remove", "Stop tracking a channel.", {"ref": S("Handle, channel id, URL or title")}),
    ("scout_wins", "/v1/wins",
     "What wins in a niche: title patterns and words that winners use more than the rest, title length, video length, top tags, "
     "thumbnail light and color, and the winning examples.",
     {"query": S("Niche keywords"), "kind": S("shorts, long or any", enum=["shorts", "long", "any"]), "max_age_days": I("Default 180"),
      "win_multiple": N("A winner beats its channel median by this much. Default 3"), "on_topic": B("Default true"),
      "with_thumbs": B("Also compare thumbnail brightness, saturation and colors"), "limit": I("Examples to return. Default 12")}),
    ("scout_similar_thumbnails", "/v1/thumbnails/similar",
     "Thumbnails that look like a given video's thumbnail (layout, light, color), from the feed and an optional query.",
     {"ref": S("Video URL or id"), "query": S("Also compare against this niche"), "kind": S("shorts, long or any"), "limit": I("Default 12")}),
    ("scout_optimize", "/v1/optimize",
     "Audit one video against its niche's winners: title, keyword, patterns, tags, description, length, thumbnail, and how it did against its channel. Returns a score and fixes.",
     {"ref": S("Video URL or id"), "query": S("Niche keywords. Default: taken from the title")}),
    ("scout_timing", "/v1/timing", "Best weekdays and hours to post, from upload history of channels or a niche.",
     {"channels": S("Comma separated channels"), "query": S("Or a niche query"), "kind": S("shorts, long or any"), "tz": S("Default: SCOUT_TZ")}),
    ("scout_calendar", "/v1/calendar", "A posting plan for the next days with a proven outlier to remix in each slot.",
     {"niche": S("Saved niche name"), "channels": S("Comma separated channels"), "query": S("Niche query"), "days": I("Default 14"),
      "per_week": I("Default 2"), "kind": S("shorts, long or any"), "tz": S("Default: SCOUT_TZ")}),
    ("scout_ideas", "/v1/ideas",
     "Idea seeds: proven outliers plus what wins in the niche, with the three-angle rules. You write the angles, the pick and why.",
     {"niche": S("Saved niche name"), "query": S("Or a query"), "kind": S("shorts, long or any"), "n": I("Default 8"),
      "with_hooks": B("Add each source's first 30 seconds of transcript")}),
    ("scout_run_daily", "/v1/jobs/daily", "Run the daily job now: refresh the feed, breakouts and competitor snapshots. Uses YouTube units.",
     {"only": S("Comma separated niche names to run, default all")}),
    ("scout_my_channels", "/v1/me/channels", "Your own connected YouTube channels and Instagram accounts, with names to use in the other my_ tools.", {}),
    ("scout_my_race", "/v1/me/race",
     "Race to monetization for one of your channels: every YPP path (long-form hours, Shorts views, early tier), progress, pace and days to go.",
     {"channel": S("Your channel name from scout_my_channels, for example demo")}),
    ("scout_my_scorecard", "/v1/me/scorecard",
     "Last 28 days against the 28 before for one of your channels: views, watch hours, retention, net subs, engagement, uploads, a score and grade, top videos, traffic sources.",
     {"channel": S("Your channel name")}),
    ("scout_my_achievements", "/v1/me/achievements", "Milestones reached and the next one for subscribers, lifetime views and best video.",
     {"channel": S("Your channel name")}),
    ("scout_my_audience", "/v1/me/audience",
     "Who watches one of your channels and how they find it: age, gender, countries, subscribed vs not, devices, traffic sources (search, suggested, browse, Shorts feed...), top YouTube search terms, and which videos bring the most subscribers. Sections YouTube has no data for come back empty with a reason. Impressions and CTR: scout_my_reach.",
     {"channel": S("Your channel name"), "days": I("Default 28")}),
    ("scout_my_ig", "/v1/me/ig",
     "Your own Instagram insights: reach, views, interactions, follower demographics, and per-reel views, saves, shares and watch time.",
     {"account": S("Username or label. Default: all connected accounts"), "days": I("Default 28, max 30"), "reels": I("Reels to read. Default 25")}),
    ("scout_disconnect_instagram", "/v1/me/ig/disconnect", "Disconnect your Instagram account from Kestrel. Removes the stored Meta access token and revokes every Kestrel OAuth session for this identity.", {}),
    ("scout_x_post", "/v1/x/post", "Free stats for one public X post: likes, replies, video views, author, age.", {"ref": S("Post URL or id")}),
    ("scout_x_posts", "/v1/x/posts", "Free stats for up to 20 X posts at once.", {"refs": S("Comma separated post URLs or ids")}),
    ("scout_x_profile", "/v1/x/profile", "An X account's followers and post count (X API, about $0.01).", {"user": S("X handle")}),
    ("scout_x_outliers", "/v1/x/outliers",
     "X posts that beat their own account's median views (X API, about $0.005 per post read, capped daily).",
     {"users": S("Comma separated X handles"), "min_multiple": N("Default 2"), "max_age_days": I("Default 30"), "limit": I("Default 30")}),
    ("scout_x_search", "/v1/x/search",
     "Last 7 days of X posts for a query, ranked by views per follower (X API, about $0.005 per post read).",
     {"query": S("Search query"), "limit": I("Default 30")}),
    ("scout_social_outliers", "/v1/social/outliers",
     "Outlier posts on X, LinkedIn or Reddit from what your scroll bots collected: posts beating the same author's, subreddit's or page's median. Free.",
     {"platform": S("x, linkedin or reddit", enum=["x", "linkedin", "reddit"]), "authors": S("Comma separated authors to limit to"),
      "communities": S("Comma separated subreddits, or page names for X and LinkedIn (for example some-page-name)"),
      "by": S("author or community. Default: subreddit for Reddit; author for X and LinkedIn, or the page when no author has 5 posts yet", enum=["author", "community"]), "min_multiple": N("Default 2"), "max_age_days": I("Default 30"), "limit": I("Default 30")}),
    ("scout_social_search", "/v1/social/search", "Search every post your scroll bots saw on X, LinkedIn or Reddit by words (typo tolerant), newest 30 days by default, best first.",
     {"query": S("What to look for, for example 'Jensen Huang board' or 'IPO lock-in'"), "platform": S("x, linkedin or reddit (default all)", enum=["x", "linkedin", "reddit"]),
      "communities": S("Comma separated subreddits, or page names for X and LinkedIn (for example some-page-name)"), "days": I("Default 30"), "limit": I("Default 30, max 100")}),
    ("scout_social_top", "/v1/social/top", "Top posts your scroll bots saw on X, LinkedIn or Reddit in the last days, by a metric.",
     {"platform": S("x, linkedin or reddit", enum=["x", "linkedin", "reddit"]), "days": I("Default 7"),
      "metric": S("views, likes, reposts, comments or saves"), "limit": I("Default 30"), "authors": S("Comma separated authors"),
      "communities": S("Comma separated subreddits")}),
    ("scout_my_fb_pages", "/v1/me/fb/pages", "Your Facebook Pages with followers.", {}),
    ("scout_my_fb", "/v1/me/fb",
     "One of your Facebook Pages: insights, posts and reels with plays and watch time, and which ones beat the Page's median.",
     {"page": S("Page name or id"), "days": I("Default 28")}),
    ("scout_ranked", "/v1/ranked",
     "Today's feed merged across niches with duplicates removed and an adjusted multiple that can't be inflated by a tiny median. Use this instead of scout_feed for ranking.",
     {"niche": S("Only this niche"), "kind": S("youtube, youtube_shorts, instagram, x, linkedin or reddit"), "day": S("YYYY-MM-DD"), "limit": I("Default 60")}),
    ("scout_brief", "/v1/brief",
     "Morning brief: the best new idea cards per page (niche), ready to turn into posts. Read only, nothing is pushed.",
     {"per_page": I("Cards per page. Default 5"), "min_multiple": N("Minimum adjusted multiple. Default 3"), "day": S("YYYY-MM-DD")}),
    ("scout_brief_push", "/v1/brief/push",
     "Build the morning brief and push the cards to Trello (needs SCOUT_TRELLO_* on the server). Cards already sent are skipped.",
     {"per_page": I("Default 5"), "min_multiple": N("Default 3"), "push": B("Must be true to push")}),
    ("scout_watch", "/v1/watch",
     "Run the early-breakout check now: reels and videos under 24 to 48 hours old that are already far above their account's usual.", {}),
    ("scout_early_alerts", "/v1/alerts/early",
     "Early breakouts found in the last hours. These are the 'post your version today' items.",
     {"hours": I("Default 48"), "niche": S("Only this niche")}),
    ("scout_hooks", "/v1/hooks",
     "Library of opening lines from outliers, with hook type (question, number, warning, secret, breaking, story, you, how-to, statement) and how each type performs.",
     {"niche": S("Only this niche"), "kind": S("youtube, youtube_shorts or instagram"), "hook_type": S("Only this hook type"), "limit": I("Default 50")}),
    ("scout_stories", "/v1/stories",
     "Stories showing up on several platforms at once (X, Reddit, LinkedIn, Instagram, YouTube). Best for fast news reels.",
     {"hours": I("Look back this many hours. Default 24"), "min_platforms": I("Default 2"), "limit": I("Default 20")}),
    ("scout_my_postmortem", "/v1/me/postmortem",
     "How each of my recent reels did against my median and what likely drove it (shares, saves, watch time, slot, hook type).",
     {"account": S("Instagram username or pool label"), "days": I("Reels from the last N days. Default 14")}),
    ("scout_my_weekly", "/v1/me/weekly",
     "Weekly scorecard for my Instagram accounts (hits, flops, best slot, best hook type). Also sent to the alert webhook if set.",
     {"account": S("One account, default all")}),
    ("scout_comments", "/v1/comments",
     "Mine comments on outlier YouTube videos for viewer questions and requests. Each question is a video idea.",
     {"ref": S("One video URL or id"), "niche": S("Or take the top YouTube outliers from this niche"), "videos": I("Default 5"), "n": I("Comments per video, max 100")}),
    ("scout_my_besttime", "/v1/me/besttime",
     "Best hours to post (your SCOUT_TZ) for one of my Instagram accounts, from when followers are online and how my past reels did by hour.",
     {"account": S("Instagram username or pool label")}),
    ("scout_ig_seed_outliers", "/v1/ig/seed_outliers",
     "Instagram outliers for the autopilot seed groups (~/.scout/ig_seeds.json) through the token pool, no cookies needed. Ranked on likes.",
     {"group": S("One seed group, default all"), "min_multiple": N("Default 2"), "max_age_days": I("Default 14"), "limit": I("Default 40"),
      "max_users": I("Accounts read per group. Default 40")}),
    ("scout_clips", "/v1/clips",
     "Clip finder: the most-replayed moments of a long YouTube video (podcast, talk, interview) with timestamps, links and transcript. Free, no quota.",
     {"ref": S("YouTube URL or id"), "n": I("How many clips. Default 8"), "clip_seconds": I("Clip length. Default 45"),
      "min_gap": I("Seconds between clips. Default 60")}),
    ("scout_my_retention", "/v1/me/retention",
     "Where viewers leave my own videos: still watching at 0:30 and halfway, the biggest drops with timestamps, and what to fix.",
     {"channel": S("My channel name (see scout_my_channels)"), "video": S("One video URL or id; default my latest uploads"), "n": I("Latest uploads to read. Default 5")}),
    ("scout_gaps", "/v1/gaps",
     "Topic gaps: searches people still watch a lot where the top results are old. The easiest videos to win. Costs one search per term.",
     {"seed": S("A search, for example 'unclog drain'"), "niche": S("Or use a niche's query or its outlier titles"), "terms": I("Default 6"),
      "kind": S("shorts, long or any", enum=["shorts", "long", "any"]), "fresh_days": I("What counts as fresh. Default 90")}),
    ("scout_changes", "/v1/changes",
     "Title and thumbnail changes on tracked and niche channels, with views at the change and views per day since. Learn from their A/B tests.",
     {"days": I("Default 30")}),
    ("scout_rising", "/v1/rising",
     "Words and names climbing across every feed and scroll bot this week against last week. Catch a wave before it peaks.",
     {"days": I("Period length. Default 7"), "min_count": I("Default 5"), "limit": I("Default 25")}),
    ("scout_sponsors", "/v1/sponsors",
     "Brands sponsoring videos in a niche: named sponsors, promo codes and linked brand domains from video descriptions. Useful as outreach leads.",
     {"niche": S("Only this niche"), "days": I("Default 60"), "limit": I("Default 30")}),
    ("scout_my_live", "/v1/me/live",
     "LIVE numbers for my latest uploads, minutes old (like vidIQ): views, likes, comments, views per hour right now, and how the video compares with my other uploads at the same age. Use this for anything about a video posted in the last 3 days; the Analytics-based tools lag 1 to 3 days.",
     {"channel": S("My channel name (see scout_my_channels)"), "n": I("Latest uploads. Default 10")}),
    ("scout_my_reach", "/v1/me/reach",
     "IMPRESSIONS and click-through rate (CTR) for my videos, the numbers Studio shows: per video, per traffic source (browse, suggested, search, Shorts feed...), per device and per day. From YouTube's reach reports, about 1 to 2 days behind.",
     {"channel": S("My channel name (see scout_my_channels)"), "video": S("One video URL or id"), "days": I("Default 28")}),
    ("kestrel_setup", "/v1/setup",
     "Guided setup and health check. Call it FIRST when someone is new to Kestrel, asks how to set it up, or any Kestrel tool says a key or "
     "token is missing or rejected. It tests every part (YouTube key, Instagram token, own channel, transcription, their profile) and returns "
     "plain steps to fix each missing one. Walk the user through ONE item at a time as a short numbered list, then call this again to confirm. "
     "Kestrel itself is free. Only the optional X API is billed by X.",
     {}),
    ("scout_whats_new", "/v1/watchlist/new",
     "What went viral on the user's watchlist since they last checked (each post against that creator's own usual). Use for 'what's new', "
     "'anything blow up today', 'check my list'. Paid boosts are marked; never suggest copying those. Offer a creator breakdown on the best hit.",
     {"min_multiple": N("Only posts at least this many times the creator's usual. Default 2"), "platform": S("instagram, youtube or x (default all)"),
      "limit": I("Most hits to return. Default 15")}),
    ("scout_watchlist", "/v1/watchlist", "The creators on the user's watchlist.", {}),
    ("scout_watchlist_add", "/v1/watchlist/add", "Add a creator to the watchlist so scout_whats_new checks them.",
     {"handle": S("Their username, no @ (YouTube: @handle or channel URL)"), "platform": S("instagram (default), youtube or x")}),
    ("scout_watchlist_remove", "/v1/watchlist/remove", "Remove a creator from the watchlist.",
     {"handle": S("Their username"), "platform": S("instagram (default), youtube or x")}),
    ("scout_my_reel_check", "/v1/me/reel_check",
     "Why the user's own latest Instagram reel popped or flopped: views against their usual, watch time, shares, saves and comments per view, "
     "hook type and posting time against what usually works for them, then up to 3 concrete fixes. Use for 'why did my reel flop', "
     "'how did my last reel do'. Lead with the verdict and the single biggest reason.",
     {"reel": S("A reel URL to check instead of the latest one"), "me": S("Instagram account, default their saved page")}),
    ("scout_my_profile", "/v1/me/profile",
     "Who the user is: their pages, niche, target audience, goal and language. Check it before the first creator breakdown in a chat. "
     "If set_up is false, ask the user for the missing items in ONE short friendly message, then save the answers with scout_my_profile_set.",
     {}),
    ("scout_my_profile_set", "/v1/me/profile/set",
     "Save who the user is, so every breakdown compares with their page and writes scripts for their niche and audience. "
     "Only pass what they told you; anything left out stays as it was.",
     {"instagram": S("Their Instagram username"), "youtube": S("Their YouTube @handle or channel"), "x": S("Their X handle"),
      "niche": S("What their page is about, in a few words"), "audience": S("Who they make content for"),
      "goal": S("What they want: followers, leads, sales, ..."), "language": S("Language they post in")}),
    ("scout_creator_breakdown", "/v1/breakdown",
     "Use this whenever the user asks to scrape, break down, analyse or find what's working for any creator on Instagram, YouTube or X "
     "(Twitter). One call per creator and platform: for two creators, call it twice. It opens their profile, pulls their recent posts, ranks the "
     "viral ones against the creator's own usual, picks the ONE post that is both very viral and closest to MY topics, transcribes it (reels and "
     "videos), reads hooks, posting times, frequency and topics, compares with MY Instagram (the one set as my_instagram / SCOUT_SHOWCASE_ME, unless the user names another) and "
     "writes what I should do. Before the first breakdown in a chat, check scout_my_profile; if it isn't set up, ask the user for "
     "their page, niche and target audience in one short message and save them with scout_my_profile_set (or just run the breakdown and "
     "ask if 'setup_needed' comes back). The chat shows all of this as a live breakdown card, so don't repeat its steps or numbers. After each call write: "
     "(1) one line on why that post works, (2) MY SCRIPT from 'script_brief', following its 'how_to_write' (their structure and hook type, my topic "
     "and voice, never their lines): a reel script as short spoken lines with rough timings, or for X a ready-to-post post, (3) two alternative "
     "hooks, (4) the top move from 'what_you_should_do'. Plain English, no em-dashes. If the user gives a name instead of a handle, use their "
     "likely username on that platform (Raj Shamani is rajshamani on Instagram, Ankur Warikoo is ankurwarikoo). LinkedIn isn't supported.",
     {"user": S("Their username or handle, no @ (for YouTube a @handle, channel URL or channel id)"),
      "platform": S("instagram (default), youtube or x"),
      "me": S("My Instagram account to compare with. Defaults to SCOUT_SHOWCASE_ME"), "top": I("Viral posts to show. Default 5"),
      "transcribe": I("Posts to transcribe, starting with the picked one. Default 1 (fastest). 0 skips transcription")}),
]
REQUIRED = {"scout_channel_stats": ["ref"], "scout_keyword_demand": ["seed"], "scout_video": ["ref"], "scout_transcript": ["ref"],
            "scout_ig_profile": ["user"], "scout_niche_add": ["name"], "scout_niche_remove": ["name"], "scout_breakouts": ["query"],
            "scout_track_add": ["ref"], "scout_track_remove": ["ref"], "scout_wins": ["query"], "scout_similar_thumbnails": ["ref"],
            "scout_optimize": ["ref"], "scout_my_race": ["channel"], "scout_my_scorecard": ["channel"],
            "scout_my_achievements": ["channel"], "scout_my_audience": ["channel"], "scout_x_post": ["ref"], "scout_x_posts": ["refs"],
            "scout_x_profile": ["user"], "scout_x_outliers": ["users"], "scout_x_search": ["query"], "scout_my_fb": ["page"], "scout_my_postmortem": ["account"], "scout_my_besttime": ["account"], "scout_clips": ["ref"], "scout_my_retention": ["channel"], "scout_my_live": ["channel"], "scout_my_reach": ["channel"], "scout_creator_breakdown": ["user"]}
POST = {"/v1/me/ig/disconnect", "/v1/watchlist/add", "/v1/watchlist/remove", "/v1/me/profile/set", "/v1/me/reach/sync", "/v1/brief/push", "/v1/watch", "/v1/hooks/extract", "/v1/me/weekly", "/v1/ingest", "/v1/niches/add", "/v1/niches/remove", "/v1/track/add", "/v1/track/remove", "/v1/jobs/daily"}


SETUP_NOTE = ("If this is someone's first time with Kestrel, or a tool says a key is missing, run kestrel_setup and walk them through "
              "what it returns, one step at a time. ")
GATEWAY_NOTE = ("Kestrel gains new tools over time and this chat's tool list can be older than the server. If you need something "
                "that isn't in your list (for example live video stats, retention, clips, gaps), call kestrel_tools to see every tool "
                "the server has right now, then run it with kestrel_call. Never tell the user to reconnect for a missing Kestrel tool.")
GATEWAY = [
    {"name": "kestrel_tools", "description": "List every Kestrel tool the live server offers right now, with parameters. " + GATEWAY_NOTE,
     "inputSchema": {"type": "object", "properties": {"search": S("Optional word to filter by, e.g. 'live' or 'retention'")}, "required": []}},
    {"name": "kestrel_call", "description": "Run any Kestrel tool by name, including ones newer than this chat's tool list. "
                                            "Get names and parameters from kestrel_tools.",
     "inputSchema": {"type": "object", "properties": {"tool": S("Tool name from kestrel_tools, e.g. scout_my_live"),
                                                      "args": {"type": "object", "description": "The tool's parameters"}}, "required": ["tool"]}},
]


def catalog():
    """Every tool with its route, method and schema. Served by the API at /v1/tools so old clients can read the current list."""
    return [{"name": n, "route": r, "method": "POST" if r in POST else "GET", "description": d,
             "inputSchema": {"type": "object", "properties": p, "required": REQUIRED.get(n, [])}} for n, r, d, p in TOOLS]


UI = {"scout_creator_breakdown": "ui://kestrel/breakdown.html"}  # tools Claude chats render as an MCP App


def ui_meta(name):
    """Links a tool to its card. ui.resourceUri is the MCP Apps standard (Claude, ChatGPT); the openai/* keys are ChatGPT's
    aliases and the status lines it shows while the tool runs."""
    uri = UI.get(name)
    if not uri:
        return None
    return {"ui": {"resourceUri": uri}, "ui/resourceUri": uri, "openai/outputTemplate": uri,
            "openai/toolInvocation/invoking": "Reading their posts", "openai/toolInvocation/invoked": "Breakdown ready"}


def tool_list(cat):
    out = []
    for t in cat:
        d = {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        if ui_meta(t["name"]):
            d["_meta"] = ui_meta(t["name"])
        out.append(d)
    return out + GATEWAY


def filter_catalog(cat, search=None):
    if not search:
        return cat
    w = str(search).lower()
    return [t for t in cat if w in t["name"].lower() or w in t["description"].lower()]


def call(route, args):
    args = {k: v for k, v in args.items() if v not in (None, "")}
    if route in POST:
        req = urllib.request.Request(URL + route, data=json.dumps(args).encode("utf8"), method="POST",
                                     headers={"X-Scout-Key": KEY, "X-Scout-Caller": CALLER_NAME, "Content-Type": "application/json"})
    else:
        q = urllib.parse.urlencode({k: ("true" if v is True else "false" if v is False else v) for k, v in args.items()})
        req = urllib.request.Request(URL + route + ("?" + q if q else ""), headers={"X-Scout-Key": KEY, "X-Scout-Caller": CALLER_NAME})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.read().decode("utf8"), False
    except urllib.error.HTTPError as e:
        return e.read().decode("utf8", "ignore"), True
    except Exception as e:
        return json.dumps({"error": "cannot reach Scout: " + str(e)}), True


def reply(i, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": i}
    m["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(m) + "\n")
    sys.stdout.flush()


def live_catalog():
    """The server's current tool list (so this wrapper is never older than the server). Falls back to the built-in list."""
    try:
        req = urllib.request.Request(URL + "/v1/tools", headers={"X-Scout-Key": KEY, "X-Scout-Caller": CALLER_NAME})
        with urllib.request.urlopen(req, timeout=15) as r:
            cat = json.loads(r.read().decode("utf8")).get("tools")
            if cat:
                return cat
    except Exception:
        pass
    return catalog()


def notify(method):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
    sys.stdout.flush()


def main_local():
    """Run every tool in process, through the same code path the hosted /mcp endpoint uses."""
    import scout_core
    import scout_remote_mcp
    import scout_server
    from scout_core import Config
    from scout_extras import Kestrel
    scout = Kestrel(Config())
    scout_core.CALLER.name = CALLER_NAME
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        out = scout_remote_mcp.handle(scout, scout_server.ROUTES, scout_server._conv, msg)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def main():
    if not os.environ.get("SCOUT_URL"):
        return main_local()
    import time
    cat, seen, checked = live_catalog(), None, 0.0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        method, i = m.get("method"), m.get("id")
        if time.time() - checked > 300:                       # every 5 minutes, look for new server tools
            checked, fresh = time.time(), live_catalog()
            if seen is not None and [t["name"] for t in fresh] != [t["name"] for t in cat]:
                cat = fresh
                notify("notifications/tools/list_changed")     # clients that listen re-read the list on their own
            cat = fresh
        if method == "initialize":
            reply(i, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": True}},
                      "serverInfo": {"name": "kestrel", "version": "1.4.0"}, "instructions": GATEWAY_NOTE})
        elif method == "tools/list":
            seen = [t["name"] for t in cat]
            reply(i, {"tools": tool_list(cat)})
        elif method == "tools/call":
            p = m.get("params", {})
            name, args = p.get("name"), p.get("arguments") or {}
            if name == "kestrel_tools":
                cat = live_catalog()
                reply(i, {"content": [{"type": "text", "text": json.dumps({"tools": filter_catalog(cat, args.get("search")), "note": GATEWAY_NOTE})}],
                          "isError": False})
                continue
            if name == "kestrel_call":
                name, args = args.get("tool"), args.get("args") or {}
            t = next((x for x in cat if x["name"] == name), None) or next((x for x in live_catalog() if x["name"] == name), None)
            if not t:
                reply(i, {"content": [{"type": "text", "text": json.dumps({"error": "no Kestrel tool called %s. Call kestrel_tools for the list." % name})}],
                          "isError": True})
                continue
            POST.add(t["route"]) if t.get("method") == "POST" else None
            text, is_err = call(t["route"], args)
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS] + '\n...truncated. Narrow the request (limit, kind, max_age_days).'
            reply(i, {"content": [{"type": "text", "text": text}], "isError": is_err})
        elif method == "ping":
            reply(i, {})
        elif i is not None:
            reply(i, error={"code": -32601, "message": "method not found"})

if __name__ == "__main__":
    main()
