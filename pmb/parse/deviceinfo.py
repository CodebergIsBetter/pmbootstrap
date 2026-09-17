# Copyright 2023 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import copy
import glob
import inspect
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pmb.config
import pmb.helpers.devices
from pmb.core.arch import Arch
from pmb.core.context import get_context
from pmb.core.pkgrepo import pkgrepo_default_path
from pmb.helpers import logging
from pmb.helpers.exceptions import NonBugError
from pmb.helpers.toml import load_toml_file
from pmb.meta import Cache


class InitfsCompressionFormat(Enum):
    ZSTD = "zstd"
    LZ4 = "lz4"
    LZMA = "lzma"
    GZIP = "gzip"
    NONE = "none"

    @staticmethod
    def from_str(compression_format: str) -> InitfsCompressionFormat:
        try:
            return InitfsCompressionFormat(compression_format)
        except ValueError as exception:
            raise ValueError(f"Invalid compression format '{compression_format}'") from exception


class InitfsCompressionLevel(Enum):
    DEFAULT = "default"
    FAST = "fast"
    BEST = "best"

    @staticmethod
    def from_str(compression_level: str) -> InitfsCompressionLevel:
        try:
            return InitfsCompressionLevel(compression_level)
        except ValueError as exception:
            raise ValueError(f"Invalid compression level '{compression_level}'") from exception


@dataclass
class InitfsCompression:
    format_: InitfsCompressionFormat
    level: InitfsCompressionLevel | None

    @staticmethod
    def from_str(initfs_compression: str) -> InitfsCompression:
        segments = initfs_compression.split(":", maxsplit=1)

        # If we can't even figure out the format, the other information is no use. As such, let this
        # raise an exception if it happens.
        format_ = InitfsCompressionFormat.from_str(segments[0])

        try:
            level = InitfsCompressionLevel.from_str(segments[1]) if len(segments) == 2 else None
        except ValueError:
            level = None

        return InitfsCompression(format_, level)


# FIXME: It feels weird to handle this at parse time.
# we should instead have the Deviceinfo object store
# the attributes for all kernels and require the user
# to specify which one they're using.
# Basically: treat Deviceinfo as a standalone type that
# doesn't need to traverse pmaports.
def _parse_kernel_suffix(info: dict[str, str], device: str, kernel: str | None) -> dict[str, str]:
    """
    Remove the kernel suffix (as selected in 'pmbootstrap init') from
    deviceinfo variables. Related:
    https://wiki.postmarketos.org/wiki/Device_specific_package#Multiple_kernels

    :param info: deviceinfo dict, e.g.:
                 {"a": "first",
                  "b_mainline": "second",
                  "b_downstream": "third"}
    :param device: which device info belongs to
    :param kernel: which kernel suffix to remove (e.g. "mainline")
    :returns: info, but with the configured kernel suffix removed, e.g:
              {"a": "first",
               "b": "second",
               "b_downstream": "third"}
    """
    # FIXME: We shouldn't have test-specific code in regular implementations like this.
    # We don't support parsing the kernel variants in tests yet, since this code
    # depends on pmaports being available and calls into a whole lot of other code.
    if os.environ.get("PYTEST_CURRENT_TEST", "").startswith("test/parse/test_deviceinfo.py"):
        # If you hit this, you're probably trying to add a test for kernel variants.
        # You'll need to figure out how to mock the APKBUILD parsing below.
        if kernel is not None:
            raise ValueError
        return info
    # Do nothing if the configured kernel isn't available in the kernel (e.g.
    # after switching from device with multiple kernels to device with only one
    # kernel)
    kernels = pmb.parse._apkbuild.kernels(device)
    if not kernels or kernel not in kernels:
        logging.verbose(f"parse_kernel_suffix: {kernel} not in {kernels}")
        return info

    ret = copy.copy(info)

    suffix_kernel = kernel.replace("-", "_")
    for key in inspect.get_annotations(Deviceinfo):
        key_kernel = f"{key}_{suffix_kernel}"
        if key_kernel not in ret:
            continue

        # Move ret[key_kernel] to ret[key]
        logging.verbose(f"parse_kernel_suffix: {key_kernel} => {key}")
        ret[key] = ret[key_kernel]
        del ret[key_kernel]

    return ret


