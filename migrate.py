#!/usr/bin/env python3
"""Migrate Jefit workout history to Hevy via the Hevy API."""

import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

import requests

load_dotenv()

API_KEY = os.environ.get("HEVY_API_KEY", "").strip()
BASE_URL = "https://api.hevyapp.com"
HEADERS = {"api-key": API_KEY, "Content-Type": "application/json"}

LBS_TO_KG = 0.453592
CROSSWALK_PATH = "exercise_crosswalk.csv"
# Jefit timestamps are absolute (Unix epoch). Render them in the user's local
# zone so evening workouts keep their correct local date (Toronto, with DST).
LOCAL_TZ = ZoneInfo("America/Toronto")


# ---------------------------------------------------------------------------
# CSV parsing helpers
# ---------------------------------------------------------------------------

def load_section(filepath, section_name):
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    marker = f"### {section_name}"
    start = content.find(marker)
    if start == -1:
        return []
    start = content.find("\n", start) + 1  # skip the marker line

    end = content.find("######################################################", start)
    block = content[start:end] if end != -1 else content[start:]

    # Parse as a proper CSV to handle quoted fields with embedded newlines/commas
    rows = []
    header = None
    reader = csv.reader(io.StringIO(block))
    for parsed in reader:
        if not any(parsed):
            continue
        if header is None:
            header = parsed
        else:
            rows.append(dict(zip(header, parsed)))
    return rows


def parse_logs(logs_str):
    sets = []
    for part in logs_str.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([\d.]+)x(\d+)$", part)
        if m:
            weight = float(m.group(1))
            reps = int(m.group(2))
            sets.append((weight, reps))
    return sets


