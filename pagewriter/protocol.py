"""Compatibility imports for the Grbl adapter and offline protocol demonstration.

New device-independent jobs are defined in :mod:`pagewriter.devices.ir`.
"""
from .devices.grbl_protocol import (Event, LineParser, JobState, SimulatedTransport,
    JobSession, MachineProfile, compile_machine_job, simulate_job)
