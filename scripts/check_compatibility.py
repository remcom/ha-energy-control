"""Import every integration module against the selected Home Assistant release.

Run alongside the pure tests when a current HA version is newer than the
published pytest-homeassistant-custom-component fixture package supports.
"""

import importlib
import pkgutil
import sys
from pathlib import Path

# Support invoking the script directly from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import homeassistant  # noqa: F401 — initializes HA's dependency aliases first
from homeassistant.const import __version__ as ha_version

import custom_components.energy_control as integration

modules = [module.name for module in pkgutil.walk_packages(integration.__path__, integration.__name__ + ".")]
for name in modules:
    importlib.import_module(name)
print(f"Imported {len(modules) + 1} Energy Control modules on Home Assistant {ha_version}")
