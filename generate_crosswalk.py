#!/usr/bin/env python3
"""Generate a crosswalk CSV: unmatched Jefit exercises vs available Hevy templates."""

import csv
import io
import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("HEVY_API_KEY", "").strip()
BASE_URL = "https://api.hevyapp.com"
HEADERS = {"api-key": API_KEY, "Content-Type": "application/json"}


def load_section(filepath, section_name):
    with open(filepath, encoding="utf-8") as f:
        lines = f.readlines()
    in_section = False
    header = None
    rows = []
    for line in lines:
        stripped = line.strip()
        if stripped == f"### {section_name}":
            in_section = True
            continue
        if in_section:
            if stripped.startswith("######"):
                break
            if not stripped:
                continue
            reader = csv.reader(io.StringIO(stripped))
            parsed = next(reader)
            if header is None:
                header = parsed
            else:
                rows.append(dict(zip(header, parsed)))
    return rows


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


def load_name_map():
    """Load the Jefit→Hevy name mapping from the community exercises.json."""
    path = "jefit_to_hevy_names.json"
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def main():
    csv_path = "jefit_data/StevenMcAvoy_20260611.csv"

    print("Loading exercise logs...")
    exercise_logs = load_section(csv_path, "EXERCISE LOGS ####################################")
    jefit_names = sorted({log["ename"].strip() for log in exercise_logs if log["ename"].strip()})
    print(f"  {len(jefit_names)} unique Jefit exercise names")

    print("Fetching Hevy exercise templates...")
    templates = fetch_all_exercise_templates()
    hevy_titles = sorted(t["title"] for t in templates)
    hevy_map = {t["title"].lower(): t["id"] for t in templates}
    print(f"  {len(hevy_titles)} Hevy templates")

    name_map = load_name_map()
    print(f"  {len(name_map)} community name mappings loaded")

    def find_match(name):
        # 1. Community name map → then exact lookup
        hevy_name = name_map.get(name)
        if hevy_name:
            tid = hevy_map.get(hevy_name.lower())
            if tid:
                return tid, "community_map"
            # Name mapped but not found as template — try partial
            for title, tid2 in hevy_map.items():
                if hevy_name.lower() in title or title in hevy_name.lower():
                    return tid2, "community_partial"
        # 2. Exact match on original name
        key = name.lower()
        if key in hevy_map:
            return hevy_map[key], "exact"
        # 3. Partial match on original name
        for title, tid in hevy_map.items():
            if key in title or title in key:
                return tid, "partial"
        return None, None

    out_path = "exercise_crosswalk.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["jefit_name", "hevy_template_id", "match_type", "notes"])
        unmatched_count = 0
        for name in jefit_names:
            tid, match_type = find_match(name)
            if tid is None:
                unmatched_count += 1
                writer.writerow([name, "", "", "NEEDS MAPPING"])
            else:
                writer.writerow([name, tid, match_type, ""])

    print(f"\nWrote {out_path}")
    print(f"  {len(jefit_names) - unmatched_count} matched, {unmatched_count} need manual mapping")

    # Also write a separate list of all Hevy templates for reference
    ref_path = "hevy_templates_reference.csv"
    with open(ref_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["hevy_template_id", "title", "type", "primary_muscle_group"])
        for t in sorted(templates, key=lambda x: x["title"]):
            writer.writerow([t["id"], t["title"], t.get("type", ""), t.get("primary_muscle_group", "")])
    print(f"  Wrote {ref_path} ({len(templates)} templates) for reference")


if __name__ == "__main__":
    main()
