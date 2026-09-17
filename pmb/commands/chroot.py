# Copyright 2026 Oliver Smith, Paul Adam
# SPDX-License-Identifier: GPL-3.0-or-later
import os

import pmb.chroot
import pmb.chroot.apk
import pmb.chroot.other
import pmb.helpers.apk
import pmb.helpers.mount
import pmb.install.blockdevice
from pmb.core import Chroot, ChrootType
from pmb.helpers import logging
from pmb.helpers.exceptions import CommandFailedError, NonBugError
from pmb.parse.deviceinfo import device_is_alpine_only
from pmb.types import Env, RunOutputTypeDefault


def chroot(
    add: str,
    chroot: Chroot,
    chroot_usb: bool,
    command: str,
    install_blockdev: bool,
    output: RunOutputTypeDefault,
    sector_size: int | None,
    user: str,
    xauth: bool,
) -> None:
    if (
        user
        and chroot != Chroot.native()
        and chroot.type not in [ChrootType.BUILDROOT, ChrootType.IMAGE]
    ):
        raise RuntimeError("--user is only supported for native or buildroot_* chroots.")
    if xauth and chroot != Chroot.native():
        raise RuntimeError("--xauth is only supported for native chroot.")

    if chroot.type == ChrootType.IMAGE:
        pmb.chroot.mount(chroot)

    # apk: check minimum version, install packages
    pmb.chroot.apk.check_min_version(chroot)
    if add:
        pmb.chroot.apk.install(add.split(","), chroot)

    pmb.chroot.init(chroot)

    # Xauthority
    env: Env = {}
    if xauth:
        pmb.chroot.other.copy_xauthority(chroot)
        x11_display = os.environ.get("DISPLAY")
        if x11_display is None:
            raise AssertionError("$DISPLAY was unset despite that it should be set at this point")
        env["DISPLAY"] = x11_display
        env["XAUTHORITY"] = "/home/pmos/.Xauthority"

    # Install blockdevice
    if install_blockdev:
        logging.warning(
            "--install-blockdev is deprecated for the chroot command"
            " and will be removed in a future release. If you need this"
            " for some reason, please open an issue on"
            " https://gitlab.postmarketos.org/postmarketOS/pmbootstrap.git"
        )
        size_boot = 128  # 128 MiB
        size_root = 4096  # 4 GiB
        pmb.install.blockdevice.create_and_mount_image(sector_size, size_boot, size_root)

    # Bind mount in sysfs dirs to accessing USB devices (e.g. for running fastboot)
    if chroot_usb:
        for folder in pmb.config.flash_mount_bind:
            pmb.helpers.mount.bind(folder, Chroot.native() / folder)

    alpine_only = chroot.type in [
        ChrootType.ROOTFS,
        ChrootType.INSTALLER,
        ChrootType.IMAGE,
    ] and device_is_alpine_only(chroot.name)
    pmb.helpers.apk.update_repository_list(
        chroot.path, user_repository=True, alpine_only=alpine_only
    )

    try:
        # Run the command as user/root
        if user:
            logging.info(f"({chroot}) % su pmos -c '" + " ".join(command) + "'")
            pmb.chroot.user(command, chroot, output=output, env=env)
        else:
            logging.info(f"({chroot}) % " + " ".join(command))
            pmb.chroot.root(command, chroot, output=output, env=env)
    except CommandFailedError as exception:
        raise NonBugError(exception) from exception
