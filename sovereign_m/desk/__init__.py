"""Paper book. Local CSV in, in-memory ledger out."""

from sovereign_m.desk.engine import DISCLAIMER, PaperDesk, format_trace
from sovereign_m.desk.tape import load_bars

__all__ = ["DISCLAIMER", "PaperDesk", "format_trace", "load_bars"]
