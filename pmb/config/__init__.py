# Copyright 2023 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
from collections.abc import Sequence
from pathlib import Path

#
# Exported functions
#
# FIXME (#2324): this sucks, we should re-organise this and not rely on "lifting"
# this functions this way
from pmb.config.file import (
    load as load,
    save as save,
    serialize as serialize,
)
from pmb.config.other import is_systemd_selected as is_systemd_selected
from pmb.config.sudo import which_sudo
from pmb.types import PathString

from . import workdir as workdir
from .init import (
    ask_for_mainline_downstream as ask_for_mainline_downstream,
    require_programs as require_programs,
)

#
# Exported variables (internal configuration)
#
pmb_src: Path = Path(Path(__file__) / "../../..").resolve()
apk_keys_path: Path = pmb_src / "pmb/data/keys"

# apk-tools minimum version
# https://pkgs.alpinelinux.org/packages?name=apk-tools&branch=edge
# Update this frequently to prevent a MITM attack with an outdated version
# (which may contain a vulnerable apk/openssl, and allows an attacker to
# exploit the system!)
apk_tools_min_version = {
    "edge": "3.0.6-r0",
    "v3.24": "3.0.6-r0",
    "v3.23": "3.0.6-r0",
    "v3.22": "2.14.9-r3",
    "v3.21": "2.14.6-r3",
    "v3.20": "2.14.4-r1",
    "v3.19": "2.14.4-r0",
    "v3.18": "2.14.4-r0",
    "v3.17": "2.12.14-r0",
    "v3.16": "2.12.9-r3",
    "v3.15": "2.12.7-r3",
    "v3.14": "2.12.7-r0",
    "v3.13": "2.12.7-r0",
    "v3.12": "2.10.8-r1",
}

# postmarketOS aports compatibility (checked against "version" in pmaports.cfg)
pmaports_min_version = "7"

# Version of the work folder (as asked during 'pmbootstrap init'). Increase
# this number, whenever migration is required and provide the migration code,
# see migrate_work_folder()).
work_version = 8

# Programs that pmbootstrap expects to be available from the host system. Keep
# in sync with README.md, and try to keep the list as small as possible. The
# idea is to run almost everything in Alpine chroots.
required_programs: dict[str, str] = {
    "git": "",
    "kpartx": "",
    "losetup": "",
    "openssl": "",
    "ps": "",
    "tar": "",
    "chroot": "",
    "sh": "",
}


def sudo(cmd: Sequence[PathString]) -> Sequence[PathString]:
    """Adapt a command to run as root."""
    sudo = which_sudo()
    if sudo:
        return [sudo, *cmd]
    else:
        return cmd


defaults: dict[str, PathString] = {
    "cipher": "aes-xts-plain64",
    "config": Path(
        (os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))
        + "/pmbootstrap_v3.cfg"
    ),
    # A higher value is typically desired, but this can lead to VERY long open
    # times on slower devices due to host systems being MUCH faster than the
    # target device (see issue #429).
    "iter_time": "200",
    "ui": "console",
}

# Whether we're connected to a TTY (which allows things like e.g. printing
# progress bars)
is_interactive = sys.stdout.isatty() and sys.stderr.isatty() and sys.stdin.isatty()


# ANSI escape codes to highlight stdout
styles = {
    "BLUE": "\033[94m",
    "BOLD": "\033[1m",
    "CYAN": "\033[96m",
    "GREEN": "\033[92m",
    "MAGENTA": "\033[95m",
    "RED": "\033[91m",
    "YELLOW": "\033[93m",
    "END": "\033[0m",
}

if "NO_COLOR" in os.environ:
    for style in styles:
        styles[style] = ""

# Supported filesystems and their fstools packages
filesystems = {
    "btrfs": "btrfs-progs",
    "ext2": "e2fsprogs",
    "ext4": "e2fsprogs",
    "f2fs": "f2fs-tools",
    "fat16": "dosfstools",
    "fat32": "dosfstools",
    "xfs": "xfsprogs",
}

