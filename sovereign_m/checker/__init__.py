"""Local Checker: re-verify a card's proof on the rig. PASS/FAIL, never "done".

It writes ``<board>/proof/<card_id>.check.md`` and, on FAIL or any error, an
escalation note. It never edits tasks.jsonl and never marks a card done.
"Can't verify" is FAIL.
"""
