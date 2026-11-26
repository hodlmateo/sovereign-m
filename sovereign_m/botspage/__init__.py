"""M Bots page: watch the fleet and follow up with each bot, from the rig, on the local model.

``web.handle`` is a pure request handler (no sockets) that the JARVIS server can call for
``/bots``. Nothing here runs a command from page input, sends to Claude or Grok, edits
tasks.jsonl, or restarts anything. Writes are new files only (O_EXCL, -vN).
"""
