#!/usr/bin/env python3
"""Scout jobs. Run from a systemd timer (see README).

  python3 scout_jobs.py daily            refresh every niche, breakouts and competitor snapshots
  python3 scout_jobs.py daily demo     only these niches
  python3 scout_jobs.py watch            early-breakout check (run hourly)
  python3 scout_jobs.py weekly           weekly scorecard for my Instagram accounts
  python3 scout_jobs.py brief            push today's brief to Trello
  python3 scout_jobs.py live             live snapshot of my latest uploads (run every 15 minutes)
  python3 scout_jobs.py reach            pull new impressions/CTR reports for my channels (run every few hours)
"""
import json
import sys

import scout_core
from scout_core import Config, ScoutError
from scout_extras import Kestrel


def main():
    args = sys.argv[1:] or ["daily"]
    if args[0] not in ("daily", "watch", "weekly", "brief", "live", "reach"):
        sys.exit("usage: scout_jobs.py daily [niche ...] | watch | weekly | brief | live | reach")
    scout_core.CALLER.name = "job-" + args[0]   # each job gets its own YouTube share
    try:
        k = Kestrel(Config())
        if args[0] == "watch":
            out = k.watch()
        elif args[0] == "weekly":
            out = k.scorecard_weekly()
        elif args[0] == "reach":
            out = k.reach_all()
        elif args[0] == "live":
            out = k.live_all()
        elif args[0] == "brief":
            out = k.brief(push=True)
        else:
            out = k.run_daily(args[1:] or None)
    except ScoutError as e:
        sys.exit("kestrel job failed: %s" % e)
    print(json.dumps(out, indent=1, ensure_ascii=False))
    if args[0] == "daily":
        sys.exit(1 if out["errors"] and not out["niches"] and not out["tracked"] else 0)


if __name__ == "__main__":
    main()