def load_crosswalk():
    """Load exercise_crosswalk.csv → {jefit_name: hevy_template_id}. Empty ID = needs custom template."""
    mapping = {}
    if not os.path.exists(CROSSWALK_PATH):
        return mapping
    with open(CROSSWALK_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["jefit_name"].strip()
            tid = row["hevy_template_id"].strip()
            if name:
                mapping[name] = tid  # empty string means needs custom template
    return mapping


# ---------------------------------------------------------------------------
# Hevy API helpers
# ---------------------------------------------------------------------------

def fetch_all_exercise_templates():
    templates = []
    page = 1
    while True:
        r = requests.get(
            f"{BASE_URL}/v1/exercise_templates",
            headers=HEADERS,
            params={"page": page, "pageSize": 100},
        )
        r.raise_for_status()
        data = r.json()
        page_templates = data.get("exercise_templates", [])
        templates.extend(page_templates)
        if len(page_templates) < 100:
            break
        page += 1
    return templates


MUSCLE_GROUP_LABELS = {
    "chest": "Chest",
    "biceps": "Biceps",
    "triceps": "Triceps",
    "shoulders": "Shoulders",
    "lats": "Back",
    "upper_back": "Back",
    "lower_back": "Back",
    "abdominals": "Core",
    "quadriceps": "Legs",
    "hamstrings": "Legs",
    "glutes": "Legs",
    "calves": "Legs",
    "full_body": "Full Body",
    "cardio": "Cardio",
    "other": None,
}


def fetch_existing_workouts_by_epoch():
    """Return {start_epoch: workout_id} for all workouts on the account.
    Keying by absolute instant (not string) is robust to timezone-offset
    differences in how start_time is formatted."""
    by_epoch = {}
    page = 1
    while True:
        r = requests.get(
            f"{BASE_URL}/v1/workouts",
            headers=HEADERS,
            params={"page": page, "pageSize": 10},
        )
        r.raise_for_status()
        data = r.json()
        ws = data.get("workouts", [])
        for w in ws:
            st = w.get("start_time")
            if st:
                ep = int(datetime.fromisoformat(st.replace("Z", "+00:00")).timestamp())
                by_epoch[ep] = w["id"]
        page_count = data.get("page_count", page)
        if page >= page_count or not ws:
            break
        page += 1
    return by_epoch


def build_template_map(templates):
    # Maps lowercased title → (id, type, primary_muscle_group)
    by_name = {
        t["title"].lower(): (t["id"], t.get("type", "weight_reps"), t.get("primary_muscle_group", "other"))
        for t in templates
    }
    # Also maps id → (type, primary_muscle_group) for crosswalk overrides
    by_id = {
        t["id"]: (t.get("type", "weight_reps"), t.get("primary_muscle_group", "other"))
        for t in templates
    }
    return by_name, by_id


def workout_title_from_muscles(muscle_groups):
    """Generate a readable title from a list of primary muscle groups."""
    seen = []
    for mg in muscle_groups:
        label = MUSCLE_GROUP_LABELS.get(mg)
        if label and label not in seen:
            seen.append(label)
    if not seen:
        return "Workout"
    if len(seen) == 1:
        return seen[0]
    if len(seen) == 2:
        return f"{seen[0]} & {seen[1]}"
    return ", ".join(seen[:2]) + f" & {seen[2]}" if len(seen) >= 3 else ", ".join(seen)


def find_template(name, template_map_by_name):
    """Return (template_id, template_type, muscle_group) or (None, None, None)."""
    key = name.lower().strip()
    if key in template_map_by_name:
        return template_map_by_name[key]
    for title, val in template_map_by_name.items():
        if key in title or title in key:
            return val
    return None, None, None


def build_set(weight_lbs, reps_or_secs, template_type):
    """Build a Hevy set dict, using duration_seconds for duration-type exercises."""
    s = {"type": "normal", "weight_kg": None, "reps": None, "duration_seconds": None}
    if template_type == "duration":
        s["duration_seconds"] = reps_or_secs if reps_or_secs > 0 else None
    else:
        s["weight_kg"] = round(weight_lbs * LBS_TO_KG, 2) if weight_lbs > 0 else None
        s["reps"] = reps_or_secs if reps_or_secs > 0 else None
    return s


# ---------------------------------------------------------------------------
# Collapse cumulative set-logging snapshots
# ---------------------------------------------------------------------------

def collapse_session_logs(session_logs):
    """
    Jefit logs each set addition as a new row holding ALL sets so far, so one
    exercise can appear as many rows: [s1] -> [s1,s2] -> [s1,s2,s3]...
    Collapse these cumulative snapshots down to the final (most complete) row,
    while preserving genuinely separate instances of the same exercise.

    For each exercise (eid) we track its most recent open "chain". A new row
    continues that chain when it preserves all-but-the-last set of the running
    snapshot (the last set may be edited, e.g. a corrected weight) AND either:
      - it adds a set (grows) — handles cumulative logging even when a superset
        interleaves another exercise between snapshots, or
      - it is the immediately preceding row (a same-length edit/correction).
    Otherwise it starts a new instance. Instances keep first-seen order.
    """
    instances = []          # {"row": log, "sets": [(w, r), ...]}
    latest_by_eid = {}      # eid -> most recent instance dict
    prev_eid = None

    for log in session_logs:
        eid = log["eid"]
        cur_sets = parse_logs(log["logs"])
        inst = latest_by_eid.get(eid)

        cont = False
        redundant = False
        if inst is not None:
            prev_sets = inst["sets"]
            stable = prev_sets[:-1]  # all but the (possibly edited) last set
            prefix_ok = len(cur_sets) >= len(prev_sets) and cur_sets[: len(stable)] == stable
            grows = len(cur_sets) > len(prev_sets)
            if prefix_ok and (grows or prev_eid == eid):
                cont = True
            # Reverse-order redundant snapshot: a shorter row that exactly
            # matches the opening of the instance we already kept.
            elif len(cur_sets) < len(prev_sets) and prev_sets[: len(cur_sets)] == cur_sets:
                redundant = True

        if redundant:
            pass  # drop it entirely
        elif cont:
            inst["row"] = log
            inst["sets"] = cur_sets
        else:
            new_inst = {"row": log, "sets": cur_sets}
            instances.append(new_inst)
            latest_by_eid[eid] = new_inst

        prev_eid = eid

    return [inst["row"] for inst in instances]


# ---------------------------------------------------------------------------
# Build workout payloads
# ---------------------------------------------------------------------------

def build_workouts(sessions, exercise_logs, template_map, template_map_by_id, crosswalk, notes_index):
    """
    Group exercise logs by session and build Hevy workout payloads.
    Resolution order per exercise:
      1. Crosswalk file (manually mapped template ID)
      2. Exact/partial match against Hevy templates
    Exercises with no match are skipped. Workouts where all exercises
    are unmatched are skipped entirely.
    Title is generated from muscle groups. Notes are attached per exercise.
    """
    logs_by_session = {}
    for log in exercise_logs:
        session_id = log["belongsession"]
        if session_id == "0":
            continue
        logs_by_session.setdefault(session_id, []).append(log)

    workouts = []
    skipped_exercises = set()

    for session in sessions:
        session_id = session["_id"]
        session_logs = logs_by_session.get(session_id, [])
        if not session_logs:
            continue

        start_ts = int(session["starttime"])
        end_ts = int(session["endtime"])
        start_time = datetime.fromtimestamp(start_ts, tz=LOCAL_TZ).isoformat()
        end_time = datetime.fromtimestamp(end_ts, tz=LOCAL_TZ).isoformat()

        # Merge all instances of the same exercise (eid) into one card. The
        # user logged some movements as a "top set" row plus a back-off row;
        # in Hevy these belong in a single exercise with all sets concatenated.
        merged = {}        # eid -> exercise dict (first-seen wins for position)
        order = []         # eids in first-seen order
        muscle_groups = []

        for log in collapse_session_logs(session_logs):
            ename = log["ename"].strip()
            if not ename:
                continue

            raw_sets = parse_logs(log["logs"])
            if not raw_sets:
                continue

            # Resolve template ID + type + muscle group
            if ename in crosswalk and crosswalk[ename]:
                template_id = crosswalk[ename]
                # Look up type/muscle by the mapped ID, not the Jefit name
                template_type, muscle_group = template_map_by_id.get(template_id, ("weight_reps", "other"))
            else:
                template_id, template_type, muscle_group = find_template(ename, template_map)

            if template_id is None:
                skipped_exercises.add(ename)
                continue

            sets = [build_set(w, r, template_type) for w, r in raw_sets]
            # Drop empty placeholder sets (Jefit logs "0x0" filler rows);
            # keep bodyweight sets (0 weight but real reps) and timed sets.
            sets = [
                s for s in sets
                if s["weight_kg"] is not None
                or s["reps"] is not None
                or s["duration_seconds"] is not None
            ]
            if not sets:
                continue

            note = notes_index.get((log["eid"], log["logTime"]))
            eid = log["eid"]

            if eid not in merged:
                merged[eid] = {
                    "exercise_template_id": template_id,
                    "superset_id": None,
                    "notes": note,
                    "sets": sets,
                }
                order.append(eid)
                if muscle_group:
                    muscle_groups.append(muscle_group)
            else:
                ex = merged[eid]
                ex["sets"].extend(sets)
                # Preserve notes from later instances too, without duplicating.
                if note:
                    ex["notes"] = note if not ex["notes"] else f"{ex['notes']}\n{note}"

        exercises = [merged[eid] for eid in order]
        if not exercises:
            continue

        title = workout_title_from_muscles(muscle_groups)

        workouts.append({
            "session_id": session_id,
            "start_epoch": start_ts,
            "payload": {
                "workout": {
                    "title": title,
                    "description": None,
                    "start_time": start_time,
                    "end_time": end_time,
                    "is_private": False,
                    "exercises": exercises,
                }
            },
        })

    return workouts, skipped_exercises


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def post_with_backoff(url, payload, max_attempts=6):
    """POST/PUT with exponential backoff on HTTP 429 (rate limit)."""
    delay = 2.0
    for _ in range(max_attempts):
        r = requests.post(url, headers=HEADERS, json=payload)
        if r.status_code != 429:
            return r
        time.sleep(delay)
        delay = min(delay * 2, 60)
    return r


def main():
    dry_run = "--dry-run" in sys.argv
    test_one = "--test" in sys.argv
    # --retry-missing: only create workouts not yet on the account (skips the
    # already-uploaded ones), with backoff. Use to finish an import that was
    # cut short by the API rate limit without re-touching everything.
    retry_missing = "--retry-missing" in sys.argv
    csv_path = "jefit_data/StevenMcAvoy_20260611.csv"

    print("Loading CSV data...")
    sessions = load_section(csv_path, "WORKOUT SESSIONS #################################")
    exercise_logs = load_section(csv_path, "EXERCISE LOGS ####################################")
    raw_notes = load_section(csv_path, "NOTES ############################################")
    # Index notes by (eid, logTime) for O(1) lookup
    notes_index = {(n["eid"], n["logTime"]): n["mynote"] for n in raw_notes if n.get("mynote")}
    print(f"  {len(sessions)} sessions, {len(exercise_logs)} exercise logs, {len(notes_index)} notes")

    if dry_run:
        print("Fetching Hevy exercise templates...")
        templates = fetch_all_exercise_templates()
        template_map, template_map_by_id = build_template_map(templates)
        print(f"  {len(templates)} templates loaded")

        crosswalk = load_crosswalk()
        if crosswalk:
            mapped = sum(1 for v in crosswalk.values() if v)
            print(f"  Crosswalk loaded: {mapped}/{len(crosswalk)} entries mapped")

        print("Building workout payloads...")
        workouts, skipped_exercises = build_workouts(sessions, exercise_logs, template_map, template_map_by_id, crosswalk, notes_index)
        print(f"  {len(workouts)} workouts built")
        if skipped_exercises:
            print(f"  {len(skipped_exercises)} unmatched exercise(s) skipped")

        out_dir = "dry_run_output"
        os.makedirs(out_dir, exist_ok=True)
        # Clear previous output
        for f in os.listdir(out_dir):
            if f.endswith(".json"):
                os.remove(os.path.join(out_dir, f))

        for w in workouts:
            start = w["payload"]["workout"]["start_time"]
            date_str = start[:10]  # YYYY-MM-DD
            filename = f"{date_str}_{w['session_id']}.json"
            with open(os.path.join(out_dir, filename), "w") as f:
                json.dump(w["payload"], f, indent=2)

        print(f"\nWrote {len(workouts)} JSON files to ./{out_dir}/")
        return

    print("Fetching Hevy exercise templates...")
    templates = fetch_all_exercise_templates()
    template_map, template_map_by_id = build_template_map(templates)
    print(f"  {len(templates)} templates loaded")

    crosswalk = load_crosswalk()
    if crosswalk:
        mapped = sum(1 for v in crosswalk.values() if v)
        print(f"  Crosswalk loaded: {mapped}/{len(crosswalk)} entries mapped")

    print("Building workout payloads...")
    workouts, skipped_exercises = build_workouts(sessions, exercise_logs, template_map, template_map_by_id, crosswalk, notes_index)
    print(f"  {len(workouts)} workouts ready")
    if skipped_exercises:
        print(f"  {len(skipped_exercises)} unmatched exercise(s) skipped (fill in crosswalk to include them):")
        for name in sorted(skipped_exercises):
            print(f"    - {name}")

    # Idempotency guard: match workouts to existing ones by start instant.
    # Existing Jefit imports are updated in place (PUT); new ones are created
    # (POST). Comparing by absolute instant makes re-runs safe and keeps
    # previously-imported workouts in sync with the latest conversion logic.
    print("Fetching existing Hevy workouts...")
    existing = fetch_existing_workouts_by_epoch()
    print(f"  {len(existing)} existing workouts on the account")

    if retry_missing:
        workouts = [w for w in workouts if w["start_epoch"] not in existing]
        print(f"Retry-missing mode: {len(workouts)} workout(s) not yet on the account.")

    if test_one:
        workouts = workouts[:1]
        print(f"\nTest mode: uploading 1 workout only.")

    n = len(workouts)
    print(f"\nUploading {n} workout(s) ({sum(1 for w in workouts if w['start_epoch'] in existing)} update, {sum(1 for w in workouts if w['start_epoch'] not in existing)} create)...")
    created = updated = failed = 0
    for i, w in enumerate(workouts):
        wid = existing.get(w["start_epoch"])
        if wid:
            r = requests.put(f"{BASE_URL}/v1/workouts/{wid}", headers=HEADERS, json=w["payload"])
            ok, verb = r.status_code in (200, 201), "UPDATED"
        else:
            r = post_with_backoff(f"{BASE_URL}/v1/workouts", w["payload"])
            ok, verb = r.status_code == 201, "created"
        if ok:
            if wid:
                updated += 1
            else:
                created += 1
            print(f"  [{i+1}/{n}] {verb} - session {w['session_id']}")
        else:
            failed += 1
            print(f"  [{i+1}/{n}] FAILED ({r.status_code}) - session {w['session_id']}: {r.text[:200]}")
        time.sleep(0.5)

    print(f"\nDone: {created} created, {updated} updated, {failed} failed (of {n}).")
    if failed:
        print("Some workouts failed (likely API rate limit). Re-run later with --retry-missing to finish.")


if __name__ == "__main__":
    main()