@Cache("device", "kernel")
def deviceinfo(device: str | None = None, kernel: str | None = None) -> Deviceinfo:
    """
    :param device: defaults to args.device
    :param kernel: defaults to args.kernel
    """
    context = get_context()
    if not device:
        device = context.config.device
    if not kernel:
        kernel = context.config.kernel

    path = pmb.helpers.devices.find_path(device, "deviceinfo")
    if not path:
        raise NonBugError(
            f"Device '{device}' not found. Run 'pmbootstrap init' to start a new device port or to "
            "choose another device. It may have been renamed, see <https://postmarketos.org/renamed>"
        )

    return Deviceinfo(path, kernel)


class Deviceinfo:
    """Variables from deviceinfo. Reference: <https://postmarketos.org/deviceinfo>"""

    path: Path
    # general
    format_version: str
    name: str
    manufacturer: str
    codename: str
    year: str
    dtb: str = ""
    arch: Arch

    # device
    chassis: str | None
    keyboard: str | None = ""  # deprecated
    drm: bool = False
    alpine_only: bool = False
    dev_touchscreen: str | None = ""
    dev_touchscreen_calibration: str | None = ""
    append_dtb: str | None = ""

    # bootloader
    flash_method: str = ""
    boot_filesystem: str | None = ""
    generate_cmdline_txt: bool = False
    create_initfs_extra: bool | None = False
    create_prep_boot: bool | None = False
    initfs_compression: InitfsCompression = InitfsCompression(InitfsCompressionFormat.GZIP, None)

    # flash
    flash_heimdall_partition_kernel: str | None = ""
    flash_heimdall_partition_vendor_boot: str | None = ""
    flash_heimdall_partition_initfs: str | None = ""
    flash_heimdall_partition_rootfs: str | None = ""
    flash_heimdall_partition_system: str | None = ""  # deprecated
    flash_heimdall_partition_vbmeta: str | None = ""
    flash_heimdall_partition_dtbo: str | None = ""
    flash_fastboot_partition_kernel: str | None = ""
    flash_fastboot_partition_vendor_boot: str | None = ""
    flash_fastboot_partition_rootfs: str | None = ""
    flash_fastboot_partition_system: str | None = ""  # deprecated
    flash_fastboot_partition_vbmeta: str | None = ""
    flash_fastboot_partition_dtbo: str | None = ""
    flash_rk_partition_kernel: str | None = ""
    flash_rk_partition_vendor_boot: str | None = ""
    flash_rk_partition_rootfs: str | None = ""
    flash_rk_partition_system: str | None = ""  # deprecated
    flash_mtkclient_partition_kernel: str | None = ""
    flash_mtkclient_partition_vendor_boot: str | None = ""
    flash_mtkclient_partition_rootfs: str | None = ""
    flash_mtkclient_partition_vbmeta: str | None = ""
    flash_mtkclient_partition_dtbo: str | None = ""
    generate_legacy_uboot_initfs: str | None = ""
    kernel_cmdline: str | None = ""
    generate_bootimg: str | None = ""
    header_version: int | None = None
    bootimg_qcdt: str | None = ""
    bootimg_qcdt_type: str | None = ""
    bootimg_mtk_mkimage: str | None = ""  # deprecated
    bootimg_mtk_label_kernel: str | None = ""
    bootimg_mtk_label_ramdisk: str | None = ""
    bootimg_dtb_second: str | None = ""
    bootimg_custom_args: str | None = ""
    flash_offset_base: str | None = ""
    flash_offset_dtb: str | None = ""
    flash_offset_kernel: str | None = ""
    flash_offset_ramdisk: str | None = ""
    flash_offset_second: str | None = ""
    flash_offset_tags: str | None = ""
    flash_pagesize: str | None = ""
    flash_fastboot_max_size: str | None = ""
    flash_sparse: str | None = ""
    flash_sparse_samsung_format: str | None = ""
    rootfs_image_sector_size: str | None = ""
    sd_embed_firmware: str | None = ""
    sd_embed_firmware_step_size: str | None = ""
    partition_blacklist: str | None = ""
    boot_part_start: str | None = ""
    partition_type: str | None = ""
    root_filesystem: str | None = ""
    flash_kernel_on_update: str | None = ""
    cgpt_kpart: str | None = ""
    cgpt_kpart_start: str | None = ""
    cgpt_kpart_size: str | None = ""

    # weston
    weston_pixman_type: str | None = ""

    # keymaps
    keymaps: str | None = ""

    def __init__(self, path: Path, kernel: str | None = None) -> None:
        ret = {}
        with open(path) as handle:
            for line in handle:
                if not line.startswith("deviceinfo_"):
                    continue
                if "=" not in line:
                    raise SyntaxError(f"{path}: No '=' found:\n\t{line}")
                split = line.split("=", 1)
                key = split[0][len("deviceinfo_") :]
                value = split[1].replace('"', "").replace("\n", "")
                ret[key] = value

        ret = _parse_kernel_suffix(ret, ret["codename"], kernel)

        for key, value in ret.items():
            # FIXME: something to turn on and fix in the future
            # Should be rewritten to use deviceinfo_schema.toml as source of truth instead of the
            # Deviceinfo class keys first though.
            # if key not in Deviceinfo.__annotations__.keys():
            #     logging.warning(f"deviceinfo: {key} is not a known attribute")
            match key:
                case "arch":
                    self.arch = Arch.from_str(value)
                case "gpu_accelerated":  # deprecated
                    self.drm = value == "true"
                case "drm":
                    self.drm = value == "true"
                case "alpine_only":
                    self.alpine_only = value == "true"
                case "generate_cmdline_txt":
                    self.generate_cmdline_txt = value == "true"
                case "header_version":
                    self.header_version = int(value)
                case "initfs_compression":
                    self.initfs_compression = InitfsCompression.from_str(value)
                case _:
                    setattr(self, key, value)

        if not self.flash_method:
            self.flash_method = "none"


