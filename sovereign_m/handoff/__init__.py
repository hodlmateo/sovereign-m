"""Handoff writer: turn open needs-code cards into ready-to-paste prompts for the operator.

Reads tasks.jsonl (read-only). Writes only new files under <board>/handoffs/.
Never sends anything to Claude or Grok; the operator pastes it.
"""
