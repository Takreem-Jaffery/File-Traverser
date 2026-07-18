#!/usr/bin/env python3

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    cfg.setdefault("repo_path", ".")
    cfg.setdefault("commit_file", "activity.log")
    cfg.setdefault("branch", "main")
    cfg.setdefault("skip_probability", 0.15)       # chance a day is skipped entirely
    cfg.setdefault("multi_commit_days_per_week", 1) # how many days/week get extra commits
    cfg.setdefault("multi_commit_min", 2)
    cfg.setdefault("multi_commit_max", 10)
    cfg.setdefault("active_hour_start", 8)           # earliest hour a commit can land (24h)
    cfg.setdefault("active_hour_end", 23)             # latest hour a commit can land
    cfg.setdefault("git_user_name", None)   # set to your GitHub name if running in CI (no global git identity there)
    cfg.setdefault("git_user_email", None)  # MUST match a verified email on your GitHub account for commits to count
    cfg.setdefault("commit_messages", [
        "Update activity log",
        "Minor tweaks",
        "Small update",
        "Chore: routine update",
        "Housekeeping",
    ])
    return cfg


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def state_path_for(day: date) -> Path:
    return STATE_DIR / f"plan_{day.isoformat()}.json"


def week_key(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_state_path() -> Path:
    return STATE_DIR / "week_multi_commit_days.json"


def load_week_state() -> dict:
    p = week_state_path()
    if p.exists():
        return json.loads(p.read_text())
    return {}


def save_week_state(state: dict) -> None:
    week_state_path().write_text(json.dumps(state, indent=2))


def choose_multi_commit_days(day: date, cfg: dict) -> list:
    """
    For the ISO week containing `day`, deterministically (but randomly,
    seeded once) pick which weekday numbers (0=Mon..6=Sun) are the
    multi-commit days. Cached so all 7 days of a week agree.
    """
    wk = week_key(day)
    state = load_week_state()
    if wk in state:
        return state[wk]

    n = min(cfg["multi_commit_days_per_week"], 7)
    chosen = sorted(random.sample(range(7), n))
    state[wk] = chosen
    # prune old weeks so the file doesn't grow forever
    state = {k: v for k, v in state.items() if k >= week_key(date.today() - timedelta(days=14))}
    save_week_state(state)
    return chosen


def random_time_today(day: date, cfg: dict) -> datetime:
    hour = random.randint(cfg["active_hour_start"], cfg["active_hour_end"])
    minute = random.randint(0, 59)
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)


def build_plan(day: date, cfg: dict) -> dict:
    skip = random.random() < cfg["skip_probability"]

    plan = {
        "date": day.isoformat(),
        "skip": skip,
        "slots": [],  # list of {"time": "HH:MM", "done": bool}
    }

    if skip:
        return plan

    multi_days = choose_multi_commit_days(day, cfg)
    is_multi = day.weekday() in multi_days

    if is_multi:
        count = random.randint(cfg["multi_commit_min"], cfg["multi_commit_max"])
    else:
        count = 1

    times = sorted(random_time_today(day, cfg) for _ in range(count))
    plan["slots"] = [{"time": t.strftime("%H:%M"), "done": False} for t in times]
    return plan


# --------------------------------------------------------------------------
# Git actions
# --------------------------------------------------------------------------

def run(cmd, cwd):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout


def make_commit(cfg: dict) -> None:
    repo = cfg["repo_path"]
    file_path = Path(repo) / cfg["commit_file"]

    if cfg.get("git_user_name"):
        run(["git", "config", "user.name", cfg["git_user_name"]], cwd=repo)
    if cfg.get("git_user_email"):
        run(["git", "config", "user.email", cfg["git_user_email"]], cwd=repo)

    with open(file_path, "a") as f:
        f.write(f"{datetime.now().isoformat()} - automated update\n")

    msg = random.choice(cfg["commit_messages"])

    run(["git", "add", cfg["commit_file"]], cwd=repo)
    run(["git", "commit", "-m", msg], cwd=repo)
    run(["git", "push", "origin", cfg["branch"]], cwd=repo)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Committed and pushed: {msg}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_plan(args):
    cfg = load_config(args.config)
    day = date.today()
    plan = build_plan(day, cfg)
    state_path_for(day).write_text(json.dumps(plan, indent=2))

    if plan["skip"]:
        print(f"{day}: skip day, no commits scheduled.")
    else:
        times = ", ".join(s["time"] for s in plan["slots"])
        print(f"{day}: {len(plan['slots'])} commit(s) scheduled at {times}")


def cmd_check(args):
    cfg = load_config(args.config)
    day = date.today()
    sp = state_path_for(day)

    if not sp.exists():
        # No plan yet for today (e.g. `plan` cron hasn't fired). Create one
        # on the fly so `check` alone is enough to keep things running.
        plan = build_plan(day, cfg)
        sp.write_text(json.dumps(plan, indent=2))
    else:
        plan = json.loads(sp.read_text())

    if plan["skip"]:
        return

    now = datetime.now()
    changed = False

    for slot in plan["slots"]:
        if slot["done"]:
            continue
        slot_time = datetime.combine(day, datetime.strptime(slot["time"], "%H:%M").time())
        if now >= slot_time:
            try:
                make_commit(cfg)
                slot["done"] = True
                changed = True
            except RuntimeError as e:
                print(f"Commit failed, will retry next check: {e}", file=sys.stderr)

    if changed:
        sp.write_text(json.dumps(plan, indent=2))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Natural-looking daily GitHub commit scheduler")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"), help="Path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("plan", help="Generate today's commit schedule").set_defaults(func=cmd_plan)
    sub.add_parser("check", help="Execute any due commits from today's plan").set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
