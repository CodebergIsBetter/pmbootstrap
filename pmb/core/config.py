# Copyright 2024 Casey Connolly
# SPDX-License-Identifier: GPL-3.0-or-later

import enum
import inspect
import multiprocessing
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, ClassVar, TypedDict


class Mirrors(TypedDict):
    alpine_custom: str
    alpine: str
    pmaports_custom: str
    pmaports: str
    systemd_custom: str
    systemd: str


class ServiceManagerConfig(enum.Enum):
    DEFAULT = "default"
    OPENRC = "openrc"
    SYSTEMD = "systemd"

    def __str__(self) -> str:
        return self.value

    @staticmethod
    def choices() -> list[str]:
        return [e.value for e in ServiceManagerConfig]


class AutoZapConfig(enum.Enum):
    NO = "no"
    YES = "yes"
    SILENTLY = "silently"

    def __str__(self) -> str:
        return self.value

    def enabled(self) -> bool:
        return self != AutoZapConfig.NO

    def noisy(self) -> bool:
        return self == AutoZapConfig.YES


class Config:
    aports: Path = Path(os.path.expanduser("~") + "/.local/var/pmbootstrap/cache_git/pmaports")
    boot_size: int = 512
    build_default_device_arch: bool = False
    build_pkgs_on_install: bool = True
    ccache_size: str = "5G"  # yeahhhh this one has a suffix
    device: str = "qemu-amd64"
    extra_packages: str = "none"
    extra_space: int = 0
    headless: bool = False
    hostname: str = ""
    is_default_channel: bool = True
    jobs: int = multiprocessing.cpu_count()
    kernel: str = "stable"
    keymap: str = ""
    locale: str = "en_US.UTF-8"
    mirrors: ClassVar[Mirrors] = {
        "alpine_custom": "none",
        "alpine": "http://dl-cdn.alpinelinux.org/alpine/",
        "pmaports_custom": "none",
        "pmaports": "http://mirror.postmarketos.org/postmarketos/",
        "systemd_custom": "none",
        "systemd": "http://mirror.postmarketos.org/postmarketos/extra-repos/systemd/",
    }
    qemu_redir_stdio: bool = False
    service_manager: ServiceManagerConfig = ServiceManagerConfig.DEFAULT
    ssh_key_glob: str = "~/.ssh/*.pub"
    ssh_keys: bool = False
    sudo_timer: bool = False
    timezone: str = "GMT"
    ui: str = "console"
    ui_extras: bool = False
    user: str = "user"
    work: Path = Path(os.path.expanduser("~") + "/.local/var/pmbootstrap")
    # automatically zap chroots that are for the wrong channel
    auto_zap_misconfigured_chroots: AutoZapConfig = AutoZapConfig.NO

    providers: ClassVar[dict[str, str]] = {}

    def __init__(self) -> None:
        # Make sure we aren't modifying the class defaults
        for key in inspect.get_annotations(Config):
            setattr(self, key, deepcopy(Config.get_default(key)))

    @staticmethod
    def keys() -> list[str]:
        keys = list(inspect.get_annotations(Config).keys())
        keys.remove("mirrors")
        keys += [f"mirrors.{k}" for k in inspect.get_annotations(Mirrors)]
        return sorted(keys)

    @staticmethod
    def get_default(dotted_key: str) -> Any:
        """
        Get the default value for a config option, supporting
        nested dictionaries (e.g. "mirrors.alpine").
        """
        keys = dotted_key.split(".")
        if len(keys) == 1:
            return getattr(Config, keys[0])
        elif len(keys) == 2:
            return getattr(Config, keys[0])[keys[1]]
        else:
            raise ValueError(f"Invalid dotted key: {dotted_key}")

    def __setattr__(self, key: str, value: Any) -> None:
        """
        Allow for setattr() to be used with a dotted key
        to set nested dictionaries (e.g. "mirrors.alpine").
        """
        keys = key.split(".")
        if len(keys) == 1:
            type_ = type(getattr(Config, key))
            try:
                value = type_(value)
            except TypeError:
                # Handle transition from aports as list[Path] to Path.
                if key == "aports" and isinstance(value, list):
                    value = value[-1]
                else:
                    raise
            try:
                if type_ is bool and isinstance(value, str):
                    if value.lower() in ["true", "false"]:
                        super().__setattr__(key, value.lower() == "true")
                    else:
                        raise ValueError()
                else:
                    super().__setattr__(key, type_(value))
            except ValueError as exception:
                msg = f"Invalid value for '{key}': '{value}' "
                if issubclass(type_, enum.Enum):
                    valid = [x.value for x in type_]
                    msg += f"(valid values: {', '.join(valid)})"
                else:
                    msg += f"(expected {type_}, got {type(value)})"
                raise ValueError(msg) from exception
        elif len(keys) == 2:
            super().__getattribute__(keys[0])[keys[1]] = value
        else:
            raise ValueError(f"Invalid dotted key: {key}")

    def __getattribute__(self, key: str) -> Any:
        """
        Allow for getattr() to be used with a dotted key
        to get nested dictionaries (e.g. "mirrors.alpine").
        """
        keys = key.split(".")
        if len(keys) == 1:
            return super().__getattribute__(key)
        elif len(keys) == 2:
            return super().__getattribute__(keys[0])[keys[1]]
        else:
            raise ValueError(f"Invalid dotted key: {key}")
