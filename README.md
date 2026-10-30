# The Pot

Runs the money for recurring [parry.gg](https://parry.gg) tournaments: what each entrant owes and paid on the night,
who still owes, what to pay the winners and the venue, and whether the bank agrees. For your LAN, behind one shared
password; SQLite, run as a small web server.

The app follows the night through three steps, **Collect → Pay out → Reconcile**. Series, players, venues and events
are the records behind them, all linked to each other.

## The run sheet

The home page says what to do next. Every tournament sits in the first lane that applies to it:

- **Waiting on parry.gg**: it hasn't happened yet, or its placements aren't final (complete on parry.gg, with a single
  1st place), so payouts can't be worked out. Fix it on parry.gg and re-sync.
- **Collect**: its takings need you: Square payments to link to players, or money missing that nobody named owes.
- **Pay out**: a winner or the venue is still owed.
- **Done**: nothing left, and it locks once a bank reconciliation takes it in.

Players who still owe for a night don't hold it up: that's followed up under **Owed to us**. Tiles across the top show
what players owe us, what we owe (against the bank), the bank's estimated balance with Square money still on its way,
and anything filed as unexplained.

## 1. Collect

Each tournament page has three numbered steps: **Takings**, **Payouts** and **Summary**, with its settings (venue fee
override, venue and cut, whether takings are tracked, exclude) in a Setup panel at the bottom.

- **Pull payments from Square** fetches the completed payments (net of refunds) taken that day, from midnight until
  6am the next morning in your `TZ`, at the venue's Square location if it has one. Set `SQUARE_AUTO_PULL_HOURS` (e.g.
  `6`) to pull the last 14 days of every tracked series automatically, or pull a whole series from its page (one
  Square query per location for the whole season).
- **Link each payment to the player who made it.** Once a card is linked to a player (by Square's card fingerprint),
  all its payments are linked to them, at every tournament; links made by hand are never changed, and a card linked to
  more than one player isn't linked automatically any more. Mark a payment you can't or needn't match **No player
  needed** (it still counts toward the night) and one that isn't a fee, like a drink, **Not a fee**.
- **What each entrant owes** is the venue fee plus the bracket fee of each event they entered. What a player pays on a
  night pays that night first, then the oldest week they still owe ("paid late": people pay for missed weeks with the
  ordinary fee items), and anything left is **credit** for their next tournament. The "Who paid" table shows each
  entrant's status: paid, paid late, paid by credit, sorted out, written off, owing, or not matched yet (when the
  night has payments nobody's linked to, only the total matters).
- **Settle** an entrant paid some other way: a bank transfer or cash, paid by another player, waived or written off.
  When someone paid more than they owed and others that night still owe, **Paid for someone?** suggests who it was
  probably for (exact amounts first, people they've paid for before first); confirming records it as paid by another
  player. Money missing that nobody named owes can be **written off** with a reason.
- **What each payment was for**: with orders and items authorised, each payment's line items show under it. Give each
  item you sell a meaning under **Setup → Square items** (venue fee, bracket fee, catch-up, or not a fee) and
  payments mixing fees and drinks count only their fee share. Items without a meaning still count.
- **Card fees**: Square adds its fee a little after each payment, so a fresh one shows "pending" until a later pull.

**Owed to us** lists everyone who still owes, the nights behind it and how old each debt is (0–7, 8–30, 31–90, 90+
days). Record a bank transfer or cash (it pays their oldest nights first, anything over is credit) or write it off
with a reason. Credit is kept for the player's next tournament unless you mark it kept as income or refunded.

## 2. Pay out

- **Pool** = fixed pool + bracket-fee share + venue-fee share, per the game's rule. Ties share the combined amount of
  the places they occupy. Money is integer cents; splits are exact to the cent.
- **The venue's cut** is a separate payable per tournament: a flat amount, a % of venue fees collected (fee x
  attendees) and a % of bracket fees collected (each event's bracket fee x entrants), added together. A tournament
  can be held at a different venue, use a custom cut, or have no venue.
- **To pay** groups everything owed by recipient, so each player and venue is one bank transfer, next to the estimated
  bank balance and what's left after paying everyone. Send it from the bank, then mark it **Paid** with the day it
  left: the bank then expects it gone on that day. The tournament's Payouts step pays one line at a time. The **Paid**
  tab lists what's been paid, filtered by player, venue, series or game, with undo until reconciled.
- Re-syncing never changes an amount already marked paid: a changed result shows a `≠` marker instead.

## 3. Reconcile

The **Bank** page is three steps:

1. Enter the balance from the bank app at the end of a day.
2. Compare it with what the app expects: the last reconciled balance, plus money in (Square's payouts, deposits added
   by hand, players' bank transfers), minus money out (payouts to players and venues, on the day they were sent).
3. Explain any difference, then lock. Each explanation has a category (cash deposited, bank fee, interest, your own
   money in or out, a refund to a player, other income or expense); "a player paid by bank transfer" records it as
   their payment against what they owe. Once it matches, lock the day. **Lock anyway** files what's left as
   *unexplained*, with a reason; it stays visible, and can be explained properly later.

The first reconciliation sets the opening balance. Deposits are Square's payouts (**Import payouts from Square**, or
the automatic pull; uses the `PAYOUTS_READ` permission), each with Square's breakdown of the payments, refunds and
fees it covered, plus any you add by hand. Failed payouts don't count.

**Locking**: a reconciliation locks everything dated on or before its day: deposits, transfers, explanations, payouts
to players and venues, and every tournament up to then with nothing left to collect (no payments to link, no money
missing, all events final). A locked tournament's settings, payouts and payment links can't change and it isn't
re-imported, though players can still pay what they owe for it. Undo the latest reconciliation, one at a time, to
unlock.

**History** (under Records) is the audit log of every money action, with when it happened and the address it came
from; tournament and player pages show their own.

## Records

- **Series**: parry.gg has no series object, so a series is either a *custom slug* (the quick link shared by every
  tournament in the series, e.g. `fridaybrawl`, matched exactly) or, as a fallback, a *match pattern* (e.g. `Friday
  Night Brawl`) matched, ignoring case and punctuation, against each tournament's name and URL slug. Individual
  tournaments can also be added by link or excluded. Each series has a three-letter code (e.g. `FNB`) used in links
  (`/series/FNB`) and tables. Its page has tabs: Tournaments (with their stage), Terms & rules, Money and Settings.
- **Rules** (per game, with optional single-event override): bracket fee, number of places paid, fixed pool, % of
  bracket fees into the pool, % of venue fees into the pool, and a split % per place (e.g. `50,30,20`).
- **Terms over time**: a series' venue, venue fee and game rules are dated. When something changes (e.g. a new
  venue), start new terms from that date: the rules in effect then are copied, and each tournament uses the terms in
  effect on its date, so earlier ones keep their old venue, fee and rules.
- **Players**: what we owe them and what they owe us, night by night, their credit, placings and prizes, Square
  payments, and history. Someone imported under two parry.gg accounts can be merged from either player's page (the
  Players page suggests tags that differ only in case or punctuation), and unmerged again.
