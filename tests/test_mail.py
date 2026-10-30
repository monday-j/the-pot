from datetime import datetime

import pytest

from app import config, db, mail, square
from tests.test_square import FakeSquare, fnb, pay  # noqa: F401 (fixtures)
from tests.test_sync import conn  # noqa: F401

MON, WED = 0, 2


def at(text):
    return datetime.fromisoformat(text)


def test_due_once_a_week_from_the_hour_and_not_late():
    # 2026-11-02 is a Monday
    assert not mail.due(at("2026-11-02 08:59"), None, MON, 9)
    assert mail.due(at("2026-11-02 09:00"), None, MON, 9)
    assert mail.due(at("2026-11-02 23:00"), at("2026-10-26 09:05"), MON, 9)  # last week's doesn't count
    assert not mail.due(at("2026-11-02 10:00"), at("2026-11-02 09:05"), MON, 9)  # already sent
    assert not mail.due(at("2026-11-03 09:30"), None, MON, 9)  # missed by over a day: skipped
    assert mail.due(at("2026-11-04 18:00"), at("2026-11-02 09:00"), WED, 18)


def test_reminder_settings(monkeypatch):
    for name in ("SMTP_HOST", "REMINDER_TO", "REMINDER_DAY", "REMINDER_HOUR", "SMTP_PORT", "SMTP_USER", "SMTP_FROM"):
        monkeypatch.delenv(name, raising=False)
    assert not config.reminders_on() and config.reminder_when() == (0, 9)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("REMINDER_TO", " a@example.com, b@example.com ,")
    monkeypatch.setenv("REMINDER_DAY", "Friday")
    monkeypatch.setenv("REMINDER_HOUR", "25")
    monkeypatch.setenv("SMTP_USER", "pot@example.com")
    assert config.reminders_on() and config.reminder_to() == ["a@example.com", "b@example.com"]
    assert config.reminder_when() == (4, 9)  # an invalid hour falls back
    assert config.smtp() == {"host": "smtp.example.com", "port": 587, "user": "pot@example.com",
                             "from_addr": "pot@example.com"}


def test_summary_lists_what_is_waiting(fnb, monkeypatch):  # noqa: F811
    monkeypatch.setenv("POT_URL", "http://pot.lan:8000/")
    subject, body = mail.summary(fnb, "2026-01-20")
    assert subject == "The Pot: 5 payments to make" and "never reconciled" in body
    assert "Collect: 0 tournaments" in body and "Owed to us: nothing." in body
    square.pull_tournament(fnb, FakeSquare([pay("P1", 1000, "fp-a")]), "t1")
    fnb.execute("INSERT INTO bank_deposit (deposited_on, amount_cents) VALUES ('2026-01-02', 2500)")
    fnb.execute("""INSERT INTO reconciliation (reconciled_on, balance_cents, expected_cents, created_at)
                   VALUES ('2026-01-01', 0, 0, '')""")
    subject, body = mail.summary(fnb, "2026-01-20")
    assert subject == "The Pot: 1 tournament to collect, 5 payments to make"
    assert "  - Friday Night Brawl #90: 1 payment to link" in body
    assert "http://pot.lan:8000/tournaments/t1#takings" in body
    assert "last reconciled 2026-01-01 (19 days ago). http://pot.lan:8000/bank" in body
    assert "To pay: " in body and "The bank should have $25.00" in body and "http://pot.lan:8000/to-pay" in body

    square.link_payment(fnb, "P1", "u1")  # Alice paid; Bob, Cat and Dan owe $10 each
    subject, body = mail.summary(fnb, "2026-01-20")
    assert "3 players owing" in subject and "Owed to us: $30.00 by 3 players" in body
    assert "  - Bob: $10.00 for 1 night" in body


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.tls, self.login_as = host, port, False, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def has_extn(self, name):
        return self.host != "plain.example.com"

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        self.login_as = (user, password)

    def send_message(self, msg):
        FakeSMTP.sent.append((self, msg))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "pot@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("REMINDER_TO", "me@example.com")
    for name in ("SMTP_PORT", "SMTP_FROM", "SMTP_PASSWORD_FILE", "REMINDER_DAY", "REMINDER_HOUR"):
        monkeypatch.delenv(name, raising=False)
    return FakeSMTP


def test_send_uses_starttls_and_logs_in(smtp, monkeypatch):
    mail.send("Hi", "body")
    [(server, msg)] = smtp.sent
    assert server.tls and server.login_as == ("pot@example.com", "secret")
    assert (msg["To"], msg["From"], msg["Subject"]) == ("me@example.com", "pot@example.com", "Hi")
    monkeypatch.setenv("SMTP_HOST", "plain.example.com")
    with pytest.raises(RuntimeError, match="STARTTLS"):
        mail.send("Hi", "body")


def test_remind_once_sends_once_a_week(smtp, conn):  # noqa: F811
    assert mail.remind_once(at("2026-11-02 09:30+11:00"))
    assert not mail.remind_once(at("2026-11-02 10:30+11:00"))
    assert len(smtp.sent) == 1
    assert mail.remind_once(at("2026-11-09 09:00+11:00"))
    with db.session() as c:
        assert c.execute("SELECT value FROM app_state WHERE key='reminder_sent'").fetchone()[0].startswith("2026-11-09")
