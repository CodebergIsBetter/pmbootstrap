# Copyright 2023 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
import os
from pathlib import Path

import pmb.helpers.cli
import pmb.helpers.devices
import pmb.helpers.run
import pmb.parse
from pmb.core.arch import Arch
from pmb.core.context import get_context
from pmb.helpers import logging
from pmb.types import Bootimg


def ask_for_architecture() -> Arch:
    architectures = [str(a) for a in Arch.supported()]
    while True:
        ret = pmb.helpers.cli.ask(
            "Device architecture", architectures, "aarch64", complete=architectures
        )
        if ret in architectures:
            if ret == "armhf":
                # ARMv6. postmarketOS does not build armhf packages anymore, so
                # such a port needs deviceinfo_alpine_only="true" and must get
                # everything from Alpine's repositories.
                logging.info(
                    "NOTE: postmarketOS does not build armhf packages. This port will be"
                    ' generated with deviceinfo_alpine_only="true", which installs Alpine'
                    " Linux only. See <https://postmarketos.org/deviceinfo>."
                )
            return Arch.from_str(ret)
        logging.fatal(
            "ERROR: Invalid architecture specified. If you want to add a new architecture, edit "
            "Arch in pmb/core/arch.py."
        )


def ask_for_manufacturer() -> str:
    logging.info("Who produced the device (e.g. LG)?")
    return pmb.helpers.cli.ask("Manufacturer", None, None, False)


def ask_for_name(manufacturer: str) -> str:
    logging.info("What is the official name (e.g. Google Nexus 5)?")
    ret = pmb.helpers.cli.ask("Name", None, None, False)

    # Always add the manufacturer
    if not ret.startswith(manufacturer) and not ret.startswith("Google"):
        ret = manufacturer + " " + ret
    return ret


def ask_for_year() -> str:
    # Regex from https://stackoverflow.com/a/12240826
    logging.info("In what year was the device released (e.g. 2012)?")
    return pmb.helpers.cli.ask("Year", None, None, False, validation_regex=r"^[1-9]\d{3,}$")


def ask_for_chassis() -> str:
    types = pmb.config.deviceinfo_chassis_types

    logging.info("What type of device is it?")
    logging.info("Valid types are: " + ", ".join(types))
    logging.info("Leave empty if this property is defined elsewhere, e.g. in Devicetree or ACPI")
    return pmb.helpers.cli.ask(
        "Chassis", None, None, True, validation_regex="|".join([*types, "None"]), complete=types
    )


def ask_for_flash_method() -> str:
    while True:
        logging.info("Which flash method does the device support?")
        method = pmb.helpers.cli.ask(
            "Flash method", pmb.config.flash_methods, "none", complete=pmb.config.flash_methods
        )

        if method in pmb.config.flash_methods:
            if method == "heimdall":
                heimdall_types = ["isorec", "bootimg"]
                while True:
                    logging.info('Does the device use the "isolated recovery" or boot.img?')
                    logging.info(
                        "<https://wiki.postmarketos.org/wiki/Deviceinfo_flash_methods#Isorec_or_bootimg.3F>"
                    )
                    heimdall_type = pmb.helpers.cli.ask("Type", heimdall_types, heimdall_types[0])
                    if heimdall_type in heimdall_types:
                        method += "-" + heimdall_type
                        break
                    logging.fatal("ERROR: Invalid type specified.")
            return method

        logging.fatal(
            "ERROR: Invalid flash method specified. If you want to add a new flash method, edit "
            "flash_methods in pmb/config/__init__.py."
        )


def ask_for_bootimg() -> Bootimg | None:
    logging.info(
        "You can analyze a known working boot.img file to automatically fill out the flasher "
        "information for your deviceinfo file. Either specify the path to an image or press return "
        "to skip this step (you can do it later with 'pmbootstrap bootimg_analyze')."
    )

    while True:
        response = pmb.helpers.cli.ask("Path", None, "", False)
        if not response:
            return None
        path = Path(os.path.expanduser(response))
        try:
            return pmb.parse.bootimg(path)
        except RuntimeError as e:
            logging.fatal("ERROR: " + str(e) + ". Please try again.")


