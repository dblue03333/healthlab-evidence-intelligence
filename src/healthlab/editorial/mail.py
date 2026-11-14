"""Gmail delivery with a durable ledger: each prepared message is sent at most once."""

import json
import re
import smtplib
import ssl

from healthlab.common import utc_now, write_text_atomic


def mailbox(value):
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value):
        raise ValueError("Expected a plain email address without display name or whitespace")
    return value


def smtp_gmail():
    return smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30, context=ssl.create_default_context())


def deliver(ledger_path, prepared, sender, app_password, smtp_factory=None):
    """Send prepared (message, key) pairs once each; the ledger survives every outcome.

    Accepted, pending and unknown keys are never resent; a pending entry is written before
    connecting, so an interrupted send stays visible for reconciliation.
    """
    smtp_factory = smtp_factory or smtp_gmail
    ledger = json.loads(ledger_path.read_bytes()) if ledger_path.exists() else {}
    outcomes = []
    for message, key in prepared:
        previous = ledger.get(key)
        if previous and previous["status"] in {"accepted", "pending", "unknown"}:
            outcomes.append({"key": key, "status": previous["status"], "skipped": True})
            continue
        entry = {
            "key": key,
            "recipient": message["To"],
            "status": "pending",
            "started_at": utc_now(),
        }
        ledger[key] = entry
        write_text_atomic(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=2))
        submitting = False
        connection = None
        try:
            connection = smtp_factory()
            connection.login(sender, app_password.replace(" ", ""))
            submitting = True
            refused = connection.send_message(message, from_addr=sender, to_addrs=[message["To"]])
            entry["status"] = "failed" if refused else "accepted"
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError):
            entry["status"] = "failed"
        except (smtplib.SMTPException, OSError):
            entry["status"] = "unknown" if submitting else "failed"
        finally:
            if connection:
                try:
                    connection.close()
                except (smtplib.SMTPException, OSError):
                    pass
        entry["finished_at"] = utc_now()
        # If this write fails, pending is durable and further sends are blocked.
        write_text_atomic(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=2))
        outcomes.append({"key": key, "status": entry["status"]})
        if entry["status"] != "accepted":
            break  # Stop after auth/transport trouble, retaining unsent recipients.
    return ledger, outcomes
