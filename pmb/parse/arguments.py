# Copyright 2023 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path
from types import GenericAlias
from typing import Any

from pmb.core import Config
from pmb.core.apkindex import Apkindex
from pmb.core.arch import Arch
from pmb.helpers.exceptions import NonBugError
from pmb.types import PmbArgs, RunOutputTypeDefault

with contextlib.suppress(ImportError):
    import argcomplete

import pmb.config
import pmb.helpers.args
import pmb.helpers.pmaports

"""This file is about parsing command line arguments passed to pmbootstrap, as
   well as generating the help pages (pmbootstrap -h). All this is done with
   Python's argparse. The parsed arguments get extended and finally stored in
   the "args" variable, which is prominently passed to most functions all
   over the pmbootstrap code base.

   See pmb/helpers/args.py for more information about the args variable.
"""


# Ugly hack from https://github.com/python/typeshed/issues/7539#issuecomment-1076640854
argparse._SubParsersAction.__class_getitem__ = classmethod(GenericAlias)  # type: ignore[attr-defined]


def toggle_other_boolean_flags(
    *other_destinations: str, value: bool = True
) -> type[argparse.Action]:
    """
    Group several argparse flags to one.
    Sets multiple other_destination to value.

    :param other_destinations: 'the other argument names' str
    :param value: 'the value to set the other_destinations to' bool
    :returns: custom Action
    """

    class SetOtherDestinationsAction(argparse.Action):
        def __init__(self, option_strings: list[str], dest: str, **kwargs: Any) -> None:
            super().__init__(option_strings, dest, nargs=0, const=value, default=value, **kwargs)

        def __call__(
            self,
            _parser: argparse.ArgumentParser,
            namespace: argparse.Namespace,
            _values: str | Sequence[Any] | None,
            _option_string: str | None = None,
        ) -> None:
            for destination in other_destinations:
                setattr(namespace, destination, value)

    return SetOtherDestinationsAction


