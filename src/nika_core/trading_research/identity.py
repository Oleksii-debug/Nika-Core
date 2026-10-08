from __future__ import annotations

import hashlib
import json

from .contracts import Instrument

type InstrumentIdentity = tuple[str, str, str, str]


def instrument_identity(instrument: Instrument) -> InstrumentIdentity:
    """Return the complete deterministic identity used by paper-trading state."""

    return (
        instrument.venue.venue_id,
        instrument.venue.timezone,
        instrument.instrument_id,
        instrument.currency,
    )


def instrument_identity_sha256(instrument: Instrument) -> str:
    payload = json.dumps(
        instrument_identity(instrument),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
