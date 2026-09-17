# Copyright 2024 Casey Connolly
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import pmb.config
from pmb.commands.repo_missing import repo_missing
from pmb.core import Chroot, ChrootType
from pmb.core.arch import Arch
from pmb.core.context import get_context
from pmb.helpers import logging
from pmb.types import PmbArgs

from .apkbuild_parse import apkbuild_parse
from .apkindex_parse import apkindex_parse
from .aportgen import aportgen
from .bootimg_analyze import bootimg_analyze
from .build import build
from .build_init import build_init
from .checksum import checksum
from .chroot import chroot
from .ci import ci
from .config import config
from .export import export
from .flasher import flasher
from .index import index
from .initfs import initfs
from .install import install
from .kconfig import KConfigCheck, KConfigEdit, KConfigGenerate, KConfigMigrate
from .log import log
from .netboot import netboot
from .newapkbuild import newapkbuild
from .pkgrel_bump import pkgrel_bump
from .pkgver_bump import pkgver_bump
from .pull import pull
from .qemu import qemu
from .shutdown import shutdown
from .sideload import sideload
from .stats import stats
from .status import status
from .test import test
from .update import update
from .work_migrate import work_migrate
from .zap import zap

"""New way to model pmbootstrap subcommands that can be invoked without PmbArgs."""


def _parse_suffix(buildroot: str, image: bool, rootfs: bool, suffix: str) -> Chroot:
    deviceinfo = pmb.parse.deviceinfo()
    if image:
        rootfs_path = Chroot.native() / f"home/pmos/rootfs/{deviceinfo.codename}.img"
        return Chroot(ChrootType.IMAGE, str(rootfs_path))
    if rootfs:
        return Chroot(ChrootType.ROOTFS, get_context().config.device)
    elif buildroot:
        if buildroot == "device":
            return Chroot.buildroot(deviceinfo.arch)
        else:
            return Chroot.buildroot(Arch.from_str(buildroot))
    elif suffix:
        (t_, s) = suffix.split("_")
        t: ChrootType = ChrootType(t_)
        return Chroot(t, s)
    else:
        return Chroot(ChrootType.NATIVE)


