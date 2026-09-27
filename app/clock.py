"""One place for "now" so deadlines are consistent. Timestamps are naive local-time strings."""

import os
from datetime import datetime, timedelta

FMT = "%Y-%m-%d %H:%M:%S"


def now():
    return datetime.now().replace(microsecond=0)


def strftime(dt, fmt):
    """dt.strftime(fmt), but '%-d'-style no-pad flags (glibc-only) become '%#d' on Windows."""
    if os.name == "nt":
        fmt = fmt.replace("%-", "%#")
    return dt.strftime(fmt)


def ts(dt):
    return dt.strftime(FMT)


def parse(value):
    return datetime.strptime(value, FMT) if value else None


def later(**kwargs):
    return ts(now() + timedelta(**kwargs))


def is_past(value):
    return value is not None and parse(value) <= now()
