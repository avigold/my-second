# mysecond — Project Context for Claude

## What this is

A chess analysis SaaS at **https://mysecond.app**. Players provide their Lichess or Chess.com username and run analysis jobs. The product is built around five tools:

1. **Fetch Games** — pulls game history from Lichess/Chess.com into a local SQLite cache
2. **Find Novelties** — beam search through the opening cache to find rare, engine-good moves the player hasn't faced; outputs a ranked, annotated PGN
3. **Analyse Habits** — finds positions where the player repeatedly plays a suboptimal move; outputs a PGN of inaccuracies for drilling
4. **Extract Repertoire** — reconstructs the player's opening tree from game history with frequency annotations
5. **Strategise** — given a player + opponent username, fetches both players' opening data, runs phase analysis on PGN games, and calls Claude (`claude-sonnet-4-6`) to write a preparation brief

**Pricing**: Free (3 analyses/month each tool, 1 Strategise), Pro at $9/month (unlimited). Stripe handles billing. Titled players (GM/IM/FM etc.) and admins get pro for free via `_effective_plan()`.

---

## Repository layout

```
my-second/
├── src/mysecond/          # Python CLI + analysis engine
│   ├── cli.py             # Click entry-point (mysecond CLI)
│   ├── cache.py           # SQLite cache for Lichess explorer API responses
│   ├── engine.py          # Stockfish UCI wrapper
│   ├── explorer.py        # Lichess masters/player API calls
│   ├── fetcher.py         # Downloads PGN from Lichess/Chess.com
│   ├── search.py          # Beam search + parallel root expansion
│   ├── score.py           # Composite scoring formula
│   ├── export.py          # PGN export
│   ├── habits.py          # Habit inaccuracy detection
│   ├── repertoire.py      # Repertoire extraction
│   ├── repertoire_extract.py
│   ├── game_phases.py     # Phase stats (endgame reach, draw rates by phase)
│   ├── strategise.py      # Orchestrates Strategise job + builds Claude prompt
│   ├── eval_cache.py      # Engine eval cache (in-progress)
│   └── models.py          # Shared dataclasses
│
├── web/                   # Flask web application
│   ├── server.py          # All Flask routes
│   ├── jobs.py            # JobRegistry (in-memory + PostgreSQL) + JobQueue
│   ├── auth.py            # OAuth: Lichess (PKCE), Chess.com, Google
│   ├── runner.py          # Subprocess launchers for CLI commands
│   ├── habits_parser.py   # Parses habits PGN output for the API
│   ├── pgn_parser.py      # Parses novelties PGN output for the API
│   ├── repertoire_parser.py  # Parses repertoire PGN for the API
│   ├── gunicorn.conf.py   # Minimal gunicorn config (no worker_abort hook)
│   ├── templates/         # Jinja2 HTML templates (extend base.html)
│   └── static/            # Static files (dist/, robots.txt, error.html, favicon.svg)
│
├── web/novelty-browser/   # React/Vite frontend (compiled to web/static/dist/)
│   └── src/
│       ├── main.jsx           # Entry point — mounts correct App by window.__APP__
│       ├── App.jsx            # Novelties browser
│       ├── RepertoireApp.jsx  # Repertoire browser
│       ├── HabitsBrowserApp.jsx
│       ├── HabitsPracticeApp.jsx
│       ├── StrategiseApp.jsx  # Strategise report viewer
│       └── components/
│           ├── NoveltyBoard.jsx
│           ├── NoveltyTable.jsx
│           ├── RepertoireBoard.jsx
│           ├── HabitsBrowserBoard.jsx
│           └── HabitsPracticeBoard.jsx
│
├── deploy.sh              # sudo -u deploy git pull + npm ci + npm run build + systemctl restart
├── pyproject.toml         # Python package config
└── CLAUDE.md              # This file
```

---

## Server

- **Host**: `mysecond.app` (SSH as `root@mysecond.app`)
- **Repo on server**: `/data/mysecond/`
- **Venv**: `/data/mysecond/.venv/`
- **Data/output**: `/data/mysecond-data/` (jobs output PGNs, logs)
- **Logs**: `/data/mysecond-data/logs/error.log`, `access.log`
- **Service**: `systemctl restart mysecond-web` (systemd, runs as `deploy` user)
- **Web server**: nginx reverse proxy → gunicorn on `127.0.0.1:5000`

### Gunicorn config (in systemd service file `/etc/systemd/system/mysecond-web.service`)
```
--workers 2 --threads 16 --bind 127.0.0.1:5000 --timeout 60 --graceful-timeout 30
```
Two workers so one stuck worker doesn't take the site down. Timeout 60s so stuck workers self-heal quickly.

