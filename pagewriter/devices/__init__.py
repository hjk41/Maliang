"""Device profiles and controller factory; importing never opens hardware."""
from .profiles import Capabilities, DeviceProfile, get_profile, list_profiles, register_profile, load_profile
from .ir import Move, Pen, Operation, compile_paths
from .base import DeviceController


def _create_grbl(profile_id, **kwargs):
    from ..controller import MachineController
    return MachineController(profile_id=profile_id,**kwargs)


def _create_servo_simulator(profile_id, **kwargs):
    from .simulator import SimulatedController
    return SimulatedController(profile_id=profile_id,**kwargs)


# Trusted built-in adapter registration point. Profile data selects a known
# driver; capability flags describe behavior and never choose implementation.
_DRIVER_FACTORIES = {
    'grbl': _create_grbl,
    'servo_simulator': _create_servo_simulator,
}


def create_controller(profile_id='songboshi_2024', **kwargs):
    profile = get_profile(profile_id)
    try:
        factory = _DRIVER_FACTORIES[profile.driver]
    except KeyError:
        raise ValueError('Unknown device driver: '+str(profile.driver)) from None
    return factory(profile_id=profile_id,**kwargs)