def generate_deviceinfo_fastboot_content(bootimg: Bootimg | None = None) -> str:
    if bootimg is None:
        bootimg = Bootimg(
            cmdline="",
            bootimg_qcdt="false",
            bootimg_qcdt_type=None,
            bootimg_qcdt_exynos_platform=None,
            bootimg_qcdt_exynos_subtype=None,
            dtb_offset=None,
            dtb_second="false",
            base="",
            kernel_offset="",
            ramdisk_offset="",
            second_offset="",
            tags_offset="",
            pagesize="2048",
            header_version=None,
            mtk_label_kernel="",
            mtk_label_ramdisk="",
        )

    content = f"""\
        deviceinfo_kernel_cmdline="{bootimg["cmdline"]}"
        deviceinfo_generate_bootimg="true"
        deviceinfo_flash_pagesize="{bootimg["pagesize"]}"
        """

    for k in [
        "bootimg_qcdt",
        "bootimg_qcdt_type",
        "bootimg_qcdt_exynos_platform",
        "bootimg_qcdt_exynos_subtype",
        "dtb_second",
        "mtk_label_kernel",
        "mtk_label_ramdisk",
        "header_version",
    ]:
        v = bootimg[k]  # type: ignore[literal-required]
        if v:
            content += f"""\
            deviceinfo_{k}="{v}"
            """

    if bootimg["header_version"] == 2:
        content += f"""\
        deviceinfo_append_dtb="false"
        deviceinfo_flash_offset_dtb="{bootimg["dtb_offset"]}"
        """

    if bootimg["base"]:
        content += f"""\
        deviceinfo_flash_offset_base="{bootimg["base"]}"
        deviceinfo_flash_offset_kernel="{bootimg["kernel_offset"]}"
        deviceinfo_flash_offset_ramdisk="{bootimg["ramdisk_offset"]}"
        deviceinfo_flash_offset_second="{bootimg["second_offset"]}"
        deviceinfo_flash_offset_tags="{bootimg["tags_offset"]}"
        """

    return content


def generate_deviceinfo(
    pkgname: str,
    name: str,
    manufacturer: str,
    year: str,
    arch: Arch,
    chassis: str,
    flash_method: str,
    device_category: pmb.helpers.devices.DeviceCategory,
    bootimg: Bootimg | None = None,
) -> None:
    codename = "-".join(pkgname.split("-")[1:])
    # Note: New variables must be added to pmb/config/__init__.py as well
    content = f"""\
        # Reference: <https://postmarketos.org/deviceinfo>
        # Please use double quotes only. You can source this file in shell
        # scripts.

        deviceinfo_format_version="0"
        deviceinfo_name="{name}"
        deviceinfo_manufacturer="{manufacturer}"
        deviceinfo_codename="{codename}"
        deviceinfo_year="{year}"
        deviceinfo_dtb=""
        deviceinfo_arch="{arch}"

        # Device related
        """

    if arch == Arch.armhf:
        # postmarketOS does not build armhf packages, so such a device can only
        # be installed from Alpine's repositories
        content += 'deviceinfo_alpine_only="true"\n'

    content += f'deviceinfo_chassis="{chassis}"\n' if chassis != "None" else ""

    if device_category == pmb.helpers.devices.DeviceCategory.DOWNSTREAM:
        content += 'deviceinfo_initfs_compression="gzip"\n'

    content += f"""\

        # Bootloader related
        deviceinfo_flash_method="{flash_method}"
        """

    content_heimdall_bootimg = """\
        deviceinfo_flash_heimdall_partition_kernel=""
        deviceinfo_flash_heimdall_partition_rootfs=""
        """

    content_heimdall_isorec = """\
        deviceinfo_flash_heimdall_partition_kernel=""
        deviceinfo_flash_heimdall_partition_initfs=""
        deviceinfo_flash_heimdall_partition_rootfs=""
        """

    content_0xffff = """\
        deviceinfo_generate_legacy_uboot_initfs="true"
        """

    content_uuu = """\
        deviceinfo_generate_legacy_uboot_initfs="true"
        """

    match flash_method:
        case "fastboot":
            content += generate_deviceinfo_fastboot_content(bootimg)
        case "heimdall-bootimg":
            content += generate_deviceinfo_fastboot_content(bootimg)
            content += content_heimdall_bootimg
        case "heimdall-isorec":
            content += content_heimdall_isorec
        case "0xffff":
            content += content_0xffff
        case "uuu":
            content += content_uuu
        # TODO: Do other flash methods meed handling here? Many aren't even available in aportgen.
        case _:
            pass

    # Write to file
    work = get_context().config.work
    pmb.helpers.run.user(["mkdir", "-p", work / "aportgen"])
    path = work / "aportgen/deviceinfo"
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(line.lstrip() + "\n" for line in content.rstrip().split("\n"))


