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
  The tournament-wide entry fee is part of the series' terms (overridable per tournament).
- **Terms over time**: a series' venue, entry fee and game rules are dated. When something changes (e.g. the series
  moves to a new venue), start new terms from that date on the series page: the game rules in effect then are copied
  so only what changed needs editing. Each tournament uses the terms in effect on its date, so earlier tournaments keep
  their old venue, fee and rules, and newly synced tournaments get the latest terms. Single-event overrides apply
  whatever the terms. Databases from before this get one set of terms "from the start" holding their current settings.
- **Pool** = fixed pool + bracket-fee share + entry-fee share. Ties share the combined amount of the places they occupy.
  Money is integer cents; splits are exact to the cent.
- **Payments**: each payout starts as *owed*; mark it paid per payout or settle everything owed to a player.
  Re-syncing never changes an amount you've already marked paid: a changed result shows a `≠` marker instead.
- **Merging players**: someone imported under two parry.gg accounts (e.g. `Alice` and `alice`) can be merged from
  either player's page; the Players page suggests tags that differ only in case or punctuation. The merged player's
  history and payouts show under the main one, including on future syncs, and Unmerge puts them back. Players who
  entered the same event can't be merged.
- **Venues** are tracked on their own (Venues page: each venue's series, tournaments held there, owed and paid). A
  venue's cut is made of up to three parts that add together: a flat amount per tournament, a % of entry fees collected
  (fee x attendees) and a % of bracket fees collected (each event's rule bracket fee x entrants; events without a rule
  count as $0). A series picks its venue in its terms (see above); a tournament can be held at a different venue, use
  a custom cut, or have no venue. The venue cut is a separate payable per tournament (it does not change the prize pool), marked
  paid on the tournament page, the venue's page or the venue payments ledger. Like player payouts, a paid amount (and
  the venue it was paid to) is never changed by a re-sync or a venue edit; a changed calculation shows a `≠` marker.
  Databases from before venues existed get one venue per distinct series venue name and cut on upgrade.
- **Square** (optional, card taps at the venue): on a tournament page, "Pull payments from Square" fetches the
  completed payments (net of refunds) taken that day, from midnight until 6am the next morning in your `TZ`, at the
  venue's Square location if it has one set. They are compared with what attendees should have paid (entry fee + the
  bracket fee of each event entered) so a count or total mismatch stands out. Link each payment to a player to see who
  is unpaid, short or overpaid. A card you've linked to a player is linked to them automatically at later tournaments
  they attend (by Square's card fingerprint), but only when that's unambiguous: a card you've linked to more than one
  player is never auto-linked, and a second payment from the same card in one night stays unlinked (someone paying for
  a friend). Auto-links are tagged "auto". Payments that aren't fees can be ignored, and a payment is only ever counted
  for one tournament.
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

For Square, create an app in the [Square Developer dashboard](https://developer.squareup.com/apps) and put its access
token (Credentials page; the app id isn't needed) in `SQUARE_API_KEY`, and set `SQUARE_ENV` to match it: `sandbox`
for the Sandbox tab's token, `production` for the Production tab's (for real payments). `SQUARE_ENV` is required:
each token is only accepted by its own Square environment (the other answers 401), so the app won't guess, and the
tournament page shows which environment is in use. It only reads payments (`PAYMENTS_READ`). After changing `.env`,
recreate the container (`docker compose up -d`); a restart keeps the old values. Set `TZ` to your local time zone
(e.g. `Australia/Hobart`) so each tournament's day is right. If you have more than one Square location, put the venue's
location id on its venue page. The only code that talks to Square is `app/square_client.py`.

Then open http://127.0.0.1:8000. The database is `data/pot.db` (git-ignored). The app has no login, so keep it on
localhost or a trusted network.

## Test before pushing

A push to `main` deploys, so try changes locally first. `scripts/test-deploy.sh` runs the image built from your
working tree (unpushed and uncommitted changes included) on port 8001 of this machine, with its own data volume.
Like production it listens on all interfaces and has no login, so limit who can reach port 8001 with a firewall
(Docker's published ports skip host firewall INPUT rules: filter them in the `DOCKER-USER` chain).

```sh
scripts/test-deploy.sh up                                  # run the tests, build, start
POT_SERVER=homeserver scripts/test-deploy.sh snapshot      # copy the live database over SSH (read-only)
scripts/test-deploy.sh load                                # load the newest snapshot, run the upgrade, compare totals
scripts/test-deploy.sh down                                # stop (reset also deletes the test data)
```

`load` prints player and venue owed/paid totals before and after the upgrade and fails if they differ, so a schema
migration that would reprice anything shows up before it reaches production. `snapshot` uses SQLite's online backup
into the production container's `/tmp`, so the live `/data` is never written to. Set `POT_CONTAINER` if the container
isn't found automatically, and `POT_DOCKER="sudo docker"` if the server needs it. Snapshots hold real payment records:
they stay in `data/snapshots/` (git- and docker-ignored). Anything you do in the test copy, like marking payouts paid,
only changes the copy. The test stack always uses the Square sandbox.

## Deploy (Docker Compose / Dockge, home server)

CI builds the image on every push to `main` and publishes it to `ghcr.io/monday-j/the-pot` (private by default).

1. On the server, log in once so it can pull the private image, using a GitHub personal access token with
   `read:packages`: `echo <token> | docker login ghcr.io -u monday-j --password-stdin`.
2. Create the stack (in Dockge: new stack, paste `compose.yaml`) and add a `.env` next to it containing
   `PARRY_API_KEY=<your parry.gg key>`, plus `TZ` and the optional Square settings (see `.env.example`).
3. Start it. Update later with Dockge's **Update** button, or `docker compose pull && docker compose up -d`.

The database lives in the `pot-data` named volume, mounted at `/data`.

- **No auth**: the port is published on all interfaces so it's reachable on your LAN at `http://<host-ip>:8000`.
  Don't port-forward it on your router.
- **One worker only**: sync state is an in-process thread and lock, so don't add `--workers` or replicas.
- **Backups**: the DB holds payment records. Use SQLite's online backup, not a plain file copy (it runs in WAL mode):
  `docker compose exec pot python -c "import sqlite3; s=sqlite3.connect('/data/pot.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"`
  then `docker compose cp pot:/data/backup.db ./pot-$(date +%F).db`.

Check that discovery finds your tournaments from the command line (prints no secrets):

```sh
.venv/bin/python -m app.parry_client "" my-series-slug      # by custom slug
.venv/bin/python -m app.parry_client "Friday Night Brawl"   # or by name
```

The only code that talks to parry.gg is `app/parry_client.py` (gRPC via the official `parrygg` package); tests use a fake.
