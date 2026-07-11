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
  Nutrition is fully built. Health and Workouts are placeholder screens --
  see Roadmap below.

## Roadmap: Health/BCA + Workouts screens

The user's stated goal is a complete fitness tracker: nutrition (done),
**medical results / body composition (BCA) / water tracking** (new screen),
and **workouts** (new screen), all interlinked in one UI.

### What's already scaffolded

- `web/index.html`: `#screen-health` and `#screen-workouts` containers exist
  as siblings of `#screen-nutrition` inside `<main>`, toggled by
  `showScreen(name)` (pure client-side visibility, no routing/reload --
  in-memory state on one screen survives a trip to another and back). Each
  placeholder gets its own one-time entrance animation on first visit
  (`animatedScreens` cache in the boot script). Nav items carry
  `data-screen="health"` / `data-screen="workouts"` attributes; wiring a new
  screen is: give it a `#screen-<name>` container, don't need to touch
  `showScreen()` itself.
- Sidebar nav SVG icons for both new sections are already in place (a
  heartbeat icon for Health, a dumbbell-ish icon for Workouts) --
  reuse/restyle if the actual content ends up wanting different iconography.

### What's not started (by design -- out of scope for this session)

- **Data model**: no schema for medical results, body composition (BCA),
  water intake beyond the bot's existing crude `/water` command (logs into
  the same `meals` table under a `"water"` meal_slot with an ad hoc
  `water_ml` nutrient key -- probably wrong once there's a real Health
  screen; likely wants its own table(s)), or workouts (exercises, sets,
  reps, duration, whatever "workout" means for this user -- needs
  clarifying).
- **Backend endpoints** for whatever that schema ends up being.
- **Actual screen UI** -- the placeholders are literally just an icon +
  text.
- **Cross-screen interlinking** the user asked for (e.g. does a workout
  affect calorie targets on the Nutrition screen? does a BCA weigh-in update
  the `bodyweight_kg` profile value that `per_kg_bodyweight` nutrient targets
  already key off?) -- this needs product decisions, not just engineering,
  before it can be built.

### Recommended session strategy

The user asked directly whether to use a separate Claude Code chat for this
next phase. **Yes, start a new session for it.** Reasons:

1. This nutrition-tracking build has accumulated a lot of context (pipeline
   design, quota-consciousness, the editorial design system, the tracking-
   tier logic) that a fresh session doesn't need to re-derive to work on
   BCA/workout screens -- those are close to a clean-slate feature area.
2. It keeps the two concerns independently iterable: if the user wants a
   nutrition-side tweak later, they can come back to a session that still
   has full nutrition context without it being buried under unrelated
   workout-screen back-and-forth (and vice versa).
3. This file is the handoff: point the new session at this README first.
   The architecture section + pipeline description + "what's already
   scaffolded" above should be enough for it to orient without replaying
   this session's history.

Before starting that session, it's worth the user deciding (or the new
session asking up front): what specifically goes on the Health screen
(which medical results? what does "BCA" mean here precisely -- a specific
device/report format to parse, or manual entry?) and what "workouts" means
for this user (which sport/style, what fields matter -- these shape the
schema a lot and are worth pinning down before writing code).