@Cache("device")
def device_is_alpine_only(device: str | None = None) -> bool:
    """Check whether a device is installed from Alpine's repositories only.

    This deliberately does not go through :func:`deviceinfo` or
    :func:`pmb.helpers.devices.find_path`. Both of them end up in
    ``pkgrepo_paths()``, which needs to know whether the systemd extra-repo is
    enabled, which asks for the service manager, which asks this function: an
    infinite recursion. So look the single variable up by hand instead, in
    pmaports only (device packages never live in an extra-repo).

    It also never raises: it is called from code paths that run without a
    device being configured (e.g. ``pmbootstrap chroot`` on the native chroot),
    and the answer for those is "no".

    :param device: defaults to the configured device
    """
    if not device:
        try:
            device = get_context().config.device
        except RuntimeError:  # no context set up (yet)
            return False
    if not device:
        return False

    try:
        matches = glob.glob(
            os.path.join(pkgrepo_default_path(), f"device/*/device-{device}/deviceinfo")
        )
    except RuntimeError:  # no package repositories configured
        return False
    if len(matches) != 1:
        return False

    # deviceinfo_alpine_only is device wide, it has no kernel variants, so
    # there is no need to run the kernel suffix parsing here.
    with open(matches[0]) as handle:
        for line in handle:
            if line.startswith("deviceinfo_alpine_only="):
                return line.split("=", 1)[1].strip().strip('"') == "true"
    return False


class DeviceinfoSchemaDatatype(Enum):
    INTEGER = "integer"
    ENUMERATION = "enumeration"
    BOOLEAN = "boolean"
    STRING = "string"

    @property
    def default_value_type(self) -> type:
        match self:
            case DeviceinfoSchemaDatatype.INTEGER:
                return int
            case DeviceinfoSchemaDatatype.ENUMERATION:
                return str
            case DeviceinfoSchemaDatatype.BOOLEAN:
                return bool
            case DeviceinfoSchemaDatatype.STRING:
                return str


@dataclass
class DeviceinfoSchemaVariable:
    """A variable in the deviceinfo schema."""

    name: str
    description: str
    datatype: DeviceinfoSchemaDatatype = DeviceinfoSchemaDatatype.STRING
    mandatory: bool = False
    allow_device_variant_suffix: bool = False
    behaviour_if_unset: str | None = None
    fate: str | None = None
    # Any following attributes could be expressed more concisely if not for
    # Python's quite limited type system / enum APIs
    epitaph: str | None = None
    integer_interval: range | None = None
    enum_values: set[str] | None = None
    # mypy treats Any | None different from Any
    default_value: Any | None = None

    def validate(self) -> None:
        # This is where we test what Python cannot test for us, i.e. that all
        # attributes are only set if specific conditions are met
        if self.integer_interval and self.datatype != DeviceinfoSchemaDatatype.INTEGER:
            raise NonBugError(
                f"Deviceinfo schema variable deviceinfo_{self.name} does not set integer type but has an integer interval"
            )
        if self.enum_values and self.datatype != DeviceinfoSchemaDatatype.ENUMERATION:
            raise NonBugError(
                f"Deviceinfo schema variable deviceinfo_{self.name} does not set enumeration type but has enum values"
            )
        if self.epitaph and not self.fate:
            raise NonBugError(
                f"Deviceinfo schema variable deviceinfo_{self.name} has an epitaph but does not have a fate set"
            )

        if self.default_value is not None:
            default_value_type = type(self.default_value)
            expected_value_type = self.datatype.default_value_type
            if default_value_type != expected_value_type:
                raise NonBugError(
                    f"Deviceinfo schema variable deviceinfo_{self.name} has default value of type {default_value_type}, expected {expected_value_type}"
                )


