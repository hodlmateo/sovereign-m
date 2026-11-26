"""Local roundtable: bot-to-bot exchanges on a local model for one board card.

The runner has no capability to run shell commands, send, post or trade. It reads
the board (read-only), calls a local model, and writes a transcript plus at most
one draft file under the roundtable drafts directory.
"""