def run_command(args: PmbArgs) -> None:
    match args.action:
        case "apkbuild_parse":
            apkbuild_parse(args.packages)
        case "apkindex_parse":
            apkindex_parse(args.apkindex_path, args.package)
        case "aportgen":
            aportgen(args.packages, args.fork_alpine, args.fork_alpine_retain_branch)
        case "bootimg_analyze":
            bootimg_analyze(args.path)
        case "build":
            if args.strict:
                logging.warning(
                    "WARNING: The --strict argument to 'pmbootstrap build' is deprecated. It is now the default behavior and the flag no longer has any effect."
                )

            build(args.packages, args.arch, args.src, args.envkernel, not args.lax)
        case "build_init":
            build_init(
                _parse_suffix(
                    args.buildroot,
                    getattr(args, "image", False),
                    getattr(args, "rootfs", False),
                    args.suffix,
                )
            )
        case "chroot":
            chroot(
                args.add,
                _parse_suffix(
                    args.buildroot,
                    getattr(args, "image", False),
                    getattr(args, "rootfs", False),
                    args.suffix,
                ),
                args.chroot_usb,
                args.command,
                args.install_blockdev,
                args.output,
                getattr(args, "sector_size", None),
                args.user,
                args.xauth,
            )
        case "checksum":
            checksum(args.packages, args.changed, args.verify)
        case "ci":
            ci(args.scripts, args.all, args.fast)
        case "config":
            config(args.name, args.value, args.reset, args.config)
        case "export":
            export(args.export_folder, args.autoinstall, args.odin_flashable_tar)
        case "flasher":
            flasher(
                args.action_flasher,
                # FIXME: defaults copied from pmb/helpers/arguments.py
                # we should have these defaults defined in one place!
                getattr(args, "autoinstall", True),
                getattr(args, "cmdline", None),
                args.flash_method,
                getattr(args, "no_reboot", False),
                getattr(args, "partition", None),
                getattr(args, "resume", False),
            )
        case "log":
            log(args.clear_log, args.lines)
        case "index":
            # FIXME: should index support --arch?
            index()
        case "initfs":
            initfs(args.action_initfs, args.hook if "hook" in args else None)
        case "install":
            install(
                args.add,
                args.android_recovery_zip,
                args.cipher,
                getattr(args, "cmdline", None),
                args.filesystem,
                args.full_disk_encryption,
                args.disk,
                args.install_base,
                args.install_cgpt,
                args.install_local_pkgs,
                args.install_recommends,
                args.iter_time,
                args.no_fde,
                args.no_firewall,
                args.no_image,
                getattr(args, "no_reboot", False),
                args.no_sshd,
                getattr(args, "partition", None),
                args.password,
                args.recovery_flash_kernel,
                args.recovery_install_partition,
                getattr(args, "resume", False),
                args.rsync,
                args.sector_size,
                args.single_partition,
                args.sparse,
                args.split,
                args.verbose,
                args.zap,
                wifi_ssid=args.wifi_ssid,
                wifi_psk=args.wifi_psk,
                wifi_country=args.wifi_country,
            )
        case "shutdown":
            shutdown()
        case "sideload":
            sideload(
                args.user or get_context().config.user,
                args.host,
                str(args.port),
                args.arch,
                args.install_key,
                args.packages,
                args.allow_untrusted,
            )
        case "test":
            test(args.action_test)
        case "netboot":
            netboot(args.action_netboot, args.replace)
        case "newapkbuild":
            # Passthrough: Strings (e.g. -d "my description")
            pass_through = []
            for entry in pmb.config.newapkbuild_arguments_strings:
                value = getattr(args, entry[1])
                if value:
                    pass_through += [entry[0], value]

            # Passthrough: Switches (e.g. -C for CMake)
            for entry in (
                pmb.config.newapkbuild_arguments_switches_pkgtypes
                + pmb.config.newapkbuild_arguments_switches_other
            ):
                if getattr(args, entry[1]) is True:
                    pass_through.append(entry[0])

            # Passthrough: PKGNAME[-PKGVER] | SRCURL
            pass_through.append(args.pkgname_pkgver_srcurl)
            newapkbuild(args.folder, pass_through, args.pkgname, args.pkgname_pkgver_srcurl)
        case "pkgrel_bump":
            pkgrel_bump(args.packages, args.dry, args.auto)
        case "pkgver_bump":
            pkgver_bump(args.packages)
        case "pull":
            pull()
        case "qemu":
            qemu(
                args.qemu_audio,
                args.qemu_cpu,
                args.qemu_display,
                args.memory,
                args.image_size,
                args.second_storage,
                args.port,
                args.host_qemu,
                args.qemu_gl,
                args.qemu_kvm,
                args.qemu_tablet,
            )
        case "kconfig":
            match args.action_kconfig:
                case "check":
                    categories = args.categories.split(",") if args.categories else []
                    KConfigCheck(
                        args.kconfig_check_details,
                        args.file,
                        args.package,
                        args.keep_going,
                        categories,
                    ).run()
                case "edit":
                    KConfigEdit(
                        args.package[0], args.arch, args.xconfig, args.nconfig, args.fragment
                    ).run()
                case "migrate":
                    KConfigMigrate(args.package, args.arch).run()
                case "generate":
                    KConfigGenerate(args.package, args.arch).run()
        case "repo_missing":
            repo_missing(args.arch)
        case "stats":
            stats(args.arch)
        case "status":
            status()
        case "update":
            update(args.arch, args.non_existing)
        case "work_migrate":
            work_migrate()
        case "zap":
            zap(
                args.dry,
                args.http,
                args.distfiles,
                args.pkgs_local,
                args.pkgs_local_mismatch,
                args.pkgs_online_mismatch,
                args.rust,
                args.netboot,
            )
        case _:
            raise NotImplementedError(f"Command '{args.action}' is not implemented.")