#
# CHROOT
#

# Usually the ID for the first user created is 1000. However, we want
# pmbootstrap to work even if the 'user' account inside the chroots has
# another UID, so we force it to be different.
chroot_uid_user = "12345"

# The PATH variable used inside all chroots
chroot_path = ":".join(
    [
        "/usr/lib/ccache/bin",
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin:/usr/bin",
        "/sbin",
        "/bin",
    ]
)

# The PATH variable used on the host, to find the "chroot" and "sh"
# executables. As pmbootstrap runs as user, not as root, the location
# for the chroot executable may not be in the PATH (Debian).
host_path = os.environ["PATH"] + ":/usr/sbin/"

# Folders that get mounted inside the chroot
# $WORK gets replaced with get_context().config.work
# $ARCH gets replaced with the chroot architecture (eg. x86_64, armhf)
# $CHANNEL gets replaced with the release channel (e.g. edge, v21.03)
# Use no more than one dir after /mnt/pmbootstrap, see remove_mnt_pmbootstrap.
chroot_mount_bind = {
    "/proc": "/proc",
    "/sys": "/sys",
    "$WORK/cache_apk_$ARCH": "/var/cache/apk",
    "$WORK/cache_appstream/$ARCH/$CHANNEL": "/mnt/appstream-data",
    "$WORK/cache_ccache_$ARCH": "/mnt/pmbootstrap/ccache",
    "$WORK/cache_distfiles": "/var/cache/distfiles",
    "$WORK/cache_git": "/mnt/pmbootstrap/git",
    "$WORK/cache_go": "/mnt/pmbootstrap/go",
    "$WORK/cache_rust": "/mnt/pmbootstrap/rust",
    "$WORK/config_abuild": "/mnt/pmbootstrap/abuild-config",
    "$WORK/config_apk_keys": "/etc/apk/keys",
    "$WORK/cache_sccache": "/mnt/pmbootstrap/sccache",
    "$WORK/images_netboot": "/mnt/pmbootstrap/netboot",
    "$WORK/packages/": "/mnt/pmbootstrap/packages",
}

# Building chroots (all chroots, except for the rootfs_ chroot) get symlinks in
# the "pmos" user's home folder pointing to mountfolders from above.
# Rust packaging is new and still a bit weird in Alpine and postmarketOS. As of
# writing, we only have one package (squeekboard), and use cargo to download
# the source of all dependencies at build time and compile it. Usually, this is
# a no-go, but at least until this is resolved properly, let's cache the
# dependencies and downloads as suggested in "Caching the Cargo home in CI":
# https://doc.rust-lang.org/cargo/guide/cargo-home.html
# Go: cache the directories "go env GOMODCACHE" and "go env GOCACHE" point to,
# to avoid downloading dependencies over and over (GOMODCACHE, similar to the
# rust depends caching described above) and to cache build artifacts (GOCACHE,
# similar to ccache).
chroot_home_symlinks = {
    "/mnt/pmbootstrap/abuild-config": "/home/pmos/.abuild",
    "/mnt/pmbootstrap/ccache": "/home/pmos/.ccache",
    "/mnt/pmbootstrap/go/gocache": "/home/pmos/.cache/go-build",
    "/mnt/pmbootstrap/go/gomodcache": "/home/pmos/go/pkg/mod",
    # "/mnt/pmbootstrap/packages": "/home/pmos/packages/pmos",
    "/mnt/pmbootstrap/rust/git/db": "/home/pmos/.cargo/git/db",
    "/mnt/pmbootstrap/rust/registry/cache": "/home/pmos/.cargo/registry/cache",
    "/mnt/pmbootstrap/rust/registry/index": "/home/pmos/.cargo/registry/index",
    "/mnt/pmbootstrap/sccache": "/home/pmos/.cache/sccache",
}

