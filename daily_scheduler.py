"""
Long-running scheduler: once a day, at a fresh random time between 12:00
and 16:00, runs check_for_new_articles().

Why this instead of OS crontab/launchd: crontab writes are blocked here by
macOS's privacy protections for whatever process runs these tools - even
calling `crontab -` straight from Python hits the same
"crontab: tmp/tmp.NNNNN: Operation not permitted" as the shell did. Rather
than fight that permission wall, this is a plain Python loop: start it once
and it re-randomizes and sleeps until the next day's check on its own, no
OS scheduler involved.

Start it once, detached, so it survives the terminal closing:

    nohup python3 daily_scheduler.py >> scheduler.log 2>&1 &
    disown

It does NOT survive a reboot on its own (nothing restarts it for you) - if
you need that, the next step up is a launchd LaunchAgent (a plain file in
~/Library/LaunchAgents, not gated by the crontab restriction above); ask if
you want that wired up too.
"""

from __future__ import annotations

import datetime
import random
import time

from check_new_articles import check_for_new_articles

WINDOW_START_HOUR = 12
WINDOW_END_HOUR = 16


def _random_time_in_window(window_start: datetime.datetime, window_end: datetime.datetime) -> datetime.datetime:
    span = max(0, int((window_end - window_start).total_seconds()))
    return window_start + datetime.timedelta(seconds=random.randint(0, span))


def _initial_target(now: datetime.datetime) -> datetime.datetime:
    """First target after starting the script: a random time in the rest of
    today's window if we're before or inside it, otherwise tomorrow's."""
    window_start = now.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    window_end = now.replace(hour=WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
    if now >= window_end:
        window_start += datetime.timedelta(days=1)
        window_end += datetime.timedelta(days=1)
    elif now > window_start:
        window_start = now  # already inside today's window - don't pick a time in the past
    return _random_time_in_window(window_start, window_end)


def _next_day_target(after: datetime.datetime) -> datetime.datetime:
    """Every run after the first always jumps a full day ahead, so a run
    that happens to land late in today's window can't trigger a second run
    later the same day."""
    tomorrow = after + datetime.timedelta(days=1)
    window_start = tomorrow.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    window_end = tomorrow.replace(hour=WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
    return _random_time_in_window(window_start, window_end)


def _log(msg: str) -> None:
    print(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def run_forever() -> None:
    target = _initial_target(datetime.datetime.now())
    _log(f"next check scheduled for {target.isoformat(timespec='seconds')}")

    while True:
        sleep_for = (target - datetime.datetime.now()).total_seconds()
        if sleep_for > 0:
            time.sleep(sleep_for)

        _log("running check...")
        try:
            new_records = check_for_new_articles()
            _log(f"done - {len(new_records)} new article(s)")
        except Exception as exc:
            _log(f"check failed: {exc}")

        target = _next_day_target(datetime.datetime.now())
        _log(f"next check scheduled for {target.isoformat(timespec='seconds')}")


if __name__ == "__main__":
    run_forever()
