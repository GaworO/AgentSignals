"""Fail closed before observer writes; never changes the shared market intake."""
import json
import shutil
from pathlib import Path

MIB = 1024 * 1024


class WritePaused(ValueError):
    def __init__(self, reason, **evidence):
        super().__init__(reason)
        self.evidence = dict(reason=reason, **evidence)


class WriteGuard:
    def __init__(self, cfg):
        self.root = Path(cfg.get('DATA_DIR', '/data/builder50')) / 'ict_ai'
        self.mode = cfg.get('ICT_AI_MODE', 'OFF')
        self.minimum = max(128, int(cfg.get('ICT_AI_MIN_FREE_MB', 512))) * MIB
        self.paths = [self.root]
        self.paths += [Path(cfg[key]).parent for key in
                       ('ICT_AI_OBSERVE_DB', 'ICT_AI_REASONING_DB') if cfg.get(key)]

    def __call__(self):
        if (self.root / 'observer.pause').exists():
            raise WritePaused('OPERATOR_PAUSE')
        config = self.root / 'runtime-config.json'
        if config.exists():
            try:
                mode = json.loads(config.read_text()).get('ICT_AI_MODE', 'OFF')
            except (OSError, ValueError):
                raise WritePaused('PRIVATE_CONFIG_UNREADABLE') from None
            if mode != self.mode or mode not in ('OBSERVE', 'SHADOW_AI'):
                raise WritePaused('PRIVATE_MODE_CHANGED_RESTART_REQUIRED')
        checks = []
        for path in self.paths:
            while not path.exists() and path != path.parent:
                path = path.parent
            try:
                free = shutil.disk_usage(path).free
            except OSError:
                raise WritePaused('FREE_SPACE_UNAVAILABLE', path=str(path)) from None
            if free < self.minimum:
                raise WritePaused('LOW_FREE_SPACE', path=str(path), free_bytes=free,
                                  minimum_free_bytes=self.minimum)
            checks.append(free)
        return dict(free_bytes=min(checks), minimum_free_bytes=self.minimum)
