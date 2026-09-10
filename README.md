# jefit-to-hevy

Migrate your Jefit workout history into [Hevy](https://hevy.com) via the
[Hevy public API](https://api.hevyapp.com/docs/) (requires Hevy Pro for an API key).

## What it does

Reads a Jefit CSV export and recreates each logged session as a Hevy workout:

- **Parses the Jefit export** — pulls the `WORKOUT SESSIONS`, `EXERCISE LOGS`,
  and `NOTES` sections out of the single multi-section CSV.
- **Maps exercises** — Jefit exercise names are matched to Hevy exercise
  template IDs via `exercise_crosswalk.csv` (seeded from a community name map
  plus exact/fuzzy matching, then hand-curated).
- **Collapses cumulative set logging** — Jefit logs each set addition as a new
  row holding all sets so far (`[s1]` → `[s1,s2]` → `[s1,s2,s3]`). These are
  collapsed to the final snapshot, including across superset interleaving,
  while genuinely separate instances of an exercise are preserved.
- **Consolidates repeated exercises** — multiple entries of the same exercise
  in one session (e.g. a top set logged separately from back-off sets) merge
  into a single Hevy exercise card with all sets.
- **Handles units & types** — converts lbs → kg, routes duration exercises
  (planks, holds) to `duration_seconds`, keeps bodyweight sets, and drops
  empty `0x0` placeholder sets.
- **Preserves timing & notes** — renders timestamps in local time
  (`America/Toronto`, DST-aware) so evening workouts keep their correct date,
  and carries exercise notes across.
- **Titles workouts** from the muscle groups trained.
- **Idempotent uploads** — skips any workout whose start instant already
  exists on the account, so re-runs never duplicate and existing workouts are
  never touched.

## Setup

1. Put your Hevy API key in a `.env` file (git-ignored):
   ```
   HEVY_API_KEY=your-key-here
   ```
   Get the key at https://hevy.com/settings?developer
2. Put your Jefit CSV export in `jefit_data/` (git-ignored).
3. Install dependencies:
   ```
   pip install requests python-dotenv
   ```

## Usage

```bash
# 1. Build/refresh the exercise crosswalk (writes exercise_crosswalk.csv
#    and hevy_templates_reference.csv). Fill in any blank template IDs.
python3 generate_crosswalk.py

# 2. Dry run — writes one JSON payload per workout to ./dry_run_output/
#    for review. Does not upload.
python3 migrate.py --dry-run

# 3. Test — upload a single workout to verify it looks right in Hevy.
python3 migrate.py --test

# 4. Full upload.
python3 migrate.py
```

## Files

| File | Purpose |
| --- | --- |
| `migrate.py` | Main converter + uploader |
| `generate_crosswalk.py` | Builds the exercise crosswalk from the Jefit export + Hevy templates |
| `exercise_crosswalk.csv` | Jefit exercise name → Hevy template ID mapping (hand-curated) |
| `hevy_templates_reference.csv` | Reference list of all Hevy exercise templates |
| `jefit_to_hevy_names.json` | Community Jefit→Hevy exercise name map used to seed the crosswalk |