def arguments_install(subparser: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    ret = subparser.add_parser(
        "install", help="set up device specific chroot and install to SD card or image file"
    )

    # Other arguments (that don't fit categories below)
    ret.add_argument(
        "--no-sshd", action="store_true", help="do not enable the SSH daemon by default"
    )
    ret.add_argument(
        "--no-firewall", action="store_true", help="do not enable the firewall by default"
    )
    ret.add_argument(
        "--wifi-ssid",
        help="connect to this Wi-Fi network on first boot, for headless setups."
        " Only for devices with deviceinfo_alpine_only",
    )
    ret.add_argument(
        "--wifi-country",
        default="US",
        help="ISO 3166-1 alpha-2 country code for the Wi-Fi regulatory domain (default: US)",
    )
    ret.add_argument(
        "--wifi-psk",
        help="passphrase (8-63 characters) or 64 character hex PSK for --wifi-ssid."
        " Leave out for an open network",
    )
    ret.add_argument(
        "--password",
        help="dummy password for automating the"
        " installation - will be handled in PLAIN TEXT during"
        " install and may be logged to the logfile, do not use an"
        " important password!",
    )
    ret.add_argument(
        "--no-cgpt",
        help="do not use cgpt partition table",
        dest="install_cgpt",
        action="store_false",
        default=True,
    )
    ret.add_argument("--zap", help="zap chroots before installing", action="store_true")
    ret.add_argument(
        "--sector-size",
        help="set the sector size for the image file",
        type=int,
        default=None,
        choices=[512, 2048, 4096],
    )

    ret.add_argument(
        "--single-partition",
        action="store_true",
        help="Create a single partition that contains both boot and root files."
        " This can be used on devices that boot without mounting the boot file"
        " system (e.g. Android boot images or fastboot).",
    )

    # Image type
    group_desc = ret.add_argument_group(
        "optional image type",
        "Format of the resulting image. Default is generating a combined image"
        " of the postmarketOS boot and root partitions (--no-split). (If the"
        " device's deviceinfo_flash_method requires separate boot and root"
        " partitions, then --split is the default.) Related:"
        " https://postmarketos.org/partitions",
    )
    group: argparse._ActionsContainer = group_desc.add_mutually_exclusive_group()
    group.add_argument(
        "--no-split",
        help="create combined boot and root image file",
        dest="split",
        action="store_false",
        default=None,
    )
    group.add_argument(
        "--split", help="create separate boot and root image files", action="store_true"
    )
    group.add_argument(
        "--disk",
        "--sdcard",
        help="do not create an image file, instead"
        " write to the given block device (SD card, USB"
        " stick, etc.), for example: '/dev/mmcblk0'",
        metavar="BLOCKDEV",
        type=Path,
    )
    group.add_argument(
        "--android-recovery-zip",
        help="generate TWRP flashable zip (recommended read: https://postmarketos.org/recoveryzip)",
        action="store_true",
        dest="android_recovery_zip",
    )
    group.add_argument(
        "--no-image", help="do not generate an image", action="store_true", dest="no_image"
    )

    # Image type "--disk" related
    group = ret.add_argument_group("optional image type 'disk' arguments")
    group.add_argument("--rsync", help="update the disk using rsync", action="store_true")

    # Image type "--android-recovery-zip" related
    group = ret.add_argument_group("optional image type 'android-recovery-zip' arguments")
    group.add_argument(
        "--recovery-install-partition",
        default="system",
        help="partition to flash from recovery (e.g. 'external_sd')",
        dest="recovery_install_partition",
    )
    group.add_argument(
        "--recovery-no-kernel",
        help="do not overwrite the existing kernel",
        action="store_false",
        dest="recovery_flash_kernel",
    )

    # Full disk encryption (disabled by default, --no-fde has no effect)
    group = ret.add_argument_group("optional full disk encryption arguments")
    group.add_argument(
        "--fde", help="use full disk encryption", action="store_true", dest="full_disk_encryption"
    )
    group.add_argument("--no-fde", help=argparse.SUPPRESS, action="store_true", dest="no_fde")
    group.add_argument(
        "--cipher",
        help="cryptsetup cipher used to encrypt the the rootfs (e.g. 'aes-xts-plain64')",
        default=pmb.config.defaults["cipher"],
    )
    group.add_argument(
        "--iter-time",
        help="cryptsetup iteration time (in"
        " milliseconds) to use when encrypting the system"
        " partition",
        default=pmb.config.defaults["iter_time"],
    )

    # Packages
    group = ret.add_argument_group(
        "optional packages arguments",
        "Select or deselect packages to be included in the installation.",
    )
    group.add_argument(
        "--add",
        help="comma separated list of packages to be added to the rootfs (e.g. 'vim,gcc')",
        metavar="PACKAGES",
    )
    group.add_argument(
        "--no-base",
        help="do not install postmarketos-base (advanced)",
        action="store_false",
        dest="install_base",
    )
    group.add_argument(
        "--no-recommends",
        dest="install_recommends",
        help="do not install packages listed in _pmb_recommends of the UI pmaports",
        action="store_false",
    )

    # Sparse image
    group_desc = ret.add_argument_group(
        "optional sparse image arguments", "Override deviceinfo_flash_sparse for testing purpose."
    )
    group = group_desc.add_mutually_exclusive_group()
    group.add_argument(
        "--sparse", help="generate sparse image file", default=None, action="store_true"
    )
    group.add_argument(
        "--no-sparse", help="do not generate sparse image file", dest="sparse", action="store_false"
    )

    # Other
    group = ret.add_argument_group("other optional arguments")
    group.add_argument(
        "--no-local-pkgs",
        dest="install_local_pkgs",
        help="do not install locally compiled packages and package signing keys",
        action="store_false",
    )
    group.add_argument(
        "--filesystem", help="root filesystem type", choices=["ext4", "f2fs", "btrfs", "xfs"]
    )


def arguments_export(
    subparser: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    ret = subparser.add_parser(
        "export",
        help="create convenience symlinks"
        " to generated image files (system, kernel,"
        " initramfs, boot.img, ...)",
    )

    ret.add_argument(
        "export_folder",
        help="export folder, defaults to /tmp/postmarketOS-export",
        default=Path("/tmp/postmarketOS-export"),
        nargs="?",
        type=Path,
    )
    ret.add_argument(
        "--odin",
        help="odin flashable tar (boot.img/kernel+initramfs only)",
        action="store_true",
        dest="odin_flashable_tar",
    )
    ret.add_argument(
        "--no-install",
        dest="autoinstall",
        default=True,
        help="skip updating kernel/initfs",
        action="store_false",
    )
    return ret


def arguments_sideload(
    subparser: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    ret = subparser.add_parser(
        "sideload", help="Push packages to a running phone connected over usb or wifi"
    )
    add_packages_arg(ret, nargs="+")
    ret.add_argument(
        "--host",
        help="ip of the device over wifi (defaults to 172.16.42.1)",
        default="172.16.42.1",
    )
    ret.add_argument(
        "--port", help="SSH port of the device over wifi (defaults to 22)", type=int, default=22
    )
    ret.add_argument("--user", help="use a different username than the one set in init")
    ret.add_argument(
        "--arch",
        choices=Arch.supported(),
        help="skip automatic architecture deduction and use the given value",
        type=Arch.from_str,
    )
    ret.add_argument(
        "--install-key",
        help="install the apk key from this machine if needed",
        action="store_true",
        dest="install_key",
    )
    ret.add_argument(
        "--allow-untrusted",
        help="allow installing packages with untrusted signature or no signature",
        action="store_true",
        dest="allow_untrusted",
    )
    return ret


def arguments_flasher(
    subparser: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    ret = subparser.add_parser("flasher", help="flash something to the target device")
    ret.add_argument("--method", help="override flash method", dest="flash_method", default=None)
    sub = ret.add_subparsers(dest="action_flasher")
    sub.required = True

    # Boot, flash kernel
    boot = sub.add_parser("boot", help="boot a kernel once")
    boot.add_argument("--cmdline", help="override kernel commandline")
    flash_kernel = sub.add_parser("flash_kernel", help="flash a kernel")
    for action in [boot, flash_kernel]:
        action.add_argument(
            "--no-install",
            dest="autoinstall",
            default=True,
            help="skip updating kernel/initfs",
            action="store_false",
        )
    flash_kernel.add_argument(
        "--partition",
        default=None,
        help="partition to flash the kernel to (defaults to deviceinfo_flash_*_partition_kernel)",
    )

    # Flash lk2nd
    flash_lk2nd = sub.add_parser(
        "flash_lk2nd",
        help="flash lk2nd, a secondary bootloader needed for various Android devices",
    )
    flash_lk2nd.add_argument(
        "--partition",
        default=None,
        help="partition to flash lk2nd to (defaults to default boot image partition ",
    )

    # Flash rootfs
    flash_rootfs = sub.add_parser(
        "flash_rootfs",
        help="flash the rootfs to a partition on the"
        " device (partition layout does not get"
        " changed)",
    )
    flash_rootfs.add_argument(
        "--partition",
        default=None,
        help="partition to flash the rootfs to (defaults"
        " to deviceinfo_flash_*_partition_rootfs,"
        " 'userdata' on Android may have more"
        " space)",
    )

    # Flash vbmeta
    flash_vbmeta = sub.add_parser(
        "flash_vbmeta",
        help="generate and flash AVB 2.0 image with"
        " disable verification flag set to a"
        " partition on the device (typically called"
        " vbmeta)",
    )
    flash_vbmeta.add_argument(
        "--partition",
        default=None,
        help="partition to flash the vbmeta to (defaults to deviceinfo_flash_*_partition_vbmeta",
    )

    # Flash dtbo
    flash_dtbo = sub.add_parser("flash_dtbo", help="flash dtbo image")
    flash_dtbo.add_argument(
        "--partition",
        default=None,
        help="partition to flash the dtbo to (defaults to deviceinfo_flash_*_partition_dtbo)",
    )

    # Actions without extra arguments
    sub.add_parser("sideload", help="sideload recovery zip")
    sub.add_parser(
        "list_flavors",
        help="list installed kernel flavors" + " inside the device rootfs chroot on this computer",
    )
    sub.add_parser("list_devices", help="show connected devices")

    group = ret.add_argument_group(
        "heimdall options",
        "With heimdall as"
        " flash method, the device automatically"
        " reboots after each flash command. Use"
        " --no-reboot and --resume for multiple"
        " flash actions without reboot.",
    )
    group.add_argument(
        "--no-reboot",
        dest="no_reboot",
        help="don't automatically reboot after flashing",
        action="store_true",
    )
    group.add_argument(
        "--resume",
        dest="resume",
        help="resume flashing after using --no-reboot",
        action="store_true",
    )

    return ret


def arguments_initfs(
    subparser: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    ret = subparser.add_parser("initfs", help="do something with the initramfs")
    sub = ret.add_subparsers(dest="action_initfs")

    # hook ls
    sub.add_parser("hook_ls", help="list available and installed hook packages")

    # hook add/del
    hook_add = sub.add_parser("hook_add", help="add a hook package")
    hook_del = sub.add_parser("hook_del", help="uninstall a hook package")
    for action in [hook_add, hook_del]:
        action.add_argument(
            "hook",
            help="name of the hook aport, without"
            f" the '{pmb.config.initfs_hook_prefix}' prefix,"
            " for example: 'debug-shell'",
        )

    # ls, build, extract
    sub.add_parser("ls", help="list initramfs contents")
    sub.add_parser("build", help="(re)build the initramfs")
    sub.add_parser("extract", help="extract the initramfs to a temporary folder")

    return ret


def arguments_qemu(
    subparser: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    ret = subparser.add_parser("qemu")
    ret.add_argument("--image-size", help="set rootfs size (e.g. 2048M or 2G)")
    ret.add_argument(
        "--second-storage",
        metavar="IMAGE_SIZE",
        help="add a second storage with the given size (default:"
        " 8G), gets created if it does not exist. Use to"
        " test install from SD to eMMC",
        nargs="?",
        default=None,
        const="8G",
    )
    ret.add_argument("-m", "--memory", type=int, default=1024, help="guest RAM (default: 1024)")
    ret.add_argument("-p", "--port", type=int, default=2222, help="SSH port (default: 2222)")

    ret.add_argument(
        "--no-kvm",
        dest="qemu_kvm",
        default=True,
        action="store_false",
        help="Avoid using hardware-assisted virtualization with KVM even when available (SLOW!)",
    )
    ret.add_argument(
        "--cpu",
        dest="qemu_cpu",
        help="Override emulated QEMU CPU. By default, the host"
        " CPU will be emulated when using KVM and the QEMU"
        " default otherwise (usually a CPU with minimal"
        " features). A useful value is 'max' (emulate all"
        " features that are available), use --cpu help to get a"
        " list of possible values from QEMU.",
    )

    ret.add_argument(
        "--tablet",
        dest="qemu_tablet",
        action="store_true",
        default=False,
        help="Use 'tablet' instead of 'mouse'"
        " input for QEMU. The tablet input device automatically"
        " grabs/releases the mouse when moving in/out of the QEMU"
        " window. (NOTE: For some reason the mouse position is"
        " not reported correctly with this in some cases...)",
    )

    ret.add_argument(
        "--display",
        dest="qemu_display",
        choices=["sdl", "gtk", "none"],
        help="QEMU's display parameter (default: gtk,gl=on)",
        default="gtk",
        nargs="?",
    )
    ret.add_argument(
        "--no-gl",
        dest="qemu_gl",
        default=True,
        action="store_false",
        help="Avoid using GL for accelerating graphics in QEMU  (use software rasterizer, slow!)",
    )

    ret.add_argument(
        "--audio",
        dest="qemu_audio",
        choices=["alsa", "pa", "sdl"],
        help="QEMU's audio backend (default: none)",
        default=None,
        nargs="?",
    )

    ret.add_argument(
        "--host-qemu", dest="host_qemu", action="store_true", help="Use the host system's qemu"
    )

    return ret


def arguments_pkgrel_bump(
    subparser: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    ret = subparser.add_parser(
        "pkgrel_bump",
        help="increase the pkgrel to"
        " indicate that a package must be rebuilt"
        " because of a dependency change",
    )
    ret.add_argument(
        "--dry",
        action="store_true",
        help="instead of modifying APKBUILDs, exit with >0 when a package would have been bumped",
    )

    # Mutually exclusive: "--auto" or package names
    mode = ret.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--auto",
        action="store_true",
        help="all packages which"
        " depend on a library which had an incompatible update"
        " (libraries with a soname bump)",
    )
    mode.add_argument("packages", nargs="*", default=[])
    return ret


def arguments_pkgver_bump(
    subparser: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    ret = subparser.add_parser(
        "pkgver_bump",
        help="increase the pkgver and reset pkgrel to 0. useful when dealing with metapackages.",
    )

    add_packages_arg(ret, nargs="*", default=[])
    return ret


def arguments_newapkbuild(subparser: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """
    Wrapper for Alpine's "newapkbuild" command.

    Most parameters will get directly passed through, and they are defined in
    "pmb/config/__init__.py". That way they can be used here and when passing
    them through in "pmb/helpers/frontend.py". The order of the parameters is
    kept the same as in "newapkbuild -h".
    """
    sub = subparser.add_parser("newapkbuild", help="get a template to package new software")
    sub.add_argument(
        "--folder", help="set postmarketOS aports folder (default: main)", default="main"
    )

    # Passthrough: Strings (e.g. -d "my description")
    for entry in pmb.config.newapkbuild_arguments_strings:
        sub.add_argument(entry[0], dest=entry[1], help=entry[2])

    # Passthrough: Package type switches (e.g. -C for CMake)
    group = sub.add_mutually_exclusive_group()
    for entry in pmb.config.newapkbuild_arguments_switches_pkgtypes:
        group.add_argument(entry[0], dest=entry[1], help=entry[2], action="store_true")

    # Passthrough: Other switches (e.g. -c for copying sample files)
    for entry in pmb.config.newapkbuild_arguments_switches_other:
        sub.add_argument(entry[0], dest=entry[1], help=entry[2], action="store_true")

    # Force switch
    sub.add_argument(
        "-f", dest="force", action="store_true", help="force even if directory already exists"
    )

    # Passthrough: PKGNAME[-PKGVER] | SRCURL
    sub.add_argument(
        "pkgname_pkgver_srcurl",
        metavar="PKGNAME[-PKGVER] | SRCURL",
        help="set either the package name (optionally with the"
        " PKGVER at the end, e.g. 'hello-world-1.0') or the"
        " download link to the source archive",
    )


def arguments_kconfig(subparser: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # Allowed architectures
    arch_choices = Arch.supported()

    # Kconfig subparser
    ret = subparser.add_parser("kconfig", help="change or edit kernel configs")
    sub = ret.add_subparsers(dest="action_kconfig")
    sub.required = True

    # "pmbootstrap kconfig check"
    check = sub.add_parser("check", help="check kernel aport config")
    check.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="check all kernels, even the ones that would be ignored by default",
    )
    check.add_argument("--arch", choices=arch_choices, dest="arch", type=Arch.from_str)
    check.add_argument("--file", help="check a file directly instead of a config in a package")
    check.add_argument(
        "--no-details",
        action="store_false",
        dest="kconfig_check_details",
        help="print one generic error per component instead of"
        " listing each option that needs to be adjusted",
    )
    check.add_argument(
        "-k",
        "--keep-going",
        action="store_true",
        help="continue on errors instead of aborting on the first error",
    )
    check.add_argument(
        "--categories", help="a comma separated list of additional kconfig categories to check"
    )
    add_kernel_arg(check, nargs="*")

    # "pmbootstrap kconfig edit"
    edit = sub.add_parser("edit", help="edit kernel aport config")
    edit.add_argument("--arch", choices=arch_choices, dest="arch", type=Arch.from_str)
    edit.add_argument("--fragment", help="fragment filename to save changes")
    edit_ui_chooser = edit.add_mutually_exclusive_group()
    edit_ui_chooser.add_argument(
        "-x",
        dest="xconfig",
        action="store_true",
        help="use xconfig rather than menuconfig for kernel configuration",
    )
    edit_ui_chooser.add_argument(
        "-n",
        dest="nconfig",
        action="store_true",
        help="use nconfig rather than menuconfig for kernel configuration",
    )
    add_kernel_arg(edit, nargs=1)

    # "pmbootstrap kconfig migrate"
    migrate = sub.add_parser(
        "migrate",
        help="Migrate kconfig from older version to "
        "newer. Internally runs 'make oldconfig', "
        "which asks question for every new kernel "
        "config option.",
    )
    migrate.add_argument("--arch", choices=arch_choices, dest="arch", type=Arch.from_str)
    add_kernel_arg(migrate, nargs=1)

    generate = sub.add_parser("generate", help="generate kernel config from fragments")
    generate.add_argument("--arch", choices=arch_choices, dest="arch", type=Arch.from_str)
    add_kernel_arg(generate, nargs=1)


def arguments_repo_missing(
    subparser: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    ret = subparser.add_parser(
        "repo_missing",
        help="list all packages + depends from pmaports for building the repository (used by bpo)",
    )
    ret.add_argument("--arch", choices=Arch.supported(), default=Arch.native(), type=Arch.from_str)
    return ret


def arguments_test(subparser: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    test = subparser.add_parser("test", help="Internal pmbootstrap test tools")
    sub = test.add_subparsers(dest="action_test", required=True)
    sub.add_parser("apkindex_parse_all", help="parse all APKINDEX files")


def arguments_status(
    subparser: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    return subparser.add_parser("status", help="show a config and pmaports overview")


def arguments_netboot(
    subparser: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    ret = subparser.add_parser("netboot", help="launch nbd server with pmOS rootfs")
    sub = ret.add_subparsers(dest="action_netboot")
    sub.required = True

    start = sub.add_parser("serve", help="start nbd server")
    start.add_argument("--replace", action="store_true", help="replace stored netboot image")

    return ret


def arguments_ci(
    subparser: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    ret = subparser.add_parser(
        "ci",
        help="run continuous integration scripts locally of git repo in current directory",
    )
    script_args = ret.add_mutually_exclusive_group()
    script_args.add_argument("-a", "--all", action="store_true", help="run all scripts")
    script_args.add_argument("-f", "--fast", action="store_true", help="run fast scripts only")
    ret.add_argument(
        "scripts",
        nargs="*",
        metavar="script",
        help="name of the CI script to run, depending on the git repository",
    )
    return ret


def package_completer(
    prefix: str,
    _action: str,
    _parser: argparse.ArgumentParser | None = None,
    _parsed_args: list[str] | None = None,
) -> set[str]:
    return {package for package in pmb.helpers.pmaports.get_list() if package.startswith(prefix)}


def kernel_completer(
    prefix: str,
    action: str,
    parser: argparse.ArgumentParser | None = None,
    parsed_args: list[str] | None = None,
) -> list[str]:
    """:returns: matched linux-* packages, with linux-* prefix and without"""
    ret: list[str] = []

    # Full package name, starting with "linux-"
    if (len("linux-") < len(prefix) and prefix.startswith("linux-")) or "linux-".startswith(prefix):
        ret += package_completer(prefix, action, parser, parsed_args)

    # Kernel name without "linux-"
    packages = package_completer(f"linux-{prefix}", action, parser, parsed_args)
    ret += [package.replace("linux-", "", 1) for package in packages]

    return ret


def add_packages_arg(
    subparser: argparse.ArgumentParser, name: str = "packages", *args: str, **kwargs: Any
) -> None:
    arg = subparser.add_argument(name, *args, **kwargs)
    if "argcomplete" in sys.modules:
        arg.completer = package_completer  # type: ignore[attr-defined]


def add_kernel_arg(
    subparser: argparse.ArgumentParser, name: str = "package", nargs: int | str = "?"
) -> None:
    arg = subparser.add_argument(
        name, nargs=nargs, help="kernel package (e.g. linux-postmarketos-allwinner)"
    )
    if "argcomplete" in sys.modules:
        arg.completer = kernel_completer  # type: ignore[attr-defined]


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pmbootstrap")
    arch_native = Arch.native()
    arch_choices = Arch.supported()

    # Other
    parser.add_argument("-V", "--version", action="version", version=pmb.__version__)
    parser.add_argument(
        "-c",
        "--config",
        dest="config",
        type=Path,
        default=pmb.config.defaults["config"],
        help="path to pmbootstrap_v3.cfg file (default in ~/.config/)",
    )
    parser.add_argument(
        "-mp",
        "--mirror-pmOS",
        dest="deprecated_mp",
        help=argparse.SUPPRESS,
        action="append",
    )
    parser.add_argument(
        "-m",
        "--mirror-alpine",
        dest="deprecated_m",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("-j", "--jobs", help="parallel jobs when compiling")
    parser.add_argument(
        "-E",
        "--extra-space",
        help="specify an integer with the amount of additional"
        "space to allocate to the image in MB (default"
        " 0)",
    )
    parser.add_argument(
        "-B",
        "--boot-size",
        help="specify an integer with your preferred boot"
        "partition size on target machine in MB (default"
        f" {Config.get_default('boot_size')})",
    )
    parser.add_argument(
        "-p",
        "--aports",
        help="postmarketos aports (pmaports) path",
        type=lambda x: [Path(p.strip()) for p in x.split(",")],
    )
    parser.add_argument(
        "-t",
        "--timeout",
        help="seconds after which processes get killed that stopped writing any output (default: "
        "no timeout unless the environment variable 'CI' is set, in which case it is 900)",
        type=float,
    )
    parser.add_argument(
        "-w",
        "--work",
        type=lambda x: Path(x).absolute(),
        help="folder where all data gets stored (chroots, caches, built packages)",
    )
    parser.add_argument(
        "-y",
        "--assume-yes",
        help="Assume 'yes' to all"
        " question prompts. WARNING: this option will"
        " cause normal 'are you sure?' prompts to be"
        " disabled!",
        action="store_true",
    )
    parser.add_argument(
        "--as-root",
        help="Allow running as root (not"
        " recommended, may screw up your work folders"
        " directory permissions!)",
        dest="as_root",
        action="store_true",
    )
    parser.add_argument(
        "-o",
        "--offline",
        help="Do not attempt to update the package index files",
        action="store_true",
    )

    # Compiler
    parser.add_argument(
        "--no-ccache", action="store_false", dest="ccache", help="do not cache the compiled output"
    )
    parser.add_argument(
        "--no-cross",
        action="store_false",
        dest="cross",
        help="disable cross compiler, build only with QEMU and gcc (slow!)",
    )

    # Logging
    parser.add_argument("-l", "--log", dest="log", default=None, help="path to log file")
    parser.add_argument(
        "--details-to-stdout",
        dest="details_to_stdout",
        help="print details (e.g. build output) to stdout, instead of writing to the log",
        action="store_true",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        help="write even more to the logfiles (this may reduce performance)",
    )
    parser.add_argument(
        "-q", "--quiet", dest="quiet", action="store_true", help="do not output any log messages"
    )

    # Actions
    sub = parser.add_subparsers(title="action", dest="action")
    sub.add_parser("shutdown", help="umount, unregister binfmt")
    sub.add_parser(
        "index",
        help="re-index all repositories with custom built"
        " packages (do this after manually removing package files)",
    )
    sub.add_parser(
        "work_migrate",
        help="run this before using pmbootstrap"
        " non-interactively to migrate the"
        " work folder version on demand",
    )
    arguments_repo_missing(sub)
    arguments_kconfig(sub)
    arguments_export(sub)
    arguments_sideload(sub)
    arguments_netboot(sub)
    arguments_flasher(sub)
    arguments_initfs(sub)
    arguments_qemu(sub)
    arguments_pkgrel_bump(sub)
    arguments_pkgver_bump(sub)
    arguments_newapkbuild(sub)
    arguments_test(sub)
    arguments_status(sub)
    arguments_ci(sub)

    # Action: init
    init = sub.add_parser("init", help="initialize config file")
    init.add_argument(
        "--shallow-initial-clone",
        help="do a shallow clone if pmaports has to be cloned. Primarily useful to speed up scripts.",
        action="store_true",
    )

    # Action: log
    log = sub.add_parser("log", help="follow the pmbootstrap logfile")
    log.add_argument("-n", "--lines", type=int, default=60, help="count of initial output lines")
    log.add_argument("-c", "--clear", help="clear the log", action="store_true", dest="clear_log")

    # Action: zap
    zap = sub.add_parser("zap", help="safely delete chroot folders")
    zap.add_argument(
        "--dry",
        action="store_true",
        help="instead of actually deleting anything, print out what would have been deleted",
    )
    zap.add_argument("-hc", "--http", action="store_true", help="also delete http cache")
    zap.add_argument(
        "-d", "--distfiles", action="store_true", help="also delete downloaded source tarballs"
    )
    zap.add_argument(
        "-p",
        "--pkgs-local",
        action="store_true",
        dest="pkgs_local",
        help="also delete *all* locally compiled packages",
    )
    zap.add_argument(
        "-m",
        "--pkgs-local-mismatch",
        action="store_true",
        dest="pkgs_local_mismatch",
        help="also delete locally compiled packages without existing aport of same version",
    )
    zap.add_argument(
        "-n", "--netboot", action="store_true", help="also delete stored images for netboot"
    )
    zap.add_argument(
        "-o",
        "--pkgs-online-mismatch",
        action="store_true",
        dest="pkgs_online_mismatch",
        help="also delete outdated packages from online mirrors"
        " (that have been downloaded to the apk cache)",
    )
    zap.add_argument("-r", "--rust", action="store_true", help="also delete rust related caches")

    zap_all_delete_args = [
        "http",
        "distfiles",
        "pkgs_local",
        "pkgs_local_mismatch",
        "netboot",
        "pkgs_online_mismatch",
        "rust",
    ]
    zap_all_delete_args_print = [arg.replace("_", "-") for arg in zap_all_delete_args]
    zap.add_argument(
        "-a",
        "--all",
        action=toggle_other_boolean_flags(*zap_all_delete_args),
        help=f"delete everything, equivalent to: --{' --'.join(zap_all_delete_args_print)}",
    )

    # Action: stats
    stats = sub.add_parser("stats", help="show ccache stats")
    stats.add_argument("--arch", default=arch_native, choices=arch_choices, type=Arch.from_str)

    # Action: update
    update = sub.add_parser("update", help="update all existing APKINDEX files")
    update.add_argument(
        "--arch",
        default=None,
        choices=arch_choices,
        help="only update a specific architecture",
        type=Arch.from_str,
    )
    update.add_argument(
        "--non-existing",
        action="store_true",
        help="do not only update the existing APKINDEX files, but all of them",
        dest="non_existing",
    )

    # Action: build_init / chroot
    build_init = sub.add_parser(
        "build_init",
        help="initialize build environment (usually you do not need to call this)",
    )
    chroot = sub.add_parser("chroot", help="start shell in chroot")
    chroot.add_argument(
        "--add",
        help="build/install comma separated list of packages in the chroot before entering it",
    )
    chroot.add_argument("--user", help="run the command as user, not as root", action="store_true")
    chroot.add_argument(
        "--output",
        choices=list(RunOutputTypeDefault),
        type=RunOutputTypeDefault.from_string,
        help="how the output of the"
        " program should be handled, choose from: 'log',"
        " 'stdout', 'interactive', 'tui' (default),"
        " 'background'. Details: pmb/helpers/run_core.py",
        default=RunOutputTypeDefault.TUI,
    )
    chroot.add_argument(
        "--image",
        help="Mount the rootfs image and treat it like a normal chroot.",
        action="store_true",
    )
    chroot.add_argument(
        "--usb",
        help="Make USB devices accessible inside the chroot.",
        action="store_true",
        dest="chroot_usb",
    )
    chroot.add_argument(
        "command",
        default=["sh", "-i"],
        help="command to execute inside the chroot. default: sh",
        nargs="*",
    )
    chroot.add_argument(
        "-x",
        "--xauth",
        action="store_true",
        help="Copy .Xauthority and set environment variables,"
        " so X11 applications can be started (native"
        " chroot only)",
    )
    chroot.add_argument(
        "-i",
        "--install-blockdev",
        action="store_true",
        help="Create a sparse image file and mount it as"
        " /dev/install, just like during the"
        " installation process.",
    )
    for action in [build_init, chroot]:
        suffix = action.add_mutually_exclusive_group()
        if action == chroot:
            suffix.add_argument(
                "-r", "--rootfs", action="store_true", help="Chroot for the device root file system"
            )
        suffix.add_argument(
            "-b",
            "--buildroot",
            nargs="?",
            const="device",
            choices={"device"} | {str(a) for a in arch_choices},
            help="Chroot for building packages, defaults to device architecture",
        )
        suffix.add_argument(
            "-s",
            "--suffix",
            default=None,
            help="Specify any chroot suffix, defaults to 'native'",
        )

    # Action: install
    arguments_install(sub)

    # Action: checksum
    checksum = sub.add_parser("checksum", help="update aport checksums")
    checksum.add_argument(
        "--verify",
        action="store_true",
        help="download"
        " sources and verify that the checksums of the"
        " APKBUILD match, instead of updating them",
    )
    checksum_changed = checksum.add_mutually_exclusive_group(required=False)
    checksum_changed.add_argument(
        "--changed",
        action="store_true",
        help="update checksums of all packages that have unstaged or uncommitted changes",
        required=False,
    )
    checksum_changed.add_argument("packages", nargs="*", default=[])

    # Action: aportgen
    aportgen = sub.add_parser(
        "aportgen",
        help="generate a postmarketOS specific package build recipe (aport/APKBUILD)",
    )
    aportgen_fork_alpine = aportgen.add_mutually_exclusive_group()
    aportgen_fork_alpine.add_argument(
        "-a",
        "--fork-alpine",
        help="fork the alpine upstream package",
        action="store_true",
        dest="fork_alpine",
    )
    aportgen_fork_alpine.add_argument(
        "-r",
        "--fork-alpine-retain-branch",
        help="fork the alpine upstream, but don't change branch to match the current channel",
        action="store_true",
        dest="fork_alpine_retain_branch",
    )
    add_packages_arg(aportgen, nargs="+")

    # Action: build
    build = sub.add_parser("build", help="create a package for a specific architecture")
    build.add_argument(
        "--arch",
        choices=arch_choices,
        default=None,
        help="CPU architecture to build for (default: "
        f"{arch_native} or first available architecture in"
        " APKBUILD)",
        type=Arch.from_str,
    )
    build.add_argument("--force", action="store_true", help="even build if not necessary")
    build.add_argument(
        "--strict", action="store_true", help="Deprecated: strict builds are now the default"
    )
    build.add_argument(
        "--lax",
        action="store_true",
        help="(faster) do not zap chroots for faster package builds",
    )
    build.add_argument(
        "--src",
        help="override source used to build the"
        " package with a local folder (the APKBUILD must"
        " expect the source to be in $builddir, so you might"
        " need to adjust it)",
        nargs=1,
    )
    build.add_argument(
        "-i",
        "--ignore-depends",
        action="store_true",
        help="only build and install makedepends from an"
        " APKBUILD, ignore the depends (old behavior). This is"
        " faster for device packages for example, because then"
        " you don't need to build and install the kernel. But"
        " it is incompatible with how Alpine's abuild handles"
        " it.",
        dest="ignore_depends",
    )
    build.add_argument(
        "-n",
        "--no-depends",
        action="store_true",
        help="never build dependencies, abort instead",
        dest="no_depends",
    )
    build.add_argument(
        "--go-mod-cache",
        action="store_true",
        default=None,
        help="for go packages: Usually they should bundle the"
        " dependency sources instead of downloading them"
        " at build time. But if they don't (e.g. with"
        " pmbootstrap build --src), then this option can"
        " be used to let GOMODCACHE point into"
        " pmbootstrap's work dir to only download"
        " dependencies once. (default: true with --src,"
        " false otherwise)",
    )
    build.add_argument(
        "--no-go-mod-cache",
        action="store_false",
        dest="go_mod_cache",
        default=None,
        help="don't set GOMODCACHE",
    )
    build.add_argument(
        "--envkernel",
        action="store_true",
        help="Create an apk package from the build output of"
        " a kernel compiled locally on the host or with envkernel.sh.",
    )
    add_packages_arg(build, nargs="+")

    # Action: deviceinfo_parse
    deviceinfo_parse = sub.add_parser("deviceinfo_parse")
    deviceinfo_parse.add_argument("devices", nargs="*")
    deviceinfo_parse.add_argument(
        "--kernel",
        help="the kernel to select (for"
        " device packages with multiple kernels),"
        " e.g. 'downstream', 'mainline'",
        dest="deviceinfo_parse_kernel",
        metavar="KERNEL",
    )

    # Action: apkbuild_parse
    apkbuild_parse = sub.add_parser("apkbuild_parse")
    add_packages_arg(apkbuild_parse, nargs="*")

    # Action: apkindex_parse
    apkindex_parse = sub.add_parser("apkindex_parse")
    apkindex_parse.add_argument("apkindex_path", type=Apkindex)
    add_packages_arg(apkindex_parse, "package", nargs="?")

    # Action: config
    config = sub.add_parser("config", help="get and set pmbootstrap options")
    config.add_argument(
        "-r",
        "--reset",
        action="store_true",
        help="Reset config options with the given name to it's default.",
    )
    config.add_argument(
        "name",
        nargs="?",
        help="variable name, one of: " + ", ".join(sorted(Config.keys())),
        choices=Config.keys(),
        metavar="name",
    )
    config.add_argument("value", nargs="?", help="set variable to value")

    # Action: bootimg_analyze
    bootimg_analyze = sub.add_parser(
        "bootimg_analyze", help="Extract all the information from an existing boot.img"
    )
    bootimg_analyze.add_argument("path", help="path to the boot.img", type=Path)
    bootimg_analyze.add_argument(
        "--force", "-f", action="store_true", help="force even if the file seems to be invalid"
    )

    # Action: pull
    sub.add_parser(
        "pull", help="update all git repositories that pmbootstrap cloned (pmaports, etc.)"
    )

    if "argcomplete" in sys.modules:
        argcomplete.autocomplete(parser, always_complete_options="long")

    return parser


def arguments() -> PmbArgs:
    args = PmbArgs()
    get_parser().parse_args(namespace=args)

    if getattr(args, "fork_alpine_retain_branch", False):
        # fork_alpine_retain_branch largely matches the behaviour of fork_alpine, so
        # just set fork_alpine here to reduce repetition.
        args.fork_alpine = args.fork_alpine_retain_branch

    pmb.helpers.args.init(args)

    if getattr(args, "deprecated_mp", None) or getattr(args, "deprecated_m", None):
        raise NonBugError(
            "Arguments --mirror-pmOS and --mirror-alpine have been removed. See docs/mirrors.md"
            " regarding how to set mirrors now."
        )

    if getattr(args, "go_mod_cache", None) is None:
        gomodcache = bool(getattr(args, "src", None))
        args.go_mod_cache = gomodcache

    return args
