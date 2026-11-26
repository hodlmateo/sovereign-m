"""Local runner and writer: run "local" cards through the roundtable on the local model.

Drafts go to <board>/roundtable/drafts/, the local Checker looks at the evidence,
and results are written as a PROPOSED board update (<board>/proposed/<date>.jsonl)
plus a short daylog summary. tasks.jsonl is never edited and nothing is sent.
"""
