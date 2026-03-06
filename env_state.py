from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .env import MachineConfig


@dataclass
class BuildEnvState:
    meson: str
    build: MachineConfig
    host: Optional[MachineConfig]
    allowed_prebuilds: List[str]
    deps: Path


def dump_build_env_state(path: Path, state: BuildEnvState):
    allowed_prebuilds = state.allowed_prebuilds
    if not isinstance(allowed_prebuilds, list):
        allowed_prebuilds = sorted(allowed_prebuilds)

    path.write_text(json.dumps({
        "meson": state.meson,
        "build": _serialize_machine_config(state.build),
        "host": _serialize_machine_config(state.host),
        "allowed_prebuilds": allowed_prebuilds,
        "deps": str(state.deps),
    }), encoding="utf-8")


def load_build_env_state(path: Path) -> BuildEnvState:
    raw = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(raw, dict):
        raise ValueError("invalid env state: expected object")

    meson = _expect_string(raw, "meson")
    build = _deserialize_machine_config(_expect_object(raw, "build"))

    host_data = raw.get("host")
    if host_data is None:
        host = None
    else:
        if not isinstance(host_data, dict):
            raise ValueError("invalid env state: host must be object or null")
        host = _deserialize_machine_config(host_data)

    allowed_prebuilds = _expect_string_list(raw, "allowed_prebuilds")
    deps = Path(_expect_string(raw, "deps"))

    return BuildEnvState(
        meson=meson,
        build=build,
        host=host,
        allowed_prebuilds=allowed_prebuilds,
        deps=deps,
    )


def _serialize_machine_config(config: Optional[MachineConfig]) -> Optional[Dict[str, Any]]:
    if config is None:
        return None
    return {
        "machine_file": str(config.machine_file),
        "binpath": [str(path) for path in config.binpath],
        "environ": config.environ,
    }


def _deserialize_machine_config(raw: Dict[str, Any]) -> MachineConfig:
    machine_file = Path(_expect_string(raw, "machine_file"))
    binpath = [Path(path) for path in _expect_string_list(raw, "binpath")]
    environ = _expect_string_map(raw, "environ")
    return MachineConfig(
        machine_file=machine_file,
        binpath=binpath,
        environ=environ,
    )


def _expect_object(raw: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"invalid env state: '{key}' must be an object")
    return value


def _expect_string(raw: Dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"invalid env state: '{key}' must be a string")
    return value


def _expect_string_list(raw: Dict[str, Any], key: str) -> List[str]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"invalid env state: '{key}' must be a list of strings")
    return value


def _expect_string_map(raw: Dict[str, Any], key: str) -> Dict[str, str]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"invalid env state: '{key}' must be an object")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError(f"invalid env state: '{key}' must map strings to strings")
    return value
