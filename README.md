# The Pot

Automates tournament payouts for recurring [parry.gg](https://parry.gg) tournaments. Track a series, import its
history, set payout rules per game or event, and keep a profile for every player with their placements, what
you've paid them and what you still owe. Local-only (no auth), SQLite, run as a small test web server.

## How it works

- **Series**: parry.gg has no series object, so a series is either a *custom slug* (the quick link shared by
  every tournament in the series, e.g. `fridaybrawl`, matched exactly by the API) or, as a fallback, a *match
  pattern* (e.g. `Friday Night Brawl`) matched, ignoring case and punctuation, against each tournament's name
  and URL slug, whoever created it. A custom slug takes precedence when both are set.
  Individual tournaments can also be added by link or excluded.
- **Series code**: every series has a unique three-letter code (e.g. `FNB`). Links use it (`/series/FNB`; the old numeric
  URLs redirect) and tables show it, with the full name on hover. Leave it blank when adding a series to get one suggested
  from the name; existing series are given one automatically on upgrade, and you can change it in the series settings.
- **Rules** (per game, with optional single-event override): bracket fee, number of places paid, fixed pool,
  % of bracket fees into the pool, % of entry fees into the pool, and a split % per place (e.g. `50,30,20`).
  The tournament-wide entry fee is set on the series (overridable per tournament).
- **Pool** = fixed pool + bracket-fee share + entry-fee share. Ties share the combined amount of the places they occupy.
  Money is integer cents; splits are exact to the cent.
- **Payments**: each payout starts as *owed*; mark it paid per payout or settle everything owed to a player.
  Re-syncing never changes an amount you've already marked paid: a changed result shows a `≠` marker instead.
- **Venue**: each series can have a venue name and a cut made of up to three parts that add together: a flat amount per
  tournament, a % of entry fees collected (fee x attendees) and a % of bracket fees collected (each event's rule bracket
  fee x entrants; events without a rule count as $0). It is a separate payable per tournament (it does not change the
  prize pool), marked paid on the tournament page or the Venue ledger, and a tournament can override it or have no venue.
  Like player payouts, a paid amount is never changed by a re-sync; a changed calculation shows a `≠` marker.
- Payouts are only calculated for events whose placements are **finalised** (complete on parry.gg, with a single 1st
  place). Others are flagged on the dashboard, series, tournament and event pages until fixed on parry.gg and re-synced.
- **UI**: server-rendered, table-first, with every player, event, tournament, series and game linking to its page, plus
  events, games, players and payouts-ledger indexes. A few lines of vanilla JS (`app/static/table.js`) add click-to-sort
  and filter boxes; every page works without it.

## Run

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
cp .env.example .env             # put your parry.gg API key in PARRY_API_KEY
set -a; . ./.env; set +a
DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload
```

`.env` is git-ignored. The key is read at runtime only and is never stored in the database or logs.

Then open http://127.0.0.1:8000. The database is `data/pot.db` (git-ignored). The app has no login, so keep it on
localhost or a trusted network.

## Deploy (Docker Compose, home server)

```sh
printf '%s' "your-parry-api-key" > parry_api_key.txt   # git-ignored; mounted as a Docker secret
docker compose up -d --build
```

The database lives in `./data` (bind-mounted at `/data`). Update with `git pull && docker compose up -d --build`.

- **No auth**: the port is published on all interfaces so it's reachable on your LAN at `http://<host-ip>:8000`.
  Don't port-forward it on your router.
- **One worker only**: sync state is an in-process thread and lock, so don't add `--workers` or replicas.
- **Backups**: the DB holds payment records. Use SQLite's online backup, not a plain file copy (it runs in WAL mode):
  `sqlite3 data/pot.db ".backup 'backup/pot-$(date +%F).db'"`.

Check that discovery finds your tournaments from the command line (prints no secrets):

```sh
.venv/bin/python -m app.parry_client "" my-series-slug      # by custom slug
.venv/bin/python -m app.parry_client "Friday Night Brawl"   # or by name
```

The only code that talks to parry.gg is `app/parry_client.py` (gRPC via the official `parrygg` package); tests use a fake.
