---
name: kestrel-setup
description: Walk a new user through setting up Kestrel step by step (YouTube key, Instagram token, their own channel, their page and niche). Use when someone says "set up Kestrel", is using Kestrel for the first time, or a Kestrel tool reports a missing or rejected key.
---

# Setting up Kestrel

Kestrel is free. Everything it needs is a free account or key the user creates once. Kestrel has no credits or paid tiers. Only the optional X API is billed by X, so say that if X comes up.

1. Run the `kestrel_setup` tool. It tests every part for real and returns `items`, each with a `status` (working, missing, broken, optional), what it unlocks, and `how_to_fix` steps.
2. Tell the user in one or two lines what already works and what's missing. Lead with what they can do right now.
3. Fix ONE item at a time, in the order the tool lists them (broken first, then missing). Give that item's `how_to_fix` as a short numbered list with the links. Say roughly how long it takes (YouTube key: 2 minutes; Instagram token: about 10).
4. Wait for them to say it's done. In Claude Code, keys go in in `/config` (Kestrel section). Then run `kestrel_setup` again to confirm before moving on. If it's still broken, read the `detail` and help with that specific error.
5. Items marked `optional` are extras (their own channel stats, reel transcription). Mention them once at the end and let the user decide.
6. For "About you", just ask for their Instagram username, what their page is about and who it's for, in one message, and save it with `scout_my_profile_set`. Don't send them anywhere for this.
7. When everything they want is working, suggest a first thing to try, for example: "Break down a creator you look up to: just tell me their handle."

Keep each message short. Never ask the user to paste a key into the chat; keys go into the plugin settings or the server's `.env`, never into the conversation.
