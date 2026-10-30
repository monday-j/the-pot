"""A weekly email of the run sheet (#26, #47): what to collect, who owes us, what to pay, and the bank.

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

from . import bank, config, db, runsheet

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
    """(subject, body) of the reminder, following the run sheet (#47): tournaments to collect, what players owe us,
    what we owe against the bank, and the bank itself."""
    today = today or bank.today()
    url = config.site_url()
    link = lambda path: f" {url}{path}" if url else ""
    plural = lambda n, word: f"{n} {word}{'s' if n != 1 else ''}"
    lanes = runsheet.stages(conn)
    owed = runsheet.owed_to_us(conn)
    topay = runsheet.to_pay(conn)
    b = runsheet.bank_summary(conn)
    lines = []

    collect = lanes["collect"]
    lines.append(f"Collect: {plural(len(collect), 'tournament')} with takings to sort out.{link('/')}")
    for t in collect[:10]:
        x = t["takings"]
        what = ", ".join(w for w in (f"{plural(x['unlinked'], 'payment')} to link" if x["unlinked"] else "",
                                     f"{money(x['gap'])} missing" if x["gap"] else "") if w)
        lines.append(f"  - {t['name']}: {what}{link('/tournaments/' + t['id'] + '#takings')}")
    if len(collect) > 10:
        lines.append(f"  ... and {len(collect) - 10} more{link('/')}")
    waiting = [t for t in lanes["waiting"] if t["not_final"]]
    if waiting:
        lines.append(f"Waiting on parry.gg: {plural(len(waiting), 'tournament')} with placements to finalise.")

    lines.append("")
    if owed["players"]:
        lines.append(f"Owed to us: {money(owed['cents'])} by {plural(len(owed['players']), 'player')}, the oldest "
                     f"{plural(owed['oldest_days'], 'day')} ago.{link('/owed')}")
        for p in owed["players"][:5]:
            lines.append(f"  - {p['name']}: {money(p['cents'])} for {plural(len(p['nights']), 'night')}")
        if len(owed["players"]) > 5:
            lines.append(f"  ... and {len(owed['players']) - 5} more")
    else:
        lines.append("Owed to us: nothing.")

    lines.append("")
    if topay["recipients"]:
        against = ""
        if b["estimate"] is not None:
            left = b["estimate"] - topay["cents"]
            against = (f" The bank should have {money(b['estimate'])}, "
                       + (f"{money(left)} left after paying." if left >= 0 else f"{money(-left)} short."))
        lines.append(f"To pay: {money(topay['cents'])} in {plural(len(topay['recipients']), 'transfer')}."
                     f"{against}{link('/to-pay')}")
        for r in topay["recipients"][:5]:
            lines.append(f"  - {r['name']}{' (venue)' if r['venue'] else ''}: {money(r['owed'])}")
        if len(topay["recipients"]) > 5:
            lines.append(f"  ... and {len(topay['recipients']) - 5} more")
    else:
        lines.append("To pay: nothing.")

    lines.append("")
    if b["last"]:
        days = (date.fromisoformat(today) - date.fromisoformat(b["last"]["reconciled_on"])).days
        lines.append(f"Bank: last reconciled {b['last']['reconciled_on']} ({plural(days, 'day')} ago).{link('/bank')}")
    else:
        lines.append(f"Bank: never reconciled. The first reconciliation sets the opening balance.{link('/bank')}")
    if b["in_transit"]:
        lines.append(f"  {money(b['in_transit'])} from Square still on its way.")
    if b["unexplained"]:
        lines.append(f"  {money(b['unexplained'])} filed as unexplained: explain it on the Bank page.")

    todo = [plural(len(collect), "tournament") + " to collect" if collect else "",
            plural(len(topay["recipients"]), "payment") + " to make" if topay["recipients"] else "",
            plural(len(owed["players"]), "player") + " owing" if owed["players"] else ""]
    todo = [t for t in todo if t]
    subject = "The Pot: " + (", ".join(todo) if todo else "nothing waiting")
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
