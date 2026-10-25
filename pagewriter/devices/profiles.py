"""Immutable driver registry. A profile describes support, never calibration."""
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import re


@dataclass(frozen=True)
class Capabilities:
    axes: tuple[str, ...]
    pen_actuation: str
    transport: str
    simulation: bool = False
    pause_resume: bool = True
    bounded_jog: bool = True
    physical_completion: bool = False
    requires_runtime_calibration: bool = True
    manual_reposition_feedback: bool = False
    cancellation_semantics: str = 'hold_and_lock_device_queue_not_cleared'
    completion_detection: str = 'fresh_controller_idle'

    def to_dict(self):
        value = asdict(self)
        value['axes'] = list(self.axes)
        return value


@dataclass(frozen=True)
class DeviceProfile:
    id: str
    name: str
    driver: str
    capabilities: Capabilities
    support_status: str
    description: str
    axis_presets: tuple = field(default_factory=tuple)
    serial_baud: int | None = None
    max_write_feed: float | None = None
    max_travel_feed: float | None = None
    max_z_feed: float | None = None
    max_page_size: tuple[float,float] | None = None

    @property
    def presets(self):
        return dict(self.axis_presets)

    def to_dict(self):
        return dict(id=self.id,name=self.name,driver=self.driver,
                    capabilities=self.capabilities.to_dict(),support_status=self.support_status,
                    description=self.description,axis_presets=self.presets,
                    serial_baud=self.serial_baud,max_write_feed=self.max_write_feed,
                    max_travel_feed=self.max_travel_feed,max_z_feed=self.max_z_feed,max_page_size=self.max_page_size)


SONGBOSHI_PRESETS = (('songboshi_2024_paper',(0,1,-1,0)),
                    ('songboshi_2024_paper_180',(0,-1,1,0)))
_PROFILES = {
    'songboshi_2024': DeviceProfile('songboshi_2024','颂博士 2024 / BtWriter','grbl',
        Capabilities(('X','Y','Z'),'z_axis','serial'), 'observed_device',
        '已观测 BtWriter 协议与轴向；真实书写仍需当前会话标定和现场授权。',SONGBOSHI_PRESETS,
        serial_baud=115200,max_write_feed=4000,max_travel_feed=4000,max_z_feed=5000),
    'grbl_generic': DeviceProfile('grbl_generic','通用 Grbl（未验证配置）','grbl',
        Capabilities(('X','Y','Z'),'z_axis','serial'), 'unverified_configuration',
        '协议适配配置，不代表任意 Grbl 机器已获物理支持；无默认轴向预设，速度和工作尺寸未知。',serial_baud=115200),
    'simulator_servo': DeviceProfile('simulator_servo','离线模拟器 · XY + 舵机笔','servo_simulator',
        Capabilities(('X','Y'),'servo','memory',simulation=True,requires_runtime_calibration=False,
            cancellation_semantics='clear_virtual_queue',completion_detection='virtual_observation'),
        'virtual_only','纯内存模型，无串口、无真实移动；确认和完成分开模拟。'),
}
_BUILTIN_IDS=frozenset(_PROFILES)


def get_profile(profile_id='songboshi_2024'):
    try:
        return _PROFILES[profile_id]
    except KeyError:
        raise ValueError('Unknown device profile: '+str(profile_id)) from None


def list_profiles():
    return [profile.to_dict() for profile in _PROFILES.values()]


