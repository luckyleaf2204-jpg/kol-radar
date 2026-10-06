"""Settings of the PAPER KOL copy bot. Override any field in config.json (same names)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass
class Config:
    starting_sol: float = 5.0            # paper balance
    position_sol: float = 0.1            # SOL spent per copy (fees included)
    max_open: int = 10                   # open positions at most
    min_kol_buy_sol: float = 0.05        # a KOL buy smaller than this is not copied
    delay_s: float = 3.0                 # our transaction lands this long after the KOL's
    fill_grace_s: float = 4.0            # no trade on the token by due + grace -> fill at the last known curve state
    max_hold_s: float = 24 * 3600        # exit at the latest after this
    extra_slippage_pct: float = 1.0      # per side, on top of the curve's own price impact (contention, MEV)
    priority_fee_sol: float = 0.005      # per transaction (buy and sell)
    network_fee_sol: float = 0.00011     # per transaction
    default_fee_bps: int = 125           # pump.fun fee if the token's events do not say (0.95 % + 0.30 % creator)
    stop_loss_pct: float | None = None   # off: exits follow the KOL (pre-registered rule of direction C)
    take_profit_pct: float | None = None
    daily_loss_limit_sol: float = 1.0    # no new entry for the rest of the UTC day after this realized loss
    gap_flag_s: float = 300              # a hold that overlaps a stream gap longer than this is flagged

    @classmethod
    def load(cls, path: Path | None) -> "Config":
        c = cls()
        if path and Path(path).exists():
            names = {f.name for f in fields(cls)}
            for k, v in json.loads(Path(path).read_text(encoding="utf-8")).items():
                if k not in names:
                    raise ValueError(f"unknown setting {k!r} in {path}")
                setattr(c, k, v)
        return c

    def as_dict(self) -> dict:
        return asdict(self)
