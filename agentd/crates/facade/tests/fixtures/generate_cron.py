"""Record what the Python server's schedules computed, before they moved to agentd.

    uv run --with croniter python agentd/crates/facade/tests/fixtures/generate_cron.py

``cron.json``: for cron lines in zones at instants (DST changes among them), the next slot as
the server's ``next_slot`` computed it with croniter; and which lines croniter called valid.
A frozen record: the server no longer depends on croniter.
"""

import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

LINES = [
    "* * * * *", "*/5 * * * *", "0 * * * *", "0 2 * * *", "30 2 * * *", "0 0 * * 0", "0 0 * * 7", "0 9 * * 1-5",
    "15,45 8-18 * * *", "0 0 1 * *", "0 0 31 * *", "0 0 29 2 *", "0 12 1 1,7 *", "0 0 1 * 1", "0 0 13 * 5",
    "*/15 9-17 * * mon-fri", "0 0 * jan,jul *", "0 6 * * sun", "5 4 * * sat,sun", "0 0 L * *", "0 0 * * 1#2",
    "0 */6 * * *", "59 23 31 12 *", "0 3 * * *", "30 1 * * *", "0 0 */2 * *", "10-20/5 * * * *",
]
ZONES = ["UTC", "Europe/Berlin", "America/New_York", "Asia/Kolkata", "Australia/Sydney"]
INSTANTS = [
    "2026-01-15T10:07:30+00:00", "2026-02-28T23:59:59+00:00", "2024-02-28T12:00:00+00:00",
    # Europe/Berlin springs forward 2026-03-29 02:00 -> 03:00, falls back 2026-10-25 03:00 -> 02:00.
    "2026-03-29T00:30:00+00:00", "2026-03-29T00:59:59+00:00", "2026-10-24T23:30:00+00:00", "2026-10-25T00:30:00+00:00",
    # America/New_York springs forward 2026-03-08, falls back 2026-11-01.
    "2026-03-08T06:30:00+00:00", "2026-11-01T05:30:00+00:00",
    "2026-12-31T23:59:00+00:00", "2026-06-30T12:00:00.500000+00:00",
]
VALIDITY = LINES + [
    "whenever", "", "* * * *", "60 * * * *", "* 24 * * *", "* * 32 * *", "* * * 13 *", "* * * * 8", "*/0 * * * *",
    "@hourly", "@daily", "0 0 * * * *", "* * * * * *", "0 0 0 * * *", "1-5 * * * *", "5-1 * * * *", "a b c d e",
    "0 0 1 * * 2026", "? * * * *", "0 0 ? * 1", "0 0 15W * *", "0 0 * * 5L", "0 0 L-1 * *", "  0   2  * * *  ",
]


def next_slot(line: str, zone: str, after: datetime.datetime) -> str:
    local = after.astimezone(ZoneInfo(zone))
    return croniter(line, local).get_next(datetime.datetime).astimezone(datetime.timezone.utc).isoformat()


cases = []
for line in LINES:
    for zone in ZONES:
        for instant in INSTANTS:
            after = datetime.datetime.fromisoformat(instant)
            try:
                cases.append({"cron": line, "timezone": zone, "after": instant, "next": next_slot(line, zone, after)})
            except Exception as error:  # noqa: BLE001
                cases.append({"cron": line, "timezone": zone, "after": instant, "error": type(error).__name__})
valid = {line: bool(croniter.is_valid(line)) for line in VALIDITY}
out = Path(__file__).with_name("cron.json")
out.write_text(json.dumps({"next": cases, "valid": valid}, indent=1) + "\n")
print(f"wrote {out}: {len(cases)} next-slot cases, {len(valid)} validity cases")
