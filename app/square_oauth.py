"""One-time setup of read-only Square access (see square_auth.py and the README).

    python -m app.square_oauth url                 print the link to authorise read-only access
    python -m app.square_oauth exchange CODE STATE  swap the code from the redirect page for a refresh token
    python -m app.square_oauth status              check what the configured token is allowed to do

Run it on the server (e.g. `docker compose exec pot python -m app.square_oauth url`) so the client secret never
leaves it. Needs SQUARE_ENV, SQUARE_CLIENT_ID and SQUARE_CLIENT_SECRET; `exchange` prints the SQUARE_REFRESH_TOKEN
to add to .env. Nothing is written to the database.
"""
import os
import secrets
import sys
import tempfile
from pathlib import Path

from . import config, square_auth
from .square_client import environment

STATE_FILE = Path(tempfile.gettempdir()) / "the-pot-square-oauth-state"


def cmd_url():
    env = environment()
    creds = square_auth.credentials(need_refresh_token=False)
    state = secrets.token_urlsafe(24)
    fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(state)
    print(f"1. In the Square Developer Console, set this app's {env} OAuth redirect URL to:\n   {config.square_redirect_url()}")
    print(f"2. Sign in as the account owner and open this link (it asks for {', '.join(square_auth.READ_SCOPES)} only;")
    print("   check the permissions Square lists before approving):")
    print(f"   {square_auth.authorize_url(env, creds['client_id'], state)}")
    print("3. Within 5 minutes, run:  python -m app.square_oauth exchange <code> <state>  (the page shows both)")


def cmd_exchange(code: str, state: str):
    env = environment()
    expected = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""
    if not expected or not secrets.compare_digest(expected, state.strip()):
        sys.exit("The state doesn't match the last `url` run: start again with `python -m app.square_oauth url`")
    STATE_FILE.unlink()
    with square_auth.http_client(env) as http:
        body = square_auth.exchange_code(http, env, code.strip(), config.square_redirect_url())
    print(f"Read-only access granted ({', '.join(body['scopes'])}) for Square merchant {body.get('merchant_id', '?')}.")
    print("Add this line to the server's .env, then recreate the container (docker compose up -d):\n")
    print(f"SQUARE_REFRESH_TOKEN={body['refresh_token']}\n")
    print("It's shown only once and only works together with the client secret. If the old SQUARE_API_KEY is still")
    print("in .env, remove it and replace it in the Developer Console so it stops working.")


def cmd_status():
    env = environment()
    with square_auth.http_client(env) as http:
        token = square_auth.access_token(http, env)
    print(f"Square ({env}): read-only, scopes {', '.join(token['scopes'])}; access token renews automatically "
          f"(current one expires {token['expires']:%Y-%m-%d})")


def main(argv):
    try:
        if argv[:1] == ["url"]:
            cmd_url()
        elif argv[:1] == ["exchange"] and len(argv) == 3:
            cmd_exchange(argv[1], argv[2])
        elif argv[:1] == ["status"]:
            cmd_status()
        else:
            sys.exit(__doc__)
    except square_auth.SquareConfigError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main(sys.argv[1:])