# Device nodes to be created in each chroot. Syntax for each entry:
# [permissions, type, major, minor, name]
chroot_device_nodes = [
    [666, "c", 1, 3, "null"],
    [666, "c", 1, 5, "zero"],
    [666, "c", 1, 7, "full"],
    [666, "c", 1, 8, "random"],
    [666, "c", 1, 9, "urandom"],
    [666, "c", 5, 0, "tty"],
    [666, "c", 5, 2, "ptmx"],
]

# Age in hours that we keep the APKINDEXes before downloading them again.
# You can force-update them with 'pmbootstrap update'.
apkindex_retention_time = 4


# When chroot is considered outdated (in seconds)
chroot_outdated = 3600 * 24 * 2

# Packages that will be installed in a chroot before it builds packages
# for the first time
# IMPORTANT: the order here matters, it is the order these packages will
# be built in (if needed). abuild must be first!
build_packages = ["abuild", "apk-tools", "build-base", "ccache", "git", "pigz"]

#
# PARSE
#

# Variables belonging to a package or subpackage in APKBUILD files
apkbuild_package_attributes = {
    "pkgdesc": {},
    "depends": {"array": True},
    "provides": {"array": True},
    "provider_priority": {"int": True},
    "install": {"array": True},
    "triggers": {"array": True},
    # Packages can specify soft dependencies in "_pmb_recommends" to be
    # explicitly installed by default, and not implicitly as a hard dependency
    # of the package ("depends"). This makes these apps uninstallable, without
    # removing the meta-package. (#1933). To disable this feature, use:
    # "pmbootstrap install --no-recommends".
    "_pmb_recommends": {"array": True},
    # UI meta-packages can specify groups to which the user must be added
    # to access specific hardware such as LED indicators.
    "_pmb_groups": {"array": True},
    # postmarketos-base, UI and device packages can use _pmb_select to provide
    # additional configuration options in "pmbootstrap init" that allow
    # selecting alternative providers for a virtual APK package.
    "_pmb_select": {"array": True},
    # postmarketos-base and UI meta-packages can define the default package
    # to select during "_pmb_select".
    "_pmb_default": {"array": True},
    # kernel packages can specify one or more defconfig to use when generating
    # the full config
    "_defconfig": {"array": True},
}

# Variables in APKBUILD files that get parsed
apkbuild_attributes = {
    **apkbuild_package_attributes,
    "maintainer": {},
    "arch": {"array": True},
    "depends_dev": {"array": True},
    "makedepends": {"array": True},
    "checkdepends": {"array": True},
    "options": {"array": True},
    "triggers": {"array": True},
    "pkgname": {},
    "pkgrel": {},
    "pkgver": {},
    "sha512sums": {},
    "subpackages": {},
    "url": {},
    # cross-compilers
    "makedepends_build": {"array": True},
    "makedepends_host": {"array": True},
    # kernels
    "_flavor": {},
    "_device": {},
    "_kernver": {},
    "_outdir": {},
    "_config": {},
    # linux-edge
    "_depends_dev": {"array": True},
    # mesa
    "_llvmver": {},
    # Overridden packages
    "_pkgver": {},
    "_pkgrel": {},
    "_pkgname": {},
    # git commit
    "_commit": {},
    "source": {"array": True},
    # gcc
    "_pkgbase": {},
    "_pkgsnap": {},
}

# Valid types for the 'chassis' attribute in deviceinfo
# See https://www.freedesktop.org/software/systemd/man/machine-info.html
deviceinfo_chassis_types = [
    "desktop",
    "laptop",
    "convertible",
    "server",
    "tablet",
    "handset",
    "watch",
    "embedded",
    "vm",
]

#
# INITFS
#
initfs_hook_prefix = "postmarketos-mkinitfs-hook-"
default_ip = "172.16.42.1"


#
# INSTALL
#

# Packages that will be installed inside the native chroot to perform
# the installation to the device.
# util-linux: losetup, fallocate
install_native_packages = ["cryptsetup", "util-linux", "parted"]
install_device_packages = ["postmarketos-base"]

