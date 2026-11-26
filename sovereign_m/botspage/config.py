"""Who the bots are and which rig timers belong to whom (from the fleet readiness report)."""

from __future__ import annotations

M_ID = "m-systems"

# id, display name, board `owner` values, systemd --user timer names (without .timer)
BOTS = [
    ("m-systems", "M Systems", ("M Systems",),
     ("m-brain-resurface", "m-brain-weekly", "m-brain-cleanup", "m-memory-nightly", "m-brain-inbox")),
    ("ops", "Ops", ("Ops",),
     ("m-cos-morning-plan", "m-cos-daylog", "m-cos-rig-health", "m-sovereign-loop")),
    ("checker", "Checker", ("Checker",), ()),
    ("studio", "Studio", ("Studio",), ()),
    ("trading-desk", "Trading Desk", ("Trading Desk",),
     ("m-trader-scout", "m-trader-quant", "m-trader-builder", "m-trader-brief", "m-trader-weekly",
      "m-trader-c-watchdog", "m-trader-d-watchdog")),
    ("institute", "Institute Bot", ("Institute Bot",),
     ("m-cos-curriculum-check", "m-institute-finetune", "m-institute-notes", "m-institute-wiki")),
    ("email", "Email", ("Email",), ("m-mail-nightly-sort",)),
    ("brand-desk", "Brand Desk", ("Brand Desk",), ()),
    ("career", "Career Scout", ("Career Scout",), ()),
    ("privacy-scout", "Privacy Scout", ("Privacy Scout",), ()),
    ("legal", "Legal & Compliance", ("Legal", "Legal & Compliance"), ()),
    ("misc", "Misc", ("Misc",), ()),
]

BY_ID = {b[0]: b for b in BOTS}


def bot_for_owner(owner: str) -> str | None:
    for bid, _name, owners, _t in BOTS:
        if owner in owners:
            return bid
    return None
