# Copyright 2026 Oliver Smith, Paul Adam
# SPDX-License-Identifier: GPL-3.0-or-later
from getpass import getpass
from pathlib import Path

import pmb.config
import pmb.helpers.cli
import pmb.install
import pmb.parse
from pmb.core.context import get_context
from pmb.helpers import logging
from pmb.helpers.exceptions import NonBugError


def install(
    add: str,
    android_recovery_zip: bool,
    cipher: str,
    cmdline: str | None,
    filesystem: str,
    full_disk_encryption: bool,
    disk: Path | None,
    install_base: bool,
    install_cgpt: bool,
    install_local_pkgs: bool,
    install_recommends: bool,
    iter_time: str,
    no_fde: bool,
    no_firewall: bool,
    no_image: bool,
    no_reboot: bool,
    no_sshd: bool,
    partition: str | None,
    password: str,
    recovery_flash_kernel: bool,
    recovery_install_partition: str,
    resume: bool,
    rsync: bool,
    sector_size: int | None,
    single_partition: bool,
    sparse: bool | None,
    split: bool | None,
    verbose: bool,
    zap: bool,
    wifi_ssid: str | None = None,
    wifi_psk: str | None = None,
    wifi_country: str | None = None,
) -> None:
    config = get_context().config
    device = config.device
    deviceinfo = pmb.parse.deviceinfo(device)
    is_split = split if split is not None else False
    if no_fde:
        logging.warning("WARNING: --no-fde is deprecated, as it is now the default.")
    if rsync and full_disk_encryption:
        raise ValueError("Installation using rsync is not compatible with full disk encryption.")
    if rsync and not disk:
        raise ValueError("Installation using rsync only works with --disk.")

    if rsync and filesystem == "btrfs":
        raise ValueError("Installation using rsync is not currently supported on btrfs filesystem.")

    if not disk and split is None:
        # Default to split if the flash method requires it
        flasher = pmb.config.flashers.get(deviceinfo.flash_method, {})
        if flasher.get("split", False):
            is_split = True

    # Android recovery zip related
    if android_recovery_zip and filesystem:
        raise ValueError(
            "--android-recovery-zip cannot be combined with --filesystem (patches welcome)"
        )
    if android_recovery_zip and full_disk_encryption:
        logging.info(
            "WARNING: --fde is rarely used in combination with"
            " --android-recovery-zip. If this does not work, consider"
            " using another method (e.g. installing via netcat)"
        )
        logging.info(
            "WARNING: the kernel of the recovery system (e.g. TWRP)"
            f" must support the cryptsetup cipher '{cipher}'."
        )
        logging.info(
            "If you know what you are doing, consider setting a"
            " different cipher with 'pmbootstrap install --cipher=..."
            " --fde --android-recovery-zip'."
        )

    # Don't install locally compiled packages and package signing keys
    if not install_local_pkgs:
        # Implies that we don't build outdated packages (overriding the answer
        # in 'pmbootstrap init')
        config.build_pkgs_on_install = False

        # Safest way to avoid installing local packages is having none
        if list((config.work / "packages").glob("*")):
            raise ValueError(
                "--no-local-pkgs specified, but locally built"
                " packages found. Consider 'pmbootstrap zap -p'"
                " to delete them."
            )

    if not password:
        password = ""
        while not password:
            password = getpass(f"Choose a password for the user '{config.user}': ")
            if not password:
                logging.error("ERROR: Password must not be empty!")

        if getpass(f"Confirm password for '{config.user}': ") != password:
            raise NonBugError("Passwords did not match!")

    # Verify that the root filesystem is supported by current pmaports branch
    pmb.install.get_root_filesystem(filesystem)

    # Ask for the wifi credentials, like we ask for the password above. Only
    # alpine_only devices, everything else configures wifi through
    # NetworkManager after the first boot.
    if (
        not wifi_ssid
        and config.headless
        and not get_context().assume_yes
        and pmb.parse.device_is_alpine_only(config.device)
    ):
        logging.info(
            "This device is configured for a headless setup, so it needs Wi-Fi credentials to"
            " be reachable over SSH after the first boot. Leave empty to skip."
        )
        wifi_ssid = pmb.helpers.cli.ask("Wi-Fi network name (SSID)", None, "", False)
        if wifi_ssid:
            while True:
                wifi_psk = getpass(f"Passphrase for '{wifi_ssid}' (empty for an open network): ")
                if not wifi_psk or 8 <= len(wifi_psk) <= 63 or len(wifi_psk) == 64:
                    break
                logging.error("ERROR: Must be 8 to 63 characters, or a 64 character hex PSK!")
            wifi_country = pmb.helpers.cli.ask(
                "Wi-Fi country code", None, wifi_country or "US", False
            )

    pmb.install.install(
        add,
        android_recovery_zip,
        cipher,
        cmdline,
        filesystem,
        full_disk_encryption,
        disk,
        install_base,
        install_cgpt,
        install_local_pkgs,
        install_recommends,
        iter_time,
        no_firewall,
        no_image,
        no_reboot,
        no_sshd,
        partition,
        password,
        recovery_flash_kernel,
        recovery_install_partition,
        resume,
        rsync,
        sector_size,
        single_partition,
        sparse,
        verbose,
        zap,
        is_split,
        wifi_ssid=wifi_ssid,
        wifi_psk=wifi_psk,
        wifi_country=wifi_country,
    )