def generate_modules_initfs() -> None:
    content = """\
    # Remove this file if unnecessary (CHANGEME!)
    # This file shall contain a list of modules to be included in the initramfs,
    # so that they are available in early boot stages. In general, it should
    # include modules to support unlocking FDE (touchscreen, panel, etc),
    # USB networking, and telnet in the debug-shell.
    # The format is one module name per line. Lines starting with the character
    # '#', and empty lines are ignored. If there are multiple kernel variants
    # with different initramfs module requirements, one modules-initfs.$variant
    # file should be created for each of them.
    """

    # Write to file
    work = get_context().config.work
    pmb.helpers.run.user(["mkdir", "-p", work / "aportgen"])
    path = work / "aportgen/modules-initfs"
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(line.lstrip() + "\n" for line in content.rstrip().split("\n"))


def generate_apkbuild(
    pkgname: str,
    name: str,
    arch: Arch,
    flash_method: str,
    device_category: pmb.helpers.devices.DeviceCategory,
) -> None:
    # Dependencies
    if arch == Arch.armhf:
        # deviceinfo_alpine_only: no postmarketOS packages may be depended on
        depends = ["alpine-base", "mkinitfs", "linux-CHANGEME"]
    else:
        depends = ["postmarketos-base"]
        if device_category == pmb.helpers.devices.DeviceCategory.DOWNSTREAM:
            depends += [
                "linux-" + "-".join(pkgname.split("-")[1:]),
                "postmarketos-base-downstream",
            ]
        else:
            depends += ["linux-CHANGEME"]

    if flash_method in ["fastboot", "heimdall-bootimg"]:
        depends.append("android-tools-mkbootimg")
    if flash_method == "0xffff":
        depends.append("uboot-tools")

    # Whole APKBUILD
    depends.sort()
    depends_fmt = ("\n" + " " * 12).join(depends)
    content = f"""\
        # Reference: <https://postmarketos.org/devicepkg>
        maintainer=""
        pkgname={pkgname}
        pkgver=1
        pkgrel=0
        pkgdesc="{name}"
        url="https://postmarketos.org"
        arch="{arch}"
        license="MIT"
        depends="
            {depends_fmt}
            "
        makedepends="devicepkg-dev"
        source="
            deviceinfo
            modules-initfs
            "
        options="!archcheck !check"

        build() {{
            devicepkg_build $startdir $pkgname
        }}

        package() {{
            devicepkg_package $startdir $pkgname
        }}

        sha512sums="(run 'pmbootstrap checksum {pkgname}' to fill)"
        """

    # Write the file
    work = get_context().config.work
    pmb.helpers.run.user(["mkdir", "-p", work / "aportgen"])
    path = work / "aportgen/APKBUILD"
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(
            line[8:].replace(" " * 4, "\t") + "\n" for line in content.rstrip().split("\n")
        )


def generate(pkgname: str, device_category: pmb.helpers.devices.DeviceCategory) -> None:
    arch = ask_for_architecture()
    manufacturer = ask_for_manufacturer()
    name = ask_for_name(manufacturer)
    year = ask_for_year()
    chassis = ask_for_chassis()
    flash_method = ask_for_flash_method()
    bootimg = None
    if flash_method in ["fastboot", "heimdall-bootimg"]:
        bootimg = ask_for_bootimg()

    generate_deviceinfo(
        pkgname,
        name,
        manufacturer,
        year,
        arch,
        chassis,
        flash_method,
        device_category,
        bootimg,
    )
    generate_modules_initfs()
    generate_apkbuild(pkgname, name, arch, flash_method, device_category)