### Deploy process
```bash
# From local machine:
git push origin main
ssh root@mysecond.app "cd /data/mysecond && sudo -u deploy git pull origin main && systemctl restart mysecond-web"

# If frontend changed:
ssh root@mysecond.app "cd /data/mysecond/web/novelty-browser && npm ci && npm run build"
# then restart

# Or just run:
ssh root@mysecond.app "bash /data/mysecond/deploy.sh"
```

### Environment (`.env` at `/data/mysecond/.env`)
```
DATABASE_URL=postgresql://mysecond:mysecond@localhost:5432/mysecond
FLASK_SECRET_KEY=...
ANTHROPIC_API_KEY=...
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
LICHESS_CLIENT_ID=...         # optional
CHESSCOM_CLIENT_ID=...        # optional
CHESSCOM_CLIENT_SECRET=...    # optional
STRIPE_SECRET_KEY=...
STRIPE_PRICE_ID=...
STRIPE_WEBHOOK_SECRET=...
APP_URL=https://mysecond.app
```

---

## Database (PostgreSQL, localhost:5432)

Three tables:

```sql
users (
    id UUID PRIMARY KEY,
    lichess_id TEXT UNIQUE,
    chesscom_id TEXT UNIQUE,
    google_id TEXT UNIQUE,
    username TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',  -- 'user' | 'admin' | 'GM' | 'IM' etc.
    created_at TIMESTAMPTZ
)

jobs (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    command TEXT,       -- 'fetch'|'search'|'habits'|'repertoire'|'strategise'|'import'
    params JSONB,
    status TEXT,        -- 'running'|'queued'|'done'|'failed'|'cancelled'
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    out_path TEXT,      -- path to output PGN or JSON file
    exit_code INTEGER,
    log_text TEXT
)

subscriptions (
    user_id UUID PRIMARY KEY,
    stripe_customer_id TEXT UNIQUE,
    stripe_subscription_id TEXT UNIQUE,
    plan TEXT,          -- 'free' | 'pro'
    status TEXT,        -- 'active' | 'trialing' | 'past_due' etc.
    current_period_end TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
)
```

**Key pattern**: `JobRegistry` loads all jobs into memory at startup (`_load_existing()`). All runtime reads come from memory; writes persist to both. The `connect_timeout=10` is appended to `DATABASE_URL` in `server.py` to prevent hanging on startup.

---

## Authentication

Three OAuth providers in `auth.py`:
- **Google** — OpenID Connect, always enabled if env vars set. Primary login method.
- **Lichess** — PKCE OAuth 2.0 (no client secret). Optional.
- **Chess.com** — OAuth 2.0. Optional.

All providers call `registry.upsert_user()` which inserts/updates the user row. Session stores `{"id": uuid, "username": str, "role": str, "platform": str}`.

`get_current_user()` returns the session user dict or None. `login_required` decorator redirects to `/login` if not authenticated. The `before_request` hook in `server.py` protects all routes except `_AUTH_EXEMPT_PATHS`.

---

## Job system

`JobRegistry` is a thread-safe in-memory store backed by PostgreSQL.

`JobQueue` limits concurrent *heavy* jobs (`search`, `habits`, `repertoire`, `strategise`) to `MAX_CONCURRENT = 4`. Light jobs (`fetch`, `import`) bypass the queue entirely.

Jobs are launched as subprocesses via `runner.py`. Stdout is streamed line-by-line into `job.queue` (a `queue.Queue`) and also appended to `job.log_lines`. The SSE endpoint `/api/jobs/<id>/stream` reads from the queue for live jobs, or replays `log_lines` for finished ones.

---

## Frontend (React/Vite)

`web/novelty-browser/` is a Vite project. The built output goes to `web/static/dist/`. Templates include the hashed asset filenames via `_vite_tags()` in `server.py`.

`main.jsx` mounts different root apps based on `window.__APP__` which is set inline in each template:
- `novelties` → `App.jsx`
- `repertoire` → `RepertoireApp.jsx`
- `habits-browser` → `HabitsBrowserApp.jsx`
- `habits-practice` → `HabitsPracticeApp.jsx`
- `strategise` → `StrategiseApp.jsx`

**Mobile pattern**: all board components have explicit `if (isMobile)` branches returning plain block `<div>` layouts (no flex-column trickery). `useIsMobile()` is always called as the first line of the component (before any early returns) to respect hooks rules.

**Build**:
```bash
cd web/novelty-browser && npm ci && npm run build
```

---

## Key routes

