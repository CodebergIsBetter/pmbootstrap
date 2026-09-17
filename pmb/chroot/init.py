# Copyright 2023 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
import filecmp
import os

import pmb.chroot
import pmb.config
import pmb.config.workdir
import pmb.helpers.apk
import pmb.helpers.apk_static
import pmb.helpers.repo
import pmb.helpers.run
from pmb.core import Chroot, ChrootType
from pmb.core.context import get_context
from pmb.helpers import logging
from pmb.meta import Cache
from pmb.parse.deviceinfo import device_is_alpine_only
from pmb.types import PathString


def copy_resolv_conf(chroot: Chroot) -> None:
    """
    Use pythons super fast file compare function (due to caching)
    and copy the /etc/resolv.conf to the chroot, in case it is
    different from the host.
    If the file doesn't exist, create an empty file with 'touch'.
    """
    host = "/etc/resolv.conf"
    resolv_path = chroot / host
    if os.path.exists(host):
        if not resolv_path.exists() or not filecmp.cmp(host, resolv_path):
            pmb.helpers.run.root(["cp", host, resolv_path])
    else:
        pmb.helpers.run.root(["touch", resolv_path])


def mark_in_chroot(chroot: Chroot = Chroot.native()) -> None:
    """
    Touch a flag so we can know when we're running in chroot (and
    don't accidentally flash partitions on our host). This marker
    gets removed in pmb.chroot.shutdown (pmbootstrap shutdown).
    """
    in_chroot_file = chroot / "in-pmbootstrap"
    if not in_chroot_file.exists():
        pmb.helpers.run.root(["touch", in_chroot_file])


def init_keys() -> None:
    """
    All Alpine and postmarketOS repository keys are shipped with pmbootstrap.
    Copy them into $WORK/config_apk_keys, which gets mounted inside the various
    chroots as /etc/apk/keys.

    This is done before installing any package, so apk can verify APKINDEX
    files of binary repositories even though alpine-keys/postmarketos-keys are
    not installed yet.
    """
    for key in pmb.config.apk_keys_path.glob("*.pub"):
        target = get_context().config.work / "config_apk_keys" / key.name
        if not target.exists():
            # Copy as root, so the resulting files in chroots are owned by root
            pmb.helpers.run.root(["cp", key, target])


@Cache()
def warn_if_chroots_outdated() -> None:
    outdated = pmb.config.workdir.chroots_outdated()
    if outdated:
        days_warn = int(pmb.config.chroot_outdated / 3600 / 24)
        msg = ""
        if Chroot.native() in outdated:
            msg += "your native"
            if Chroot.rootfs(get_context().config.device) in outdated:
                msg += " and rootfs chroots are"
            else:
                msg += " chroot is"
        elif Chroot.rootfs(get_context().config.device) in outdated:
            msg += "your rootfs chroot is"
        else:
            msg += "some of your chroots are"
        logging.warning(
            f"WARNING: {msg} older than {days_warn} days. Consider running 'pmbootstrap zap'."
        )