# Same, for devices that set deviceinfo_alpine_only: no postmarketOS packages,
# just Alpine's base system. The device package pulls in everything else.
install_device_packages_alpine_only = ["alpine-base"]

#
# FLASH
#

flash_methods = [
    "0xffff",
    "fastboot",
    "heimdall",
    "mtkclient",
    "none",
    "rkdeveloptool",
    "uuu",
]

# These folders will be mounted at the same location into the native
# chroot, before the flash programs get started.
flash_mount_bind = [
    Path("/sys/bus/usb/devices/"),
    Path("/sys/dev/"),
    Path("/sys/devices/"),
    Path("/dev/bus/usb/"),
]

"""
Flasher abstraction. Allowed variables:

$BOOT: Path to the /boot partition
$DTB: Name of device dtb without .dtb extension
$FLAVOR: Backwards compatibility with old mkinitfs (pma#660)
$IMAGE: Path to the combined boot/rootfs image
$IMAGE_SPLIT_BOOT: Path to the (split) boot image
$IMAGE_SPLIT_ROOT: Path to the (split) rootfs image
$PARTITION_KERNEL: Partition to flash the kernel/boot.img to
$PARTITION_VENDOR_BOOT: Partition to flash the vendor_boot.img to
$PARTITION_ROOTFS: Partition to flash the rootfs to

Fastboot specific: $KERNEL_CMDLINE
Heimdall specific: $PARTITION_INITFS
uuu specific: $UUU_SCRIPT
"""
flashers: dict[str, dict[str, bool | list[str] | dict[str, list[list[str]]]]] = {
    "fastboot": {
        "depends": [],  # pmaports.cfg: supported_fastboot_depends
        "actions": {
            "list_devices": [["fastboot", "devices", "-l"]],
            "flash_rootfs": [["fastboot", "flash", "$PARTITION_ROOTFS", "$IMAGE"]],
            "flash_kernel": [["fastboot", "flash", "$PARTITION_KERNEL", "$BOOT/boot.img$FLAVOR"]],
            "flash_vendorboot": [
                ["fastboot", "flash", "$PARTITION_VENDOR_BOOT", "$BOOT/vendor_boot.img"]
            ],
            "flash_vbmeta": [
                # Generate vbmeta image with "disable verification" flag
                [
                    "avbtool",
                    "make_vbmeta_image",
                    "--flags",
                    "2",
                    "--padding_size",
                    "$FLASH_PAGESIZE",
                    "--output",
                    "/vbmeta.img",
                ],
                ["fastboot", "flash", "$PARTITION_VBMETA", "/vbmeta.img"],
                ["rm", "-f", "/vbmeta.img"],
            ],
            "flash_dtbo": [["fastboot", "flash", "$PARTITION_DTBO", "$BOOT/dtbo.img"]],
            "boot": [["fastboot", "--cmdline", "$KERNEL_CMDLINE", "boot", "$BOOT/boot.img$FLAVOR"]],
            # Like boot, but stage vendor_boot first
            "boot_gki": [
                ["fastboot", "stage", "$BOOT/vendor_boot.img"],
                ["fastboot", "boot", "$BOOT/boot.img$FLAVOR"],
            ],
            "flash_lk2nd": [["fastboot", "flash", "$PARTITION_KERNEL", "$BOOT/lk2nd.img"]],
        },
    },
    # Some devices provide Fastboot but using Android boot images is not
    # practical for them (e.g. because they support booting from FAT32
    # partitions directly and/or the Android boot partition is too small).
    # This can be implemented using --split (separate image files for boot and
    # rootfs).
    # This flasher allows flashing the split image files using Fastboot.
    "fastboot-bootpart": {
        "split": True,
        "depends": ["android-tools-fastboot"],
        "actions": {
            "list_devices": [["fastboot", "devices", "-l"]],
            "flash_rootfs": [["fastboot", "flash", "$PARTITION_ROOTFS", "$IMAGE_SPLIT_ROOT"]],
            "flash_kernel": [["fastboot", "flash", "$PARTITION_KERNEL", "$IMAGE_SPLIT_BOOT"]],
            "flash_boot": [["fastboot", "flash", "boot", "$BOOT/boot.img$FLAVOR"]],
            "boot": [["fastboot", "--cmdline", "$KERNEL_CMDLINE", "boot", "$BOOT/boot.img$FLAVOR"]],
        },
    },
    # Some Samsung devices need the initramfs to be baked into the kernel (e.g.
    # i9070, i9100). We want the initramfs to be generated after the kernel was
    # built, so we put the real initramfs on another partition (e.g. RECOVERY)
    # and load it from the initramfs in the kernel. This method is called
    # "isorec" (isolated recovery), a term coined by Lanchon.
    "heimdall-isorec": {
        "depends": ["heimdall"],
        "actions": {
            "list_devices": [["heimdall", "detect"]],
            "flash_rootfs": [
                ["heimdall", "flash", "--wait", "--$PARTITION_ROOTFS", "$IMAGE"],
            ],
            "flash_kernel": [
                [
                    "heimdall_flash_isorec_kernel.sh",
                    "$BOOT/initramfs$FLAVOR",
                    "$PARTITION_INITFS",
                    "$BOOT/vmlinuz$FLAVOR",
                    "$PARTITION_KERNEL",
                    "$BOOT/$DTB",
                ]
            ],
        },
    },
    # Some Samsung devices need a 'boot.img' file, just like the one generated
    # fastboot compatible devices. Example: s7562, n7100
    "heimdall-bootimg": {
        "depends": [],  # pmaports.cfg: supported_heimdall_depends
        "actions": {
            "list_devices": [["heimdall", "detect"]],
            "flash_rootfs": [
                [
                    "heimdall",
                    "flash",
                    "--wait",
                    "--$PARTITION_ROOTFS",
                    "$IMAGE",
                    "$NO_REBOOT",
                    "$RESUME",
                ],
            ],
            "flash_kernel": [
                [
                    "heimdall",
                    "flash",
                    "--wait",
                    "--$PARTITION_KERNEL",
                    "$BOOT/boot.img$FLAVOR",
                    "$NO_REBOOT",
                    "$RESUME",
                ],
            ],
            "flash_vbmeta": [
                [
                    "avbtool",
                    "make_vbmeta_image",
                    "--flags",
                    "2",
                    "--padding_size",
                    "$FLASH_PAGESIZE",
                    "--output",
                    "/vbmeta.img",
                ],
                [
                    "heimdall",
                    "flash",
                    "--$PARTITION_VBMETA",
                    "/vbmeta.img",
                    "$NO_REBOOT",
                    "$RESUME",
                ],
                ["rm", "-f", "/vbmeta.img"],
            ],
            "flash_lk2nd": [
                [
                    "heimdall",
                    "flash",
                    "--wait",
                    "--$PARTITION_KERNEL",
                    "$BOOT/lk2nd.img",
                    "$NO_REBOOT",
                    "$RESUME",
                ],
            ],
        },
    },
    "adb": {
        "depends": ["android-tools-adb"],
        "actions": {
            "list_devices": [["adb", "-P", "5038", "devices"]],
            "sideload": [
                ["echo", "< wait for any device >"],
                ["adb", "-P", "5038", "wait-for-usb-sideload"],
                ["adb", "-P", "5038", "sideload", "$RECOVERY_ZIP"],
            ],
        },
    },
    "uuu": {
        "depends": ["nxp-mfgtools-uuu"],
        "actions": {
            "flash_rootfs": [
                # There's a bug(?) in uuu where it clobbers the path in the cmd
                # script if the script is not in pwd...
                ["cp", "$UUU_SCRIPT", "./flash_script.lst"],
                ["uuu", "flash_script.lst"],
            ],
        },
    },
    "rkdeveloptool": {
        "split": True,
        "depends": ["rkdeveloptool"],
        "actions": {
            "list_devices": [["rkdeveloptool", "list"]],
            "flash_rootfs": [
                ["rkdeveloptool", "write-partition", "$PARTITION_ROOTFS", "$IMAGE_SPLIT_ROOT"]
            ],
            "flash_kernel": [
                ["rkdeveloptool", "write-partition", "$PARTITION_KERNEL", "$IMAGE_SPLIT_BOOT"]
            ],
        },
    },
    "mtkclient": {
        "depends": ["mtkclient"],
        "actions": {
            "flash_rootfs": [["mtk", "w", "$PARTITION_ROOTFS", "$IMAGE"]],
            "flash_kernel": [["mtk", "w", "$PARTITION_KERNEL", "$BOOT/boot.img$FLAVOR"]],
            "flash_vbmeta": [
                # Generate vbmeta image with "disable verification" flag
                [
                    "avbtool",
                    "make_vbmeta_image",
                    "--flags",
                    "2",
                    "--padding_size",
                    "$FLASH_PAGESIZE",
                    "--output",
                    "/vbmeta.img",
                ],
                ["mtk", "w", "$PARTITION_VBMETA", "/vbmeta.img"],
                ["rm", "-f", "/vbmeta.img"],
            ],
            "flash_dtbo": [["mtk", "w", "$PARTITION_DTBO", "$BOOT/dtbo.img"]],
            "flash_lk2nd": [["mtk", "w", "$PARTITION_KERNEL", "$BOOT/lk2nd.img"]],
        },
    },
}

