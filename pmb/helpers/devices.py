# Copyright 2023 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pmb.config
import pmb.helpers.pmaports
from pmb.core.pkgrepo import pkgrepo_glob_one, pkgrepo_iglob
from pmb.helpers import logging


def find_path(codename: str, file: str = "") -> Path | None:
    """
    Find path to device APKBUILD under `device/*/device-`.

    :param codename: device codename
    :param file: file to look for (e.g. APKBUILD or deviceinfo), may be empty
    :returns: path to APKBUILD
    """
    g = pkgrepo_glob_one(f"device/*/device-{codename}/{file}")
    if not g:
        return None

    return g


# TODO: This could be simplified using StrEnum once we stop supporting Python 3.10.
class DeviceCategory(Enum):
    """Enum for representing a specific device category."""

    ARCHIVED = "archived"
    ALPINE = "alpine"
    DOWNSTREAM = "downstream"
    TESTING = "testing"
    COMMUNITY = "community"
    MAIN = "main"

    @staticmethod
    def shown() -> list[DeviceCategory]:
        """
        Get a list of all device categories that typically are visible, in order of "best" to
        "worst".

        :returns: List of all non-hidden device categories.
        """
        return [
            DeviceCategory.MAIN,
            DeviceCategory.COMMUNITY,
            DeviceCategory.TESTING,
            DeviceCategory.DOWNSTREAM,
            DeviceCategory.ALPINE,
        ]

    def allows_downstream_ports(self) -> bool:
        """
        Check whether a given category is allowed to contain downstream ports. This does not
        necessarily mean that it exclusively contains downstream ports.

        :returns: True, if the category allows downstream ports, False if only allows mainline ports.
        """
        match self:
            case DeviceCategory.ARCHIVED | DeviceCategory.DOWNSTREAM | DeviceCategory.ALPINE:
                return True
            case DeviceCategory.TESTING | DeviceCategory.COMMUNITY | DeviceCategory.MAIN:
                return False

    def explain(self) -> str:
        """
        Provide an explanation of a given category.

        :returns: String explaining the given category.
        """
        match self:
            case DeviceCategory.ARCHIVED:
                return "ports that have a better alternative available"
            case DeviceCategory.ALPINE:
                return (
                    "ports that install Alpine Linux only, because postmarketOS does not build"
                    " packages for their architecture (deviceinfo_alpine_only)"
                )
            case DeviceCategory.DOWNSTREAM:
                return "ports that use a downstream kernel — very limited functionality. Not recommended"
            case DeviceCategory.TESTING:
                return 'anything from "just boots in some sense" to almost fully functioning ports'

            case DeviceCategory.COMMUNITY:
                return "often mostly usable, but may lack important functionality"
            case DeviceCategory.MAIN:
                return "ports where mostly everything works"

    def color(self) -> str:
        """
        Returns the color associated with the given device category.

        :returns: ANSI escape sequence for the color associated with the given device category.
        """
        styles = pmb.config.styles

        match self:
            case DeviceCategory.ARCHIVED:
                return styles["RED"]
            case DeviceCategory.ALPINE:
                return styles["CYAN"]
            case DeviceCategory.DOWNSTREAM:
                return styles["YELLOW"]
            case DeviceCategory.TESTING:
                return styles["GREEN"]
            case DeviceCategory.COMMUNITY:
                return styles["BLUE"]
            case DeviceCategory.MAIN:
                return styles["MAGENTA"]

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class DeviceEntry:
    codename: str
    category: DeviceCategory

    def codename_without_vendor(self) -> str:
        return self.codename.split("-", 1)[1]

    def __str__(self) -> str:
        """Remove "vendor-" prefix from device codename and add category."""
        styles = pmb.config.styles
        return f"{self.category.color()}{self.codename_without_vendor()}{styles['END']} ({self.category})"


def list_codenames(vendor: str) -> list[DeviceEntry]:
    """
    Get all devices for which aports are available.

    :param vendor: Vendor name to choose devices from.
    :returns: ["first-device", "second-device", ...]}
    """
    ret: list[DeviceEntry] = []
    for path in pkgrepo_iglob(f"device/*/device-{vendor}-*"):
        codename = os.path.basename(path).split("-", 1)[1]
        # Ensure we don't crash on unknown device categories.
        try:
            category = get_device_category_by_directory_path(path)
        except RuntimeError as exception:
            logging.warning("WARNING: %s: %s", codename, exception)
            continue
        # Get rid of ports inside of hidden device categories.
        if category not in DeviceCategory.shown():
            continue
        ret.append(DeviceEntry(codename, category))
    return ret


def list_vendors() -> set[str]:
    """
    Get all device vendors, for which aports are available.

    :returns: {"vendor1", "vendor2", ...}
    """
    ret = set()
    for path in pkgrepo_iglob("device/*/device-*"):
        vendor = path.name.split("-", 2)[1]
        ret.add(vendor)
    return ret


def get_device_category_by_apkbuild_path(apkbuild_path: Path) -> DeviceCategory:
    """
    Get the category of a device based on the path to its APKBUILD inside of pmaports.

    This will fail to determine the device category from out-of-tree APKBUILDs.

    :apkbuild_path: Path to an APKBUILD within pmaports for a particular device.
    :returns: The device category of the provided device APKBUILD.
    """
    # Path is something like this:
    # .../device/community/device-samsung-m0/APKBUILD
    #            ↑         ↑ parent 1
    #            | parent 2
    category_str = apkbuild_path.parent.parent.name

    try:
        device_category = DeviceCategory(category_str)
    except ValueError as exception:
        raise RuntimeError(f'Unknown device category "{category_str}"') from exception

    return device_category


def get_device_category_by_directory_path(device_directory: Path) -> DeviceCategory:
    """
    Get the category of a device based on the path to its directory inside of pmaports.

    :device_directory: Path to the device package directory for a particular device.
    :returns: The device category of the provided device directory.
    """
    device_apkbuild_path = device_directory / "APKBUILD"

    return get_device_category_by_apkbuild_path(device_apkbuild_path)


def get_device_category_by_name(device_name: str) -> DeviceCategory:
    """
    Get the category of a device based on its name.

    :device_name: Name of a particular device to determine the category of.
                  Format should be "vendor-codename".
    :returns: The device category of the provided device name.
    """
    device_directory = pmb.helpers.pmaports.find(f"device-{device_name}")

    return get_device_category_by_directory_path(device_directory)
