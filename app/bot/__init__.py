"""Pacote do robô Sitrax."""

from .report import build_narrative_report, Position, positions_from_rows

__all__ = ["SitraxBot", "build_narrative_report", "Position", "positions_from_rows"]


def __getattr__(name: str):
    if name == "SitraxBot":
        from .sitrax import SitraxBot

        return SitraxBot
    raise AttributeError(name)