| Route | Description |
|---|---|
| `GET /` | Landing (logged out) or dashboard (logged in) |
| `GET /healthz` | 200 OK, no auth, used for monitoring |
| `GET /sitemap.xml` | XML sitemap (landing + pricing) |
| `GET /admin` | Admin dashboard (role = 'admin' only) |
| `GET /pricing` | Pricing page |
| `GET /account` | Account + subscription management |
| `POST /api/fetch` | Start a fetch-games job |
| `POST /api/search` | Start a novelty search job |
| `POST /api/habits` | Start a habits analysis job |
| `POST /api/repertoire` | Start a repertoire extraction job |
| `POST /api/strategise` | Start a strategise job |
| `POST /api/import-pgn` | Upload + start an import job |
| `GET /api/jobs/<id>/stream` | SSE stream of job stdout |
| `GET /api/jobs/<id>/novelties` | Parsed novelties from job output |
| `GET /api/jobs/<id>/habits` | Parsed habits from job output |
| `GET /api/jobs/<id>/repertoire` | Parsed repertoire tree from job output |
| `GET /api/jobs/<id>/strategise` | Strategise JSON result |
| `GET /api/admin/stats` | Admin: aggregate stats |
| `GET /api/admin/users` | Admin: all users with plan/job info |
| `GET /api/admin/jobs` | Admin: last 200 jobs across all users |
| `POST /api/admin/users/<id>/role` | Admin: set user role |
| `POST /api/stripe/webhook` | Stripe webhook (auth-exempt) |

---

## Plan / freemium gating

```python
_FREE_LIMITS = {"search": 3, "habits": 3, "repertoire": 3, "strategise": 1}

def _effective_plan(user):
    if user["role"] in {"admin"} | _TITLED_ROLES:  # GM, IM, FM, etc.
        return "pro"
    return registry.get_user_plan(user["id"])
```

`_TITLED_ROLES = {"GM", "WGM", "IM", "WIM", "FM", "WFM", "CM", "WCM", "NM"}`

---

## Strategise feature

`src/mysecond/strategise.py` orchestrates:
1. Parallel: fetch opening cache stats for both player and opponent
2. Parallel: `game_phases.analyze_game_phases()` for both players (endgame reach rate, draw rate by phase, avg game length)
3. `_build_opening_lines()` — BFS through cache to produce human-readable move sequences (e.g. "1.e4 c5 2.Nf3") instead of raw FENs
4. `_compute_style_profile()` — win rate, draw rate, decisive rate, opening diversity, top openings
5. `_build_prompt()` — assembles all data into a prompt with explicit format instructions
6. Calls `claude-sonnet-4-6` via `anthropic` SDK
7. Writes JSON output: `player_style`, `opponent_style`, `player_phase_stats`, `opponent_phase_stats`, `strategic_brief`, `battleground_positions`, etc.

The frontend (`StrategiseApp.jsx`) renders the `strategic_brief` field via `react-markdown`.

---

## Nginx config (`/etc/nginx/sites-enabled/mysecond`)

Key points:
- HTTPS only, HTTP redirects to HTTPS
- `error_page 502 503 504 /error.html` → served from `/data/mysecond/web/static/error.html` (static, no gunicorn needed). Auto-reloads every 15s.
- `robots.txt` served directly from filesystem
- `proxy_connect_timeout 5s`, `proxy_read_timeout 120s`
- SSE keepalive comments sent every 5s prevent the 120s read timeout from firing

---

## Known issues / pending work

- **Task #15**: Engine eval cache (`eval_cache.py` exists in src but not fully wired up)
- **Task #24**: Pre-fill forms with the user's saved username + platform (quality of life)
- **Task #25**: Lichess OAuth login (partially implemented in auth.py; needs env vars + UI)
- **OG image**: `og:image` meta tag is ready to add once a 1200×630 PNG is placed at `web/static/og.png`

---

## Common gotchas

- **Frontend changes require a rebuild** (`npm run build`) before they're visible. The server serves from `web/static/dist/`.
- **Hooks must come before early returns** in React components. `useIsMobile()` is always the first line.
- **`JobRegistry` connects to DB at startup** — if PostgreSQL is slow at that moment, the worker hangs. Fixed with `connect_timeout=10` in the DATABASE_URL and `--timeout 60` in gunicorn (stuck workers self-heal in ≤60s). Running 2 workers means one stuck worker doesn't cause an outage.
- **`_load_existing()` marks `running` jobs as `cancelled`** at startup (can't resume a subprocess from a previous process).
- **Gunicorn's gthread heartbeat comes from the main thread**, not request threads — so SSE streams don't affect the 60s timeout.
- **The deploy user (`deploy`) has the SSH key for GitHub**, not root. Use `sudo -u deploy git pull` on the server.
- **Strategise uses `claude-sonnet-4-6`** (not opus) for cost reasons. The API key comes from `ANTHROPIC_API_KEY` env var, injected server-side; clients never see it.
