"""Run one follow-up reminder sweep (due / overdue notifications), for an external cron.

Usage:
    python -m app.scripts.send_follow_up_reminders

Safe to run as often as you like, alongside the in-process sweep and on several hosts:
each reminder is sent at most once (see app/services/reminder_service.py).
"""

from app.services import reminder_service


def main() -> None:
    counts = reminder_service.run_once()
    print(f"Follow-up reminders: {counts['due']} due, {counts['overdue']} overdue")


if __name__ == "__main__":
    main()
