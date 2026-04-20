from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError


def parse_bar_time(raw: str, data_timezone: str, market_timezone: str) -> datetime | None:
    for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d  %H:%M:%S", "%Y%m%d-%H:%M:%S", "%Y%m%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt == "%Y%m%d":
                return None
            break
        except ValueError:
            continue
    else:
        return None

    try:
        source_tz = ZoneInfo(data_timezone)
        target_tz = ZoneInfo(market_timezone)
    except ZoneInfoNotFoundError:
        return parsed

    return parsed.replace(tzinfo=source_tz).astimezone(target_tz)
