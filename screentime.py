#!/usr/bin/env python3
"""Per-user daily screen time limits for Plex, enforced through Tautulli.

Only users listed in the config are touched; everyone else streams freely.
Watch time is accumulated in a small local SQLite ledger (wall-clock time the
user is actually playing, counted once even with several streams), and is
reconciled against Tautulli's history so a restart or downtime can't erase it.
When a user is over their limit (or outside their allowed hours), their
streams are terminated via Tautulli's terminate_session.
"""
import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests
import yaml

log = logging.getLogger("plex-limits")

DEFAULT_MESSAGE = "Screen time is up for today. Come back tomorrow!"
CURFEW_MESSAGE = "Plex is closed right now. Come back during your allowed hours."
COUNTED_STATES = {"playing", "buffering"}  # paused time does not count


class Tautulli:
    def __init__(self, url, apikey):
        self.url = url.rstrip("/") + "/api/v2"
        self.apikey = apikey
        self.http = requests.Session()

    def call(self, cmd, **params):
        params.update(apikey=self.apikey, cmd=cmd)
        r = self.http.get(self.url, params=params, timeout=15)
        r.raise_for_status()
        resp = r.json()["response"]
        if resp.get("result") != "success":
            raise RuntimeError(f"Tautulli {cmd} failed: {resp.get('message')}")
        return resp.get("data")


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS usage("
            "day TEXT, user_id TEXT, seconds REAL NOT NULL DEFAULT 0,"
            "PRIMARY KEY(day, user_id))"
        )
        self.db.commit()

    def get(self, day, uid):
        row = self.db.execute(
            "SELECT seconds FROM usage WHERE day=? AND user_id=?", (day, uid)
        ).fetchone()
        return row[0] if row else 0.0

    def add(self, day, uid, secs):
        self.db.execute(
            "INSERT INTO usage VALUES(?,?,?) ON CONFLICT(day,user_id) "
            "DO UPDATE SET seconds = seconds + excluded.seconds",
            (day, uid, secs),
        )
        self.db.commit()

    def raise_to(self, day, uid, secs):
        self.db.execute(
            "INSERT INTO usage VALUES(?,?,?) ON CONFLICT(day,user_id) "
            "DO UPDATE SET seconds = MAX(seconds, excluded.seconds)",
            (day, uid, secs),
        )
        self.db.commit()

    def today(self, day):
        return self.db.execute(
            "SELECT user_id, seconds FROM usage WHERE day=?", (day,)
        ).fetchall()


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    cfg["_users"] = {str(k).lower(): (v or {}) for k, v in (cfg.get("users") or {}).items()}
    return cfg


def find_rule(cfg, session):
    for key in (session.get("user_id"), session.get("username"), session.get("friendly_name")):
        if key is not None and str(key).lower() in cfg["_users"]:
            return cfg["_users"][str(key).lower()]
    return None


def limit_minutes(rule, now):
    if now.weekday() >= 5 and rule.get("weekend_minutes") is not None:
        return rule["weekend_minutes"]
    return rule.get("daily_minutes")  # None = no daily cap (curfew only)


def in_allowed_hours(rule, now):
    window = rule.get("allowed_hours")
    if not window:
        return True
    start_s, end_s = window.split("-")
    start = dtime.fromisoformat(start_s.strip())
    end = dtime.fromisoformat(end_s.strip())
    t = now.time().replace(second=0, microsecond=0)
    return start <= t < end if start < end else (t >= start or t < end)


def history_seconds(tautulli, uid, day_start):
    data = tautulli.call(
        "get_history",
        user_id=uid,
        start_date=day_start.strftime("%Y-%m-%d"),
        grouping=0,
        include_activity=0,
        length=1000,
    )
    rows = (data or {}).get("data", [])
    cutoff = day_start.timestamp()
    return sum((r.get("duration") or 0) for r in rows if (r.get("started") or 0) >= cutoff)


