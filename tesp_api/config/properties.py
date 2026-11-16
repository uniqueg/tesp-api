import os
from typing import Any, Sequence

from dynaconf import Dynaconf
from dynaconf.utils.parse_conf import parse_conf_data

ENVVAR_PREFIX = "TESP_API"


def get_properties(settings_files: Sequence[str] = ("settings.toml", ".secrets.toml")) -> Dynaconf:
    return Dynaconf(
        settings_files=list(settings_files),
        environments=True,
        default_env="default",
        env_switcher="FASTAPI_PROFILE",
        envvar_prefix=ENVVAR_PREFIX,
        merge_enabled=True
    )


properties = get_properties()


def replacing_list(settings: Dynaconf, key: str) -> Any:
    """
    A list setting whose environment variable replaces the file value. With
    merge_enabled, Dynaconf would append it to the file's list, which would
    widen allowlists such as accepted issuers or admitted groups.
    """
    name = f"{ENVVAR_PREFIX}_{key.replace('.', '__')}".upper()
    for env_name, value in os.environ.items():
        if env_name.upper() == name:
            return parse_conf_data(value, tomlfy=True, box_settings=settings)
    return settings.get(key)
