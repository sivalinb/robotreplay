import re


def parse_console(text, duration, offset=0.0, uncertainty=0.3):
    """Student prints: RR|<elapsed milliseconds>|<message>. No claimed native VEX schema."""
    if len(text.encode("utf-8")) > 64 * 1024:
        raise ValueError("log_too_large")
    rows = []
    for line in text.splitlines():
        match = re.fullmatch(r"RR\|(\d{1,9})\|([^\x00-\x08]{1,200})", line.strip())
        if not match:
            continue
        t = int(match[1]) / 1000 + offset
        if not 0 <= t <= duration:
            raise ValueError("log_time_out_of_range")
        rows.append({"t": t, "text": match[2], "uncertainty": uncertainty})
    if not rows:
        raise ValueError("no_supported_log_lines")
    return rows
