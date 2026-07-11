# Nutrition Ledger

A self-hosted nutrition/fitness tracker built around a Telegram bot (the
primary day-to-day logging surface) and a single-page web dashboard, both
backed by the same SQLite database and the same orchestration logic. Free-tier
APIs (Gemini, USDA FoodData Central, CalorieNinjas, Open Food Facts) do the
actual nutrition lookups; Gemini fills gaps and never stores anything the
database/API layer already has as ground truth.

## Quick start

```bash
cp .env.example .env   # fill in GEMINI_API_KEY at minimum; see comments for the rest
pip install -r requirements.txt
python -m scripts.check_setup   # verifies every configured API key actually works
python run.py                   # starts both the bot and the web dashboard
```

The web dashboard serves on `API_PORT` (default 8001). The bot uses
long-polling, so nothing needs to be publicly reachable.

## Architecture

```
core/
  db.py               SQLite access layer (WAL mode, shared by bot + web).
                       Canonical data model: every food stores nutrients
                       PER 100G + a `grams` quantity, never a pre-multiplied
                       absolute value -- absolute nutrients are always
                       nutrients_per_100g * grams/100, computed on read.
  gemini/              One Gemini call per file, re-exported from __init__:
    client.py            Shared HTTP plumbing (the only file touching `requests`)
    extraction.py         STAGE 1: what was eaten + how much (no nutrition)
    fill.py               STAGE 2: per-100g nutrient profile (API data = ground truth)
    review.py             ON-DEMAND: sanity_check_meal(), refine_draft()
    rating.py             rate_meal(), generate_daily_report(),
                           generate_weekly_report(), answer_question(),
                           transcribe_voice()
  logging_service.py   Orchestrates the whole pipeline -- the ONE module
                        the bot and web API both call into. Neither talks
                        to nutrition_apis.py or gemini/ directly.
  nutrition_apis.py    USDA / CalorieNinjas / Open Food Facts clients.
  food_match.py        Fuzzy cache matching (difflib, 0.82 threshold) --
                        this is what makes corrections "stick" over time:
                        confirm_meal() and edit_item() both teach the cache.

bot/telegram_bot.py    Telegram front-end. Every handler is wrapped in
                        `safe_handler` (turns any unhandled exception into
                        a visible error message instead of a silently
                        stuck/frozen chat) and `staged_build_and_offer`
                        edits one message through each real pipeline stage
                        (extraction, then fill) rather than one long silence.

backend/api_server.py  Flask REST API + serves web/index.html.

web/index.html         Single-file SPA (inline CSS/JS, no build step,
                        no framework). Editorial aesthetic: Young Serif
                        headings, IBM Plex Mono for numbers, warm
                        cream/paper palette -- deliberately not a generic
                        SaaS dashboard look. See the `.screen` /
                        `showScreen()` shell described below.
```

### Meal-logging pipeline (both bot and web call the same functions)

1. `extract_only()` -- Gemini identifies WHAT + HOW MUCH from text/photo/voice.
   Never touches nutrition.
2. `resolve_draft()` -- deterministic API lookups (cache -> USDA ->
   CalorieNinjas -> Open Food Facts), then Gemini's `fill_nutrition()` covers
   whatever the APIs didn't, capped at 2 calls total per meal regardless of
   ingredient count (one batch + one batched retry for stragglers -- never
   one call per ingredient, quota is tight: ~50-500 Gemini requests/day on
   the free tier).
3. Deterministic Atwater check (`_reconcile_kcal`, zero LLM cost): any
   LLM-estimated item whose kcal doesn't reconcile with
   `4*protein + 4*carbs + 9*fat` within ~15% gets kcal recomputed from the
   macros. Never touches real API data or explicit user corrections.
4. `confirm_meal()` -- persists, teaches the food cache, requests a healthiness
   rating (1-10, a 4th distinct Gemini call).
