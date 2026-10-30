# The Pot

Automates tournament payouts for recurring [parry.gg](https://parry.gg) tournaments. Track a series, import its
history, set payout rules per game or event, and keep a profile for every player with their placements, what
you've paid them and what you still owe. For your LAN, behind one shared password; SQLite, run as a small web server.

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
  % of bracket fees into the pool, % of venue fees into the pool, and a split % per place (e.g. `50,30,20`).
  The tournament-wide venue fee is part of the series' terms (overridable per tournament).
- **Terms over time**: a series' venue, venue fee and game rules are dated. When something changes (e.g. the series
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
  venue's cut is made of up to three parts that add together: a flat amount per tournament, a % of venue fees collected
  (fee x attendees) and a % of bracket fees collected (each event's rule bracket fee x entrants; events without a rule
  count as $0). A series picks its venue in its terms (see above); a tournament can be held at a different venue, use
  a custom cut, or have no venue. The venue cut is a separate payable per tournament (it does not change the prize pool), marked
  paid on the tournament page, the venue's page or the venue payments ledger. Like player payouts, a paid amount (and
  the venue it was paid to) is never changed by a re-sync or a venue edit; a changed calculation shows a `≠` marker.
  Databases from before venues existed get one venue per distinct series venue name and cut on upgrade.
- **Square** (optional, card taps at the venue): on a tournament page, "Pull payments from Square" fetches the
  completed payments (net of refunds) taken that day, from midnight until 6am the next morning in your `TZ`, at the
  venue's Square location if it has one set. They are compared with what attendees should have paid (venue fee + the
  bracket fee of each event entered) so a count or total mismatch stands out. Link each payment to a player to see who
  is unpaid, short or overpaid. Once you've linked a card to a player (by Square's card fingerprint), all its payments
  are linked to them, at every tournament, past and future: a second payment the same night, or one where they didn't
  enter, then shows them as overpaid. Payments you've linked or unlinked by hand are never changed, and a card you've
  linked to more than one player (someone paying for a friend) isn't linked automatically any more. Auto-links are
  tagged "auto". What matters most is that the total taken covers what was expected: mark a payment you can't or
  needn't match to anyone **No player needed** and it still counts, without asking to be linked. Mark payments that
  aren't tournament fees (e.g. a drink) **Not a fee** to leave them out. A payment is only ever counted for one
  tournament. A tournament needs attention when less was taken than expected, a linked player paid too little, or
  payments are neither linked nor marked. When a tournament is short and you'll make up the difference yourself,
  press **I'll cover it** (with a note): it stops being flagged, unless more goes missing later.
  When an entrant's fee was sorted out some other way (someone else paid for them, or it was waived), mark them
  **Sorted out** in the "Who paid" table with a note saying how: they no longer count as unpaid or missing.
- **What each payment was for**: with orders and items authorised, pulling payments also fetches each payment's order
  (its line items) and, when it sells something new, the item library. On **Square items** (linked from each
  tournament's Square section) give each item you sell a meaning, matched by its name (custom amounts share one):
  venue fee, bracket fee or catch-up (a player paying for a week they missed; it counts toward the week it's paid
  at) count toward the takings; "not a fee" (drinks, food) is
  left out. Line items show under each payment. A payment split across both counts only its fee share, pro rata, and
  one that was all drinks isn't counted or asked to be linked. Items without a meaning still count, as before.
  A grant from before this (payments and payouts only) keeps working without line items: authorise again (the steps
  under "Square: read-only access") to turn them on.
- **Card fees**: each Square payment's processing fee (as Square reports it, tax adjustments included) is stored and
  shown, with gross, fees and net per tournament and per series. Square adds the fee a little after the payment, so
  a fresh payment shows "pending" until a later pull fills it in.
- **Bank**: reconcile by balance. On the Bank page, enter the bank balance at the end of a day: it's compared with
  what the app expects (the last reconciled balance, plus deposits, minus payouts to players and venues marked paid
  since), and any difference is recorded with your note. The first reconciliation sets the opening balance. Deposits
  are Square's payouts ("Import payouts from Square", or the automatic pull; uses the `PAYOUTS_READ` permission
  granted during setup), each with Square's breakdown of the payments, refunds and fees it covered, plus any you add
  by hand from the statement. Failed payouts don't count. Cash and other money through the account aren't tracked:
  note them against the difference.
- **Locking**: a reconciliation locks everything dated on or before its day: deposits, and payouts to players and
  venues it took in (they can't be undone or edited), and every tournament up to then with no issues left (no
  payments missing or to link, all events final). Tournaments with issues stay editable (payment links, ignores,
  "sorted out") and lock as soon as they're clear. A locked tournament's settings, payouts and payment links can't
  change and it isn't recomputed. Syncing from parry.gg and pulling from Square skip locked tournaments, so a long
  series only re-reads what's still open. Undo the latest reconciliation, one at a time, to unlock (and re-import).