- **Venues**: each venue's series, tournaments held there, and what it's owed and been paid.
- **Events**: every event, filtered by series, game or unfinalised placements; one game's events also show its payout
  rules, winners and players.

## Setup

The **Setup** page shows the parry.gg and Square connections, each series' syncing, the Square item meanings, and the
weekly reminder.

- **Weekly reminder** (optional): set `SMTP_HOST` (plus `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` or
  `SMTP_PASSWORD_FILE`, `SMTP_FROM`) and `REMINDER_TO` (comma-separated) to get the run sheet by email once a week, at
  `REMINDER_HOUR` (default 9) on `REMINDER_DAY` (default `mon`) in your `TZ`: tournaments to collect, who owes us,
  what to pay against the bank, and when it was last reconciled. Set `POT_URL` to have it link to the pages. Port 587
  needs STARTTLS (465 is TLS from the start). `python -m app.mail preview` prints this week's email; `python -m
  app.mail test` sends it now. A reminder missed by more than a day (the app was down) is skipped, not sent late.
- **UI**: server-rendered, with its own look (self-hosted Barlow fonts, a dark scoreboard style, a print stylesheet
  on white). Every status is a word, not just a colour. A few lines of vanilla JS (`app/static/table.js`) add
  click-to-sort and filter boxes; every page works without it. `/setup/styleguide` shows every component.
- **Upgrading** from before the ledger moves earlier decisions in once: entrants sorted out by hand become
  settlements, and covered shortfalls become write-offs ("Organiser covered"). `python -m app.ledger report` lists
  what was moved, and `python -m app.ledger check` checks the books balance.

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
reaches production. It also checks the books balance (`python -m app.ledger check`) and lists what the upgrade moved
into the ledger, and why (`python -m app.ledger report`). `snapshot` uses SQLite's online backup
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
