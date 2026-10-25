"""Backward-compatible Songboshi/Grbl controller entry point.

Importing or constructing a controller never opens a device.
"""
from .devices.grbl_controller import GrblController, fit_affine


class MachineController(GrblController):
    """Legacy controller name; defaults to the tested Songboshi profile."""
