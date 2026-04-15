"""
Multi-instance configuration loader.

Supports two env var patterns:

  Multi-instance:
    INDICO_INSTANCES=cern,su
    INDICO_DEFAULT=su
    INDICO_CERN_URL=https://indico.cern.ch
    INDICO_CERN_TOKEN=indp_xxx
    INDICO_SU_URL=https://indico.fysik.su.se
    INDICO_SU_TOKEN=indp_yyy
    INDICO_SU_ROOM_LOCATIONS=AlbaNova,Albano Building 3,Fysikum

  Single-instance shorthand:
    INDICO_BASE_URL=https://indico.cern.ch
    INDICO_TOKEN=indp_xxx          # optional for public instances
    INDICO_ROOM_LOCATIONS=Main Building,Building 40

Room location names are the site names configured in Indico's room booking module.
They seed the discover_rooms tool and are used as fallback when no rooms cache exists.
Run discover_rooms once to build a full catalogue; after that the cache is used instead.
"""

import os
import re
from dataclasses import dataclass, field

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class InstanceConfig:
    name: str
    base_url: str
    token: str | None
    room_locations: list[str] = field(default_factory=list)


def _parse_locations(value: str) -> list[str]:
    return [loc.strip() for loc in value.split(",") if loc.strip()]


class Config:
    def __init__(self) -> None:
        self._instances: dict[str, InstanceConfig] = {}
        self._default: str = ""
        self._load()

    def _load(self) -> None:
        instances_env = os.getenv("INDICO_INSTANCES")

        if instances_env:
            # Multi-instance mode
            names = [n.strip() for n in instances_env.split(",") if n.strip()]
            for name in names:
                if not _SAFE_NAME_RE.match(name):
                    raise ValueError(
                        f"Instance name '{name}' is invalid. "
                        "Names must contain only letters, digits, hyphens, and underscores."
                    )
                prefix = f"INDICO_{name.upper()}_"
                url = os.getenv(f"{prefix}URL")
                if not url:
                    raise ValueError(
                        f"INDICO_INSTANCES lists '{name}' but {prefix}URL is not set"
                    )
                token = os.getenv(f"{prefix}TOKEN")
                room_locations = _parse_locations(os.getenv(f"{prefix}ROOM_LOCATIONS", ""))
                self._instances[name] = InstanceConfig(
                    name=name, base_url=url.rstrip("/"), token=token,
                    room_locations=room_locations,
                )

            default = os.getenv("INDICO_DEFAULT", names[0])
            if default not in self._instances:
                raise ValueError(
                    f"INDICO_DEFAULT='{default}' is not in INDICO_INSTANCES={instances_env}"
                )
            self._default = default
        else:
            # Single-instance shorthand
            url = os.getenv("INDICO_BASE_URL")
            if not url:
                raise ValueError(
                    "Set INDICO_BASE_URL (and optionally INDICO_TOKEN) or use "
                    "INDICO_INSTANCES for multi-instance configuration."
                )
            token = os.getenv("INDICO_TOKEN")
            room_locations = _parse_locations(os.getenv("INDICO_ROOM_LOCATIONS", ""))
            self._instances["default"] = InstanceConfig(
                name="default", base_url=url.rstrip("/"), token=token,
                room_locations=room_locations,
            )
            self._default = "default"

    def get(self, name: str | None = None) -> InstanceConfig:
        key = name or self._default
        if key not in self._instances:
            available = ", ".join(self._instances)
            raise ValueError(
                f"Unknown instance '{key}'. Available: {available}. "
                f"Use one of these names or omit instance to use default '{self._default}'."
            )
        return self._instances[key]

    @property
    def instance_names(self) -> list[str]:
        return list(self._instances)

    @property
    def default_name(self) -> str:
        return self._default