- **Weekly reminder** (optional): set `SMTP_HOST` (plus `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` or
  `SMTP_PASSWORD_FILE`, `SMTP_FROM`) and `REMINDER_TO` (comma-separated) to get an email once a week, at
  `REMINDER_HOUR` (default 9) on `REMINDER_DAY` (default `mon`) in your `TZ`, reminding you to process payments: when
  the bank was last reconciled and what's moved since, tournaments with Square payments missing or to link, events to
  finalise, and what's owed. Set `POT_URL` to have it link to the pages. Port 587 needs STARTTLS (465 is TLS from the
  start). `python -m app.mail preview` prints this week's email; `python -m app.mail test` sends it now. A reminder
  missed by more than a day (the app was down) is skipped, not sent late.
- **Pulling a whole series**: "Pull Square payments (all tournaments)" on the series page pulls every tournament in the
  background with a single Square query per location (spanning all their dates, then filing each payment under the
  night it belongs to), so a season costs a few requests rather than one per night. Set `SQUARE_AUTO_PULL_HOURS`
  (e.g. `6`) to do this automatically for the last 14 days of every tracked series; leave it blank to turn it off.
  Only one Square pull runs at a time, and the series page shows the last result.
- Payouts are only calculated for events whose placements are **finalised** (complete on parry.gg, with a single 1st
  place). Others are flagged on the dashboard, series, tournament and event pages until fixed on parry.gg and re-synced.
- **UI**: server-rendered, table-first, with every player, event, tournament, series and game linking to its page, plus
  events, games, players and payouts-ledger indexes. A few lines of vanilla JS (`app/static/table.js`) add click-to-sort
  and filter boxes; every page works without it.

## Run

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
cp .env.example .env             # set POT_PASSWORD, and your parry.gg API key in PARRY_API_KEY
set -a; . ./.env; set +a
DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload
```

`.env` is git-ignored. The key is read at runtime only and is never stored in the database or logs.

Dependencies are pinned with hashes: `requirements.txt` (the app) and `requirements-dev.txt` are compiled from the
`.in` files, so change those and recompile with `.venv/bin/pip-compile --generate-hashes --strip-extras --allow-unsafe
requirements.in` (then the same for `requirements-dev.in`). Dependabot opens weekly PRs for these, the Docker base
image digest and the GitHub Actions SHAs, and CI fails on any known vulnerability (`pip-audit`).

### Square: read-only access

The app only ever reads from Square, and it's set up so the server can't do anything else. Square's personal
access tokens give "unrestricted" access to the whole account and can't be limited, so the app refuses them. Instead
you authorise the app once, through Square OAuth, for **only** `PAYMENTS_READ`, `PAYOUTS_READ`, `ORDERS_READ` and
`ITEMS_READ` (orders and the item library say what each payment was for). The server keeps
the app's client id and secret and that grant's refresh token, and swaps them for short-lived access tokens held in
memory only, renewed at least every 6 days. Square only issues tokens with the granted read scopes, so even with
everything on the server, nobody can take a payment, refund money or change anything. Every new token's scopes are
checked with Square before use, and any token that can do more than read is refused. The client can only send GET
requests (apart from the two OAuth token calls). Nothing Square-related is stored in the database or logs.

One-time setup (per environment, `sandbox` or `production`):

1. In the [Square Developer Console](https://developer.squareup.com/apps), open the app, then **OAuth** for that
   environment. Set the **Redirect URL** to `https://monday-j.github.io/the-pot/square-oauth.html` (a static page in
   `docs/` that only shows the one-time code to copy; it loads and sends nothing). Copy the **Application ID** and
   **Application secret**.
2. In the server's `.env`, set `SQUARE_ENV`, `SQUARE_CLIENT_ID` and `SQUARE_CLIENT_SECRET`, then recreate the
   container (`docker compose up -d`).