def register_profile(value):
    """Register validated model data, never executable driver code/calibration.

    Custom physical models remain explicitly unverified. Bounds omitted from
    data stay unknown; runtime operator authorization remains mandatory.
    Existing IDs cannot be overwritten, preserving active plan identities.
    """
    if isinstance(value,DeviceProfile): value=value.to_dict()
    if not isinstance(value,dict): raise ValueError('Device profile must be an object')
    allowed=set(DeviceProfile.__dataclass_fields__)
    if set(value)-allowed: raise ValueError('Unknown device profile fields')
    required={'id','name','driver','capabilities','description'}
    if not required<=set(value): raise ValueError('Missing device profile fields')
    identifier=value['id']
    if not isinstance(identifier,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',identifier):
        raise ValueError('Invalid device profile id')
    if identifier in _PROFILES: raise ValueError('Device profile id already registered')
    if not isinstance(value['name'],str) or not value['name'].strip() or len(value['name'])>120:
        raise ValueError('Invalid device profile name')
    if not isinstance(value['description'],str) or len(value['description'])>2000:
        raise ValueError('Invalid device profile description')
    if value['driver']!='grbl': raise ValueError('Custom model data currently supports the Grbl driver only')
    caps=value['capabilities']
    if not isinstance(caps,dict) or set(caps)-set(Capabilities.__dataclass_fields__): raise ValueError('Invalid capabilities')
    caps=dict(caps)
    if caps.get('axes') not in (['X','Y','Z'],('X','Y','Z')) or caps.get('pen_actuation')!='z_axis' or caps.get('transport')!='serial':
        raise ValueError('Capabilities incompatible with Grbl/Z adapter')
    for key in ('simulation','pause_resume','bounded_jog','physical_completion','requires_runtime_calibration','manual_reposition_feedback'):
        if key in caps and type(caps[key]) is not bool: raise ValueError('Capability flags must be booleans')
    if caps.get('simulation') or caps.get('physical_completion') or caps.get('manual_reposition_feedback') or caps.get('requires_runtime_calibration',True) is not True:
        raise ValueError('Custom physical profile cannot claim simulation, measured completion, or skip calibration')
    if caps.get('cancellation_semantics','hold_and_lock_device_queue_not_cleared')!='hold_and_lock_device_queue_not_cleared' or caps.get('completion_detection','fresh_controller_idle')!='fresh_controller_idle':
        raise ValueError('Capabilities incompatible with Grbl cancellation/completion semantics')
    caps['axes']=tuple(caps['axes'])
    capabilities=Capabilities(**caps)
    presets=value.get('axis_presets',{})
    if not isinstance(presets,dict) or len(presets)>20: raise ValueError('Invalid axis presets')
    validated=[]
    for name,mapping in presets.items():
        if not isinstance(name,str) or not name or len(name)>100 or not isinstance(mapping,(list,tuple)) or len(mapping)!=4:
            raise ValueError('Invalid axis preset')
        if not all(type(v) in (int,float) and math.isfinite(v) for v in mapping): raise ValueError('Invalid axis preset numbers')
        a,b,c,d=mapping
        if not .2<=math.hypot(a,b)<=5 or not .2<=math.hypot(c,d)<=5 or abs(a*d-b*c)<.04: raise ValueError('Invalid axis preset mapping')
        validated.append((name,tuple(mapping)))
    settings={key:value.get(key) for key in ('serial_baud','max_write_feed','max_travel_feed','max_z_feed','max_page_size')}
    if settings['serial_baud'] is None: settings['serial_baud']=115200
    if type(settings['serial_baud']) is not int or not 1200<=settings['serial_baud']<=4_000_000: raise ValueError('Invalid serial baud')
    for key in ('max_write_feed','max_travel_feed','max_z_feed'):
        v=settings[key]
        if v is not None and (type(v) not in (int,float) or not math.isfinite(v) or not 0<v<=1_000_000): raise ValueError('Invalid model feed limit')
    size=settings['max_page_size']
    if size is not None:
        if not isinstance(size,(list,tuple)) or len(size)!=2 or not all(type(v) in (int,float) and math.isfinite(v) and 0<v<=2000 for v in size):
            raise ValueError('Invalid model page size')
        settings['max_page_size']=tuple(size)
    if value.get('support_status','unverified_configuration')!='unverified_configuration':
        raise ValueError('Custom physical profiles must remain unverified_configuration')
    profile=DeviceProfile(identifier,value['name'],'grbl',capabilities,'unverified_configuration',
        value['description'],tuple(validated),**settings)
    _PROFILES[identifier]=profile
    return profile


def load_profile(path):
    """Explicit local JSON import. Importing the module never loads files."""
    path=Path(path)
    if path.stat().st_size>65536: raise ValueError('Device profile JSON is too large')
    return register_profile(json.loads(path.read_text(encoding='utf-8')))
