"""Three-lane router: small (7B, local runner) / big (35B, handoff) / cloud (handoff, send to Claude or Grok).

The 7B proposes a lane. The regex classifier in sovereign_m.handoff.classify is a HARD FLOOR:
if it says needs-code, the lane can never be small. If the 7B is down or answers garbage the
router fails closed to needs-code (lane big) and says so. It never starts or stops a model server.
"""