3. Run `docker compose exec pot python -m app.square_oauth url`, open the link it prints, signed in as the account
   owner, and check that Square lists only permission to read payments, payouts, orders and items before approving. (For sandbox,
   open the sandbox test account's dashboard from the Developer Console first.)
4. Within 5 minutes, run the `python -m app.square_oauth exchange ...` command the redirect page shows (prefix it
   with `docker compose exec pot`). It checks the grant is read-only and prints `SQUARE_REFRESH_TOKEN=...` once: add it
   to `.env` and recreate the container again. `python -m app.square_oauth status` confirms the scopes.
5. If `.env` still has the old `SQUARE_API_KEY`, remove it (the app refuses to talk to Square while it's set), and
   **replace** that personal access token in the Developer Console (Credentials) so the copy that was on the server
   stops working.

Keep `.env` readable only by the deploy user. The authorisation code on the redirect page is single-use, expires in
5 minutes, and is useless without the client secret. If the server is ever compromised, disconnect the app in the
Square Dashboard (or replace the application secret): that cuts off its read access straight away. What can't be
prevented is someone with server access *reading* payment data (amounts, card brand and last 4 digits), since the app
needs that. The Square account login and Developer Console still have full access, so protect them with two-factor
authentication.

`SQUARE_ENV` must match the credentials (each environment only accepts its own), and the tournament page shows which
is in use. After changing `.env`, recreate the container (`docker compose up -d`); a restart keeps the old values. Set
`TZ` to your local time zone (e.g. `Australia/Hobart`) so each tournament's day is right. If you have more than one
Square location, put the venue's location id on its venue page. The only code that talks to Square is
`app/square_client.py` with `app/square_auth.py`. Square publishes no rate limits, so requests are spaced at least
250ms apart, and a rate-limited (`429`), server-error or dropped request is retried up to 5 times with exponential
backoff and jitter (honouring `Retry-After`). Each retry is logged.

Then open http://127.0.0.1:8000 and log in with `POT_PASSWORD`. The database is `data/pot.db` (git-ignored).

### Login and request checks

- **Password**: every page needs the shared password in `POT_PASSWORD` (or a file named by `POT_PASSWORD_FILE`).
  With none set, the app refuses every page (503) rather than run open. Use a long random one.
- **Session**: logging in sets an HttpOnly, SameSite=Lax cookie that lasts 30 days. It's signed with a random key
  the app creates in `DATA_DIR/session.key` (mode 0600, never in the database), mixed with a hash of the password,
  so **changing the password logs everyone out**, and deleting `session.key` does too. Log out from the nav bar.
- **Failed logins**: 10 failures from one address within 15 minutes block further tries from it for up to 15 minutes.
  Behind Docker's port publishing, every client can look like the same address, so a flood of bad guesses can lock
  you out for 15 minutes too. Restarting the container clears it.
- **Cross-site requests**: form posts that a browser marks as coming from another site (`Sec-Fetch-Site`, `Origin`
  or `Referer`) are refused, so a web page you visit can't submit forms to the app.
- **Host names** (optional): set `POT_ALLOWED_HOSTS` to the names and addresses you use to reach the app, e.g.
  `192.168.1.10,pot.lan,localhost,127.0.0.1`. Requests for any other host get a 400, which blocks DNS-rebinding
  pages. Include `127.0.0.1` for the container healthcheck.
- **HTTPS**: if you put the app behind an HTTPS reverse proxy, set `POT_COOKIE_SECURE=1`.
- Every page is sent with a Content Security Policy (scripts and styles from the app only, no framing),
  `nosniff`, `Referrer-Policy: same-origin`, and no API docs or `Server` header. `/healthz` is open for the
  healthcheck and shows nothing.

## Test before pushing

A push to `main` deploys, so try changes locally first. `scripts/test-deploy.sh` runs the image built from your
working tree (unpushed and uncommitted changes included) on port 8001 of this machine, with its own data volume.
Like production it listens on all interfaces, and it uses `POT_PASSWORD` from your `.env`. A loaded snapshot holds real
payment data, so limit who can reach port 8001 with a firewall too (Docker's published ports skip host firewall INPUT
rules: filter them in the `DOCKER-USER` chain).

```sh
scripts/test-deploy.sh up                                  # run the tests, build, start
POT_SERVER=homeserver scripts/test-deploy.sh snapshot      # copy the live database over SSH (read-only)
scripts/test-deploy.sh load                                # load the newest snapshot, run the upgrade, compare totals
scripts/test-deploy.sh down                                # stop (reset also deletes the test data)
```

`load` prints player and venue owed/paid totals, Square takings, deposits and the last reconciled bank balance before
and after the upgrade and fails if they differ, so a schema migration that would reprice anything shows up before it
reaches production. Once the ledger exists (`app/ledger.py`), it also checks the books balance and lists each tournament
whose amount owing changed, and why. `snapshot` uses SQLite's online backup
into the production container's `/tmp`, so the live `/data` is never written to. Set `POT_CONTAINER` if the container
isn't found automatically, and `POT_DOCKER="sudo docker"` if the server needs it. Snapshots hold real payment records:
they stay in `data/snapshots/` (git- and docker-ignored, readable only by you). Anything you do in the test copy, like marking payouts paid,
only changes the copy. The test stack always uses the Square sandbox.

## Deploy (Docker Compose / Dockge, home server)

CI builds the image on every push to `main` and publishes it to `ghcr.io/monday-j/the-pot` (private by default).

1. On the server, log in once so it can pull the private image, using a GitHub personal access token with
   `read:packages`: `echo <token> | docker login ghcr.io -u monday-j --password-stdin`.
2. Create the stack (in Dockge: new stack, paste `compose.yaml`) and add a `.env` next to it containing
   `POT_PASSWORD=<a long random password>` and `PARRY_API_KEY=<your parry.gg key>`, plus `TZ` and the optional
   `POT_ALLOWED_HOSTS` and Square settings (see `.env.example`).
3. Start it. Update later with Dockge's **Update** button, or `docker compose pull && docker compose up -d`.

The database lives in the `pot-data` named volume, mounted at `/data`.

- **LAN only**: the port is published on all interfaces so it's reachable on your LAN at `http://<host-ip>:8000`,
  behind the password. It's plain HTTP, so still don't port-forward it on your router.
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
