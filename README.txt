How it works (only used on Unraid that has Tautulli and Plex with Plex Pass)

Every 15 seconds it calls get_activity. For each limited user it adds time to a local SQLite ledger while they’re playing or buffering. Paused time doesn’t count, and two simultaneous streams count once.
Each poll it also checks that user’s Tautulli history for today, so a restart or downtime can’t lose watch time. One side effect: after two overlapping streams end, the history total counts both, so time can jump ahead of the ledger.
Once a user is over their daily limit, or outside their allowed hours, it calls terminate_session with a message the player displays. If they hit play again, the new stream is killed on the next poll, usually within 15 seconds.
The config supports daily_minutes, an optional weekend_minutes, and an optional allowed_hours curfew window. It hot-reloads when you edit the file, and the counter resets at midnight in your configured timezone.

To set it up

Copy config.example.yml to config.yml and add your Tautulli URL and API key (Settings → Web Interface).
Run python screentime.py --list-users to see user IDs and which are managed (restricted). Use a user ID, username or friendly name as the key under users:.
Run it with --dry-run first to see what it would stop.
For Docker, which suits your Unraid box, use the included Dockerfile and docker-compose.yml. Mount a config folder (so edits reload) and a data folder, and set db_path: /data/usage.db.

Caveats

Stopping streams through Tautulli needs Plex Pass on the server, as far as I know.
Enforcement lives entirely in this script, so if it or Tautulli is down, nothing is stopped. Precision is the poll interval.
It only covers streaming. Downloads or sync, or a kid switching to an unlimited profile on the same device, aren’t covered.
If you want a harder lock, the next step would be removing the user’s library access through the Plex API once the limit is hit. I left that out because it’s more invasive.

Check that --list-users shows your managed users the way you expect, Tautulli labels them on your setup. Run --usage any time to see today’s tracked minutes or look at Unraid's container log.
