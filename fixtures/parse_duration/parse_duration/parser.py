from .units import UNITS


def parse_duration(s: str) -> int:
    """Parse a duration string into seconds.

    Supported units: ``s`` (seconds), ``m`` (minutes), ``h`` (hours).
    """
    return int(s[:-1]) * UNITS[s[-1]]
