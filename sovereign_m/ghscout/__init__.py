"""Local, read-only GitHub open-source scout for M.

Finds repos (public GitHub search, GET only), scores them with a fixed /100 rubric, and writes a
report. Repo code never runs on the host: tests run only inside a bubblewrap sandbox with no network,
no home directory and a time limit; otherwise the repo is scored from reading and marked NOT RUN.
The local model writes notes and a one-line fit for M; it never sets the score.
"""
