# Copyright 2024 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
import pmb.helpers.ui
from pmb.core import Config
from pmb.core.config import ServiceManagerConfig
from pmb.helpers.exceptions import NonBugError
from pmb.types import WithExtraRepos


def service_managers_from_packaging(
    ui: str,
) -> tuple[ServiceManagerConfig, list[ServiceManagerConfig], str]:
    """
    Get default and available service managers and reasoning based on the
    selected UI package (and in the future device too).
    """
    default = ServiceManagerConfig.SYSTEMD
    available = [ServiceManagerConfig.SYSTEMD, ServiceManagerConfig.OPENRC]
    reason = ""

    # Select based on UI APKBUILD options:
    # * pmb:support-systemd
    # * pmb:support-openrc
    # * pmb:default-systemd
    # * pmb:default-openrc
    pkgname_ui = f"postmarketos-ui-{ui}"
    apkbuild_ui = pmb.helpers.pmaports.get(pkgname_ui, False, False, WithExtraRepos.DISABLED)
    if not apkbuild_ui:
        reason = f"{pkgname_ui} does not exist on current pmaports branch"
    else:
        opts = apkbuild_ui["options"]
        if "pmb:default-systemd" not in opts and "pmb:default-openrc" not in opts:
            # Legacy case where we don't know whether systemd and openrc are
            # explicitly supported or not. We only know the default by looking
            # at the deprecated pmb:systemd being present or not.
            # TODO: Remove after enforcing in pmaports CI that all UIs have
            # either pmb:default-systemd or pmb:default-openrc set and that all
            # supported pmaports branches have this
            if "pmb:systemd" in opts:
                reason = f"{pkgname_ui} defaults to systemd"
            else:
                default = ServiceManagerConfig.OPENRC
                reason = f"{pkgname_ui} defaults to openrc"
        elif "pmb:support-systemd" not in opts:
            default = ServiceManagerConfig.OPENRC
            available = [ServiceManagerConfig.OPENRC]
            reason = f"{pkgname_ui} doesn't support systemd"
        elif "pmb:support-openrc" not in opts:
            available = [ServiceManagerConfig.SYSTEMD]
            reason = f"{pkgname_ui} doesn't support openrc"
        else:
            # pmb:support-systemd and pmb:support-openrc are in opts
            # as enforced by pmb.helpers.ui.validate_ui_options().
            if "pmb:default-systemd" in opts:
                reason = f"{pkgname_ui} defaults to systemd"
            elif "pmb:default-openrc" in opts:
                default = ServiceManagerConfig.OPENRC
                reason = f"{pkgname_ui} defaults to openrc"
            else:
                raise NonBugError(f"{pkgname_ui} doesn't set a default service manager")

    return default, available, reason


def service_managers_from_user_selection(config: Config) -> tuple[ServiceManagerConfig, str]:
    """
    Get selected service manager and reasoning based on user selection in
    "pmbootstrap init".
    """
    # Imported here to avoid a circular import at module load time
    from pmb.parse.deviceinfo import device_is_alpine_only

    # systemd is packaged in postmarketOS' extra-repo, so a device that is
    # installed from Alpine only can only use OpenRC (which is what
    # alpine-base depends on anyway).
    if device_is_alpine_only(config.device):
        return ServiceManagerConfig.OPENRC, "device sets deviceinfo_alpine_only"

    default, available, reason = service_managers_from_packaging(config.ui)
    selected = ServiceManagerConfig.SYSTEMD

    if len(available) > 1:
        match config.service_manager:
            case ServiceManagerConfig.DEFAULT:
                selected = default
                reason = f"default selected in pmbootstrap init, {reason}"
            case ServiceManagerConfig.SYSTEMD:
                selected = ServiceManagerConfig.SYSTEMD
                reason = "systemd selected in pmbootstrap init"
            case ServiceManagerConfig.OPENRC:
                selected = ServiceManagerConfig.OPENRC
                reason = "openrc selected in pmbootstrap init"
    else:
        selected = default

    return selected, reason


def is_systemd_selected(config: Config) -> bool:
    selected, _ = service_managers_from_user_selection(config)
    return selected == ServiceManagerConfig.SYSTEMD


def systemd_selected_str(config: Config) -> tuple[str, str]:
    selected, reasoning = service_managers_from_user_selection(config)
    yesno = "yes" if selected == ServiceManagerConfig.SYSTEMD else "no"
    return yesno, reasoning