@Cache("chroot")
def init(chroot: Chroot) -> None:
    """
    Initialize a chroot by copying the resolv.conf and updating
    /etc/apk/repositories. If /bin/sh is missing, create the chroot from
    scratch.
    """
    # If the channel is wrong and the user has auto_zap_misconfigured_chroots
    # enabled, zap the chroot and reinitialize it
    if chroot.exists():
        zap = pmb.config.workdir.chroot_check_channel(chroot)
        if zap:
            pmb.chroot.del_chroot(chroot.path, confirm=False)
            pmb.config.workdir.clean()

    pmb.chroot.mount(chroot)
    mark_in_chroot(chroot)

    # Chroots that end up on the device must not see postmarketOS' binary
    # repository when the device is installed from Alpine only. Buildroots and
    # the native chroot are deliberately excluded: nothing from them is copied
    # into the image, and they still need pmaports to build the device package.
    alpine_only = chroot.type in [
        ChrootType.ROOTFS,
        ChrootType.INSTALLER,
        ChrootType.IMAGE,
    ] and device_is_alpine_only(chroot.name)

    # When already initialized: just prepare the chroot
    if chroot.exists():
        copy_resolv_conf(chroot)
        pmb.helpers.apk.update_repository_list(chroot.path, alpine_only=alpine_only)
        warn_if_chroots_outdated()
        return

    # Fetch apk.static
    pmb.helpers.apk_static.init()

    logging.info(f"({chroot}) Creating chroot")

    # Initialize /etc/apk/keys/, resolv.conf, repositories
    init_keys()
    copy_resolv_conf(chroot)
    pmb.helpers.apk.update_repository_list(chroot.path, alpine_only=alpine_only)

    pmb.config.workdir.chroot_save_init(chroot)

    pmb.helpers.repo.update(chroot.arch, alpine_only=alpine_only)
    # Create the /usr-merge-related symlinks, which needs to be done manually
    pmb.helpers.run.root(
        [
            "mkdir",
            "-p",
            f"{chroot.path}/usr/bin",
            f"{chroot.path}/usr/sbin",
            f"{chroot.path}/usr/lib",
        ]
    )
    pmb.helpers.run.root(["ln", "-s", "usr/bin", "usr/sbin", "usr/lib", f"{chroot.path}/"])
    # Create the bin-merge-related symlinks, which are done manually to be
    # consistent with the /usr-merge.
    #
    # Not for alpine_only devices: alpine-baselayout ships /bin, /sbin,
    # /usr/bin and /usr/sbin as four distinct real directories, and Alpine's
    # packages are built for that. Collapsing sbin into bin makes any package
    # that ships both bin/X and sbin/X destroy itself, because the two paths
    # become one and apk extracts the compat symlink over the real binary.
    # eudev is one such package: it ships bin/udevadm plus a compat
    # sbin/udevadm -> ../bin/udevadm, which under the bin-merge becomes a
    # symlink to itself, so udevadm fails with ELOOP and udev-trigger dies.
    # (This is why pmaports carries temp/eudev, "Forked from Alpine to remove
    # symlink in /sbin/udevadm".)
    if not alpine_only and pmb.config.pmaports.read_config().get("supported_bin_merge", False):
        pmb.helpers.run.root(
            [
                "rm",
                "-r",
                f"{chroot.path}/sbin",
                f"{chroot.path}/usr/sbin",
            ]
        )
        pmb.helpers.run.root(["ln", "-s", "usr/bin", f"{chroot.path}/sbin"])
        pmb.helpers.run.root(["ln", "-s", "bin", f"{chroot.path}/usr/sbin"])
    # Install minimal amount of things to get a functional chroot.
    # We don't use alpine-base since it depends on openrc, and neither
    # postmarketos-base, since that's quite big (e.g: contains an init system)
    pkgs = ["alpine-baselayout", "apk-tools", "busybox", "musl-utils"]
    cmd: list[PathString] = ["--initdb"]
    pmb.helpers.apk.run([*cmd, "add", *pkgs], chroot)

    # Building chroots: create "pmos" user, add symlinks to /home/pmos
    if chroot.type != ChrootType.ROOTFS:
        pmb.chroot.root(["adduser", "-D", "pmos", "-u", pmb.config.chroot_uid_user], chroot)

        # Create the links (with subfolders if necessary)
        for target, link_name in pmb.config.chroot_home_symlinks.items():
            link_dir = os.path.dirname(link_name)
            if not os.path.exists(chroot / link_dir):
                pmb.chroot.user(["mkdir", "-p", link_dir], chroot)
            if not os.path.exists(chroot / target):
                pmb.chroot.root(["mkdir", "-p", target], chroot)
            pmb.chroot.user(["ln", "-s", target, link_name], chroot)
            pmb.chroot.root(["chown", "pmos:pmos", target], chroot)
