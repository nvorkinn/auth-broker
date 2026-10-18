"""The notice board's postcode: a UK or a Norwegian one, in canonical form, and which country it's in."""

import re

# UK postcode shape: outward code (e.g. SW1A, N1, EC2V) then inward code (digit + two letters).
UK_POSTCODE_RE = re.compile(r"^([A-Z]{1,2}[0-9][A-Z0-9]?)([0-9][A-Z]{2})$")
# A Norwegian postcode is four digits ("0150"); a UK one always has letters, so the two can't be confused.
NORWEGIAN_POSTCODE_RE = re.compile(r"^[0-9]{4}$")


def normalize_postcode(value: str) -> str | None:
    """Return the postcode in canonical form ("SW1A 1AA", "0150").

    Return "" if it is blank, or None if it is neither a UK nor a Norwegian postcode.
    """
    compact = re.sub(r"\s+", "", value).upper()
    if not compact:
        return ""
    if NORWEGIAN_POSTCODE_RE.match(compact):
        return compact
    match = UK_POSTCODE_RE.match(compact)
    return f"{match[1]} {match[2]}" if match else None


def postcode_country(postcode: str) -> str | None:
    """Return the country of a normalized postcode: "NO", "GB", or None for none."""
    if not postcode:
        return None
    return "NO" if NORWEGIAN_POSTCODE_RE.match(postcode) else "GB"
