"""A weekly email reminding you to process payments (#26), with what's waiting.

    python -m app.mail preview   print this week's reminder
    python -m app.mail test      send it now (whether or not it's due)

Settings: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD (or SMTP_PASSWORD_FILE), SMTP_FROM, REMINDER_TO,
REMINDER_DAY, REMINDER_HOUR and POT_URL (see config.py and the README). Sent once a week, from the configured hour
on: a reminder missed by more than a day (the app was down) is skipped rather than sent late.
"""
import logging
import smtplib
import ssl
import sys
import threading
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from . import bank, config, db, square, sync

CHECK_SECONDS = 600  # how often the loop checks whether the reminder is due
FIRST_DELAY = 90  # seconds after startup before the first check
log = logging.getLogger("the_pot.mail")


def money(cents) -> str:
    cents = int(cents or 0)
    return f"{'-' if cents < 0 else ''}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def slot(now: datetime, weekday: int, hour: int) -> datetime:
    """The latest scheduled time at or before `now`."""
    at = now.replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=(now.weekday() - weekday) % 7)
    return at if at <= now else at - timedelta(days=7)


def due(now: datetime, last_sent: datetime | None, weekday: int, hour: int) -> bool:
    """Whether this week's reminder should go now: not sent since its time, and that time is under a day ago."""
    at = slot(now, weekday, hour)
    return now - at < timedelta(days=1) and (last_sent is None or last_sent < at)


def summary(conn, today: str | None = None) -> tuple[str, str]:
    """(subject, body) of the reminder: what's waiting to be processed."""
    today = today or bank.today()
    url = config.site_url()
    link = lambda path: f" {url}{path}" if url else ""
    lines = []
    last = bank.latest(conn)
    moved = bank.movements(conn)
    if last:
        days = (date.fromisoformat(today) - date.fromisoformat(last["reconciled_on"])).days
        lines.append(f"Bank: last reconciled {last['reconciled_on']} ({days} day{'s' if days != 1 else ''} ago).")
    else:
        lines.append("Bank: never reconciled. The first reconciliation sets the opening balance.")
    lines.append(f"  Not reconciled yet: {money(moved['in'])} in ({len(moved['deposits'])} deposits), "
                 f"{money(moved['out'])} out ({len(moved['players']) + len(moved['venues'])} payments).{link('/bank')}")
    issues = square.payment_issues(conn)
    missing = [i for i in issues if i["kind"] == "missing"]
    to_link = [i for i in issues if i["kind"] == "link"]
    lines.append("")
    lines.append(f"Square payments: {len(missing)} tournament{'s' if len(missing) != 1 else ''} with missing payments, "
                 f"{len(to_link)} with payments to link.")
    for i in issues[:10]:
        what = (f"{money(i['missing'])} missing" if i["kind"] == "missing"
                else f"{i['unlinked']} payment{'s' if i['unlinked'] != 1 else ''} to link")
        lines.append(f"  - {i['tournament_name']}: {what}{link('/tournaments/' + i['tournament_id'] + '#square')}")
    if len(issues) > 10:
        lines.append(f"  ... and {len(issues) - 10} more{link('/')}")
    events = sync.event_issues(conn)
    if events:
        lines.append(f"Events with placements to finalise on parry.gg: {len(events)}.")
    owed = conn.execute("SELECT COALESCE(SUM(amount_cents),0), COUNT(*) FROM payout WHERE status='owed'").fetchone()
    venue = conn.execute("SELECT COALESCE(SUM(amount_cents),0), COUNT(*) FROM venue_payment WHERE status='owed'").fetchone()
    lines.append("")
    lines.append(f"Owed: {money(owed[0])} to players ({owed[1]} payouts), {money(venue[0])} to venues.{link('/')}")
    need = f" ({len(issues)} tournament{'s need' if len(issues) != 1 else ' needs'} attention)" if issues else ""
    subject = "The Pot: time to process payments" + need
    return subject, "\n".join(lines) + "\n"


def send(subject: str, body: str):
    """Send to REMINDER_TO over SMTP: TLS from the start on port 465, otherwise STARTTLS (required unless the server is
    localhost, e.g. a relay on the same machine)."""
    s = config.smtp()
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, s["from_addr"], ", ".join(config.reminder_to())
    msg.set_content(body)
    context = ssl.create_default_context()
    if s["port"] == 465:
        server = smtplib.SMTP_SSL(s["host"], s["port"], context=context, timeout=30)
    else:
        server = smtplib.SMTP(s["host"], s["port"], timeout=30)
    with server:
        if s["port"] != 465:
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls(context=context)
                server.ehlo()
            elif s["host"] not in ("localhost", "127.0.0.1"):
                raise RuntimeError(f"{s['host']} doesn't offer STARTTLS: refusing to send in the clear")
        if s["user"]:
            server.login(s["user"], config.smtp_password() or "")
        server.send_message(msg)


def _last_sent(conn) -> datetime | None:
    row = conn.execute("SELECT value FROM app_state WHERE key='reminder_sent'").fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def remind_once(now: datetime | None = None) -> bool:
    """Send this week's reminder if it's due. True when one was sent."""
    if not config.reminders_on():
        return False
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    with db.session() as conn:
        if not due(now, _last_sent(conn), *config.reminder_when()):
            return False
        subject, body = summary(conn, now.date().isoformat())
        send(subject, body)
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('reminder_sent', ?)", (now.isoformat(),))
    log.info("Sent the weekly reminder to %d recipient(s)", len(config.reminder_to()))
    return True


def reminder_loop(stop: threading.Event):
    """Check every CHECK_SECONDS (first shortly after startup) until `stop` is set."""
    wait = FIRST_DELAY
    while not stop.wait(wait):
        try:
            remind_once()
        except Exception:
            log.exception("Weekly reminder failed")
        wait = CHECK_SECONDS


def main(argv):
    if argv[:1] not in (["preview"], ["test"]):
        sys.exit(__doc__)
    db.init()
    with db.session() as conn:
        subject, body = summary(conn)
    if argv[0] == "preview":
        print(f"Subject: {subject}\n\n{body}")
        return
    if not config.reminders_on():
        sys.exit("Set SMTP_HOST and REMINDER_TO first (see the README)")
    send(subject, body)
    print(f"Sent to {', '.join(config.reminder_to())}")


if __name__ == "__main__":
    main(sys.argv[1:])