#
# GIT
#
git_repos = {
    "aports_upstream": [
        "https://gitlab.alpinelinux.org/alpine/aports.git",
        "git@gitlab.alpinelinux.org:alpine/aports.git",
    ],
    "pmaports": [
        "https://gitlab.postmarketos.org/postmarketOS/pmaports.git",
        "git@gitlab.postmarketos.org:postmarketOS/pmaports.git",
    ],
}

#
# APORTGEN
#

# Use a deterministic mirror URL instead of CDN for aportgen. Otherwise we may
# generate a pmaport that wraps an apk from Alpine (e.g. musl-armv7) locally
# with one up-to-date mirror given by the CDN. But then the build will fail if
# CDN picks an outdated mirror for CI or BPO.
aportgen_mirror_alpine = "http://dl-4.alpinelinux.org/alpine/"

#
# NEWAPKBUILD
# Options passed through to the "newapkbuild" command from Alpine Linux. They
# are duplicated here, so we can use Python's argparse for argument parsing and
# help page display. The -f (force) flag is not defined here, as we use that in
# the Python code only and don't pass it through.
#
newapkbuild_arguments_strings = [
    ["-n", "pkgname", "set package name (only use with SRCURL)"],
    ["-d", "pkgdesc", "set package description"],
    ["-l", "license", "set package license identifier from <https://spdx.org/licenses/>"],
    ["-u", "url", "set package URL"],
]
newapkbuild_arguments_switches_pkgtypes = [
    ["-a", "autotools", "create autotools package (use ./configure ...)"],
    ["-C", "cmake", "create CMake package (assume cmake/ is there)"],
    ["-m", "meson", "create meson package (assume meson.build is there)"],
    ["-p", "perl", "create perl package (assume Makefile.PL is there)"],
    ["-y", "python", "create python package (assume setup.py is there)"],
    ["-e", "python_gpep517", "create python package (assume pyproject.toml is there)"],
    ["-r", "rust", "create rust package (assume Cargo.toml is there)"],
]
newapkbuild_arguments_switches_other = [
    ["-s", "sourceforge", "use sourceforge source URL"],
    ["-c", "copy_samples", "copy a sample init.d, conf.d and install script"],
]

#
# CI
#
# Valid options for 'pmbootstrap ci', see https://postmarketos.org/pmb-ci
ci_valid_options = ["native", "slow", "with-dot-git"]