class Limiter:
    def __init__(self, tautulli, ledger, dry_run=False):
        self.tautulli = tautulli
        self.ledger = ledger
        self.dry_run = dry_run
        self.names = {}          # user_id -> display name, learned from sessions
        self.active = set()      # user_ids with a session on the last poll
        self.last_status = None  # monotonic time of the last summary line

    def status(self, cfg, now, active_states):
        """Log one line per configured user: time used today vs. their limit."""
        day = now.strftime("%Y-%m-%d")
        used = {str(uid): secs for uid, secs in self.ledger.today(day)}
        for key, rule in cfg["_users"].items():
            # Resolve a config key (id, username or friendly name) to a user_id.
            uid = key if key in used or key.isdigit() else next(
                (u for u, n in self.names.items() if n.lower() == key), key
            )
            name = self.names.get(uid, key)
            mins = used.get(uid, 0.0) / 60
            cap = limit_minutes(rule, now)
            if cap is None:
                usage = f"{mins:.0f} min used (no daily cap)"
            else:
                usage = f"{mins:.0f}/{cap:g} min used, {max(cap - mins, 0):.0f} left"
            extras = []
            if rule.get("allowed_hours"):
                extras.append(
                    f"allowed {rule['allowed_hours']}"
                    + ("" if in_allowed_hours(rule, now) else " (closed now)")
                )
            state = active_states.get(uid, "idle")
            log.info("STATUS %s: %s [%s]%s", name, usage, state,
                     (" " + ", ".join(extras)) if extras else "")

    def tick(self, cfg, now, dt):
        sessions = self.tautulli.call("get_activity").get("sessions", [])
        day = now.strftime("%Y-%m-%d")
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        by_user = {}
        for s in sessions:
            rule = find_rule(cfg, s)
            if rule is not None:
                entry = by_user.setdefault(str(s["user_id"]), {"rule": rule, "sessions": []})
                entry["sessions"].append(s)

        for uid, entry in by_user.items():
            rule, user_sessions = entry["rule"], entry["sessions"]
            name = user_sessions[0].get("friendly_name") or user_sessions[0].get("username")
            self.names[uid] = name
            if uid not in self.active:
                titles = ", ".join(s.get("full_title") or "?" for s in user_sessions)
                log.info("%s started watching: %s", name, titles)

            if any(s.get("state") in COUNTED_STATES for s in user_sessions):
                self.ledger.add(day, uid, dt)
            try:
                self.ledger.raise_to(day, uid, history_seconds(self.tautulli, uid, day_start))
            except Exception as e:  # reconcile is best-effort
                log.warning("history reconcile failed for %s: %s", name, e)
            used = self.ledger.get(day, uid)

            reason = None
            if not in_allowed_hours(rule, now):
                reason = rule.get("curfew_message", CURFEW_MESSAGE)
            else:
                cap = limit_minutes(rule, now)
                if cap is not None and used >= cap * 60:
                    reason = rule.get("message", DEFAULT_MESSAGE)

            if reason:
                for s in user_sessions:
                    log.info("stopping %s (used %.0f min): %s", name, used / 60, s.get("full_title"))
                    if not self.dry_run:
                        self.tautulli.call(
                            "terminate_session", session_id=s["session_id"], message=reason
                        )

        # Log users whose sessions ended since the last poll.
        for uid in self.active - set(by_user):
            log.info("%s stopped watching (%.0f min used today)",
                     self.names.get(uid, uid), self.ledger.get(day, uid) / 60)
        self.active = set(by_user)

        # Periodic summary of every configured user.
        interval = cfg.get("status_interval_seconds", 300)
        nowm = time.monotonic()
        if interval and (self.last_status is None or nowm - self.last_status >= interval):
            self.last_status = nowm
            states = {
                uid: ("/".join(sorted({s.get("state") or "?" for s in e["sessions"]})))
                for uid, e in by_user.items()
            }
            self.status(cfg, now, states)


def run(args):
    cfg = load_config(args.config)
    mtime = os.path.getmtime(args.config)
    tautulli = Tautulli(cfg["tautulli"]["url"], cfg["tautulli"]["apikey"])
    ledger = Ledger(cfg.get("db_path", "usage.db"))
    limiter = Limiter(tautulli, ledger, dry_run=args.dry_run)
    last = time.monotonic()
    while True:
        poll = cfg.get("poll_seconds", 15)
        tz = ZoneInfo(cfg.get("timezone") or os.environ.get("TZ") or "UTC")
        elapsed = time.monotonic() - last
        last = time.monotonic()
        try:
            limiter.tick(cfg, datetime.now(tz), min(elapsed, poll * 3))
        except Exception as e:
            log.warning("poll failed: %s", e)
        time.sleep(poll)
        if os.path.getmtime(args.config) != mtime:  # hot-reload edits
            try:
                cfg, mtime = load_config(args.config), os.path.getmtime(args.config)
                log.info("config reloaded")
            except Exception as e:
                log.error("config reload failed, keeping old config: %s", e)


def list_users(args):
    cfg = load_config(args.config)
    t = Tautulli(cfg["tautulli"]["url"], cfg["tautulli"]["apikey"])
    print(f"{'user_id':<12}{'username':<24}{'home':<6}{'restricted':<11}{'admin'}")
    for u in t.call("get_users"):
        def col(key):
            v = u.get(key)
            return "" if v is None else str(v)

        print(
            f"{col('user_id'):<12}{col('username'):<24}{col('is_home_user'):<6}"
            f"{col('is_restricted'):<11}{col('is_admin')}"
        )


def show_usage(args):
    cfg = load_config(args.config)
    tz = ZoneInfo(cfg.get("timezone") or os.environ.get("TZ") or "UTC")
    day = datetime.now(tz).strftime("%Y-%m-%d")
    for uid, secs in Ledger(cfg.get("db_path", "usage.db")).today(day):
        print(f"{uid}: {secs / 60:.1f} min today")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default="config.yml")
    p.add_argument("--dry-run", action="store_true", help="log what would be stopped, stop nothing")
    p.add_argument("--list-users", action="store_true", help="print Tautulli users and ids, then exit")
    p.add_argument("--usage", action="store_true", help="print today's tracked minutes, then exit")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.list_users:
        return list_users(args)
    if args.usage:
        return show_usage(args)
    run(args)


if __name__ == "__main__":
    sys.exit(main())