@dataclass
class DeviceinfoSchemaCategory:
    """A category in the deviceinfo schema."""

    name: str
    description: str
    variables: dict[str, DeviceinfoSchemaVariable]


@dataclass
class DeviceinfoSchema:
    """Parsed deviceinfo schema."""

    schema_version: str
    categories: dict[str, DeviceinfoSchemaCategory]
    # We're omitting renames for now

    def get(self, category_name: str, variable_name: str) -> DeviceinfoSchemaVariable | None:
        """Look up a variable in a category of the schema"""
        if not (category := self.categories.get(category_name)):
            return None
        return category.variables.get(variable_name)


@Cache()
def deviceinfo_schema() -> DeviceinfoSchema:
    """Parse the deviceinfo_schema.toml file in pmaports into a structured format"""
    deviceinfo_schema_path = pkgrepo_default_path() / "deviceinfo_schema.toml"
    if not deviceinfo_schema_path.exists():
        raise NonBugError(
            "pmaports checkout does not contain a 'deviceinfo_schema.toml'. You may be on an unsupported branch"
        )

    toml = load_toml_file(deviceinfo_schema_path)

    if not (metadata := toml.get("metadata")):
        raise NonBugError("Deviceinfo schema does not contain a 'metadata' section")

    if (schema_version := metadata.get("schema_version")) != "0.1":
        raise NonBugError(
            f"pmbootstrap only implements deviceinfo schema version 0.1, got {schema_version}"
        )

    if not (categories := metadata.get("categories")):
        raise NonBugError("No categories found under the 'metadata' table in the deviceinfo schema")

    if not (variables := toml.get("variable")):
        raise NonBugError("No variables found in the deviceinfo schema")

    # For the sake of simplicity we assume that the file is mostly
    # well-formatted, otherwise this would be extremely verbose

    parsed_categories: dict[str, DeviceinfoSchemaCategory] = {}

    for category_name, category_description in categories.items():
        parsed_variables: dict[str, DeviceinfoSchemaVariable] = {}

        for variable_name, variable in variables[category_name].items():
            # Some of these need type conversions
            if datatype := variable.get("datatype"):
                variable["datatype"] = DeviceinfoSchemaDatatype(datatype)
            if enum_values := variable.get("enum_values"):
                variable["enum_values"] = set(enum_values)
            if interval := variable.get("integer_interval"):
                parts = interval[1:-1].split(", ")
                start = int(parts[0])
                end = int(parts[1])
                # Python ranges are start inclusive by default
                if interval[0] == "(":
                    start += 1
                # ...and end exclusive by default
                if interval[-1] == "]":
                    end += 1
                variable["integer_interval"] = range(start, end)

            parsed_variable = DeviceinfoSchemaVariable(
                name=variable_name,
                **variable,
            )
            parsed_variable.validate()
            parsed_variables[variable_name] = parsed_variable

        parsed_category = DeviceinfoSchemaCategory(
            name=category_name, description=category_description, variables=parsed_variables
        )
        parsed_categories[category_name] = parsed_category

    return DeviceinfoSchema(schema_version=schema_version, categories=parsed_categories)


@Cache()
def deviceinfo_schema_default_boot_filesystem() -> str:
    """Reads the deviceinfo schema and returns the default boot filesystem specified"""
    variable = deviceinfo_schema().get("flash", "boot_filesystem")
    if variable and variable.default_value is not None:
        return variable.default_value
    elif variable:
        raise NonBugError(
            "[variable.flash.boot_filesystem] lacks a default value in the deviceinfo schema"
        )
    else:
        raise NonBugError(
            "Could not find [variable.flash.boot_filesystem] in the deviceinfo schema"
        )