5. On demand only (user-invoked, never automatic): `sanity_check()` reviews a
   resolved draft for internal consistency; `refine_meal()` applies a
   free-text correction or answers a concern -- distinguishes an explicit
   instruction ("it was 150g", applied exactly) from a question ("does this
   look right?", judged like a sanity check) and cascades corrections across
   correlated macros. Attempts Google Search grounding first, with automatic
   fallback to the normal call.

### Tracking-tier awareness

A day with nothing logged is NOT a 0-calorie day -- `_day_tracking_tier()` in
`logging_service.py` classifies every day as `tracked` (>=50% of kcal
target), `partial` (some data, under 50%), or `untracked` (~nothing logged).
Weekly averages/scores (`build_week_context`) are computed ONLY over
tracked+partial days -- an untracked day contributes nothing, not a silent
zero. Daily/weekly report generation skips the Gemini call entirely for
weeks/days with zero trackable data. Bake this same tier logic in anywhere
else that aggregates across days, if you add more.

## Current features

- Telegram bot: natural-language logging (text/photo/voice), staged live
  editing of one message through the pipeline, on-demand double-check +
  reply-to-correct, saved meals, water logging, weekly report.
- Web dashboard: same logging pipeline via a staged quick-add flow, energy
  ring + macro/micronutrient tracking against configurable targets, calendar
  date picker, trend charts (any tracked nutrient, 7/30 day), daily report
  (score /100, auto-generated for today, manual refresh for other days),
  weekly report (week-on-week comparison), CSV/JSON export, meal-slot
  editing, saved meals, custom nutrient definitions (fully modular --
  something added via Settings is genuinely requested from Gemini's fill
  call too, not just displayed).
- Multi-screen shell (`app-shell` / `.screen` / `showScreen()` in
  `web/index.html`): sidebar has 3 nav items (Nutrition, Health, Workouts).
  Nutrition and Workouts are fully built. Health is still a placeholder --
  see Roadmap below.
- Workouts screen: session-based strength/cardio logging (`workouts` ->
  `workout_exercises` -> `workout_sets` in `core/db.py`, pure CRUD/
  aggregation, no Gemini involved). Volume (`reps * weight_kg`) and cardio
  duration/distance are always computed on read from raw sets, same
  never-store-a-derived-value convention as meal nutrients. Deleting the
  last set of an exercise (or last exercise of a session) cascades cleanly
  so the log never accumulates empty rows. UI: date-scoped session view
  (own date nav/calendar, mirroring the nutrition one), quick-add with a
  strength/cardio toggle and an exercise-name datalist for repeat lifts,
  volume trend chart, day/workout streak, a personal-records board (best
  set ever logged per exercise), and a read-only history list for other
  days. Endpoints live under `/api/workouts/*` in `backend/api_server.py`.

## Roadmap: Health/BCA screen

The user's stated goal is a complete fitness tracker: nutrition (done),
workouts (done), and **medical results / body composition (BCA) / water
tracking** (new screen), all interlinked in one UI.

### What's already scaffolded

- `web/index.html`: `#screen-health` exists as a sibling of
  `#screen-nutrition` / `#screen-workouts` inside `<main>`, toggled by
  `showScreen(name)` (pure client-side visibility, no routing/reload --
  in-memory state on one screen survives a trip to another and back). The
  placeholder gets its own one-time entrance animation on first visit
  (`animatedScreens` cache in the boot script). The nav item carries a
  `data-screen="health"` attribute; wiring it up is: give it content inside
  `#screen-health`, don't need to touch `showScreen()` itself.
- The sidebar nav SVG icon for Health (a heartbeat icon) is already in
  place -- reuse/restyle if the actual content ends up wanting different
  iconography.
- The Workouts screen (`core/db.py`'s workouts/workout_exercises/
  workout_sets section, the `/api/workouts/*` routes, and the
  `#screen-workouts` markup/JS in `web/index.html`) is a concrete, recent
  example of taking a screen from placeholder to fully built within this
  same file layout -- worth skimming as a template for Health's own
  data model + endpoints + screen.

### What's not started (by design -- out of scope for this session)

- **Data model**: no schema for medical results, body composition (BCA),
  or water intake beyond the bot's existing crude `/water` command (logs
  into the `meals` table under a `"water"` meal_slot with an ad hoc
  `water_ml` nutrient key -- probably wrong once there's a real Health
  screen; likely wants its own table(s)).
- **Backend endpoints** for whatever that schema ends up being.
- **Actual screen UI** -- the placeholder is literally just an icon + text.
- **Cross-screen interlinking** the user asked for (e.g. does a workout
  affect calorie targets on the Nutrition screen? does a BCA weigh-in update
  the `bodyweight_kg` profile value that `per_kg_bodyweight` nutrient targets
  already key off?) -- this needs product decisions, not just engineering,
  before it can be built.

Before starting on Health, it's worth the user deciding (or the next
session asking up front): what specifically goes on the Health screen
(which medical results? what does "BCA" mean here precisely -- a specific
device/report format to parse, or manual entry?) -- that shapes the schema
a lot and is worth pinning down before writing code.
