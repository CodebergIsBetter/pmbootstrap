# Copyright 2023 Clayton Craft
# SPDX-License-Identifier: GPL-3.0-or-later
import os
from typing import NamedTuple

import pmb.helpers.pmaports
import pmb.parse
import pmb.parse.apkindex
from pmb.core.arch import Arch
from pmb.core.pkgrepo import pkgrepo_iglob
from pmb.helpers import logging
from pmb.helpers.exceptions import NonBugError
from pmb.types import WithExtraRepos


def validate_ui_options(ui: str) -> None:
    if check_option(ui, "pmb:default-systemd") and check_option(ui, "pmb:default-openrc"):
        raise NonBugError(
            f"ERROR: UI {ui} has both pmb:default-systemd and pmb:default-openrc in the APKBUILD options. Only one can be used at a time!"
        )

    if check_option(ui, "pmb:default-systemd") and not check_option(ui, "pmb:support-systemd"):
        raise NonBugError(
            f"ERROR: UI {ui} has pmb:default-systemd without pmb:support-systemd the APKBUILD options!"
        )

    if check_option(ui, "pmb:default-openrc") and not check_option(ui, "pmb:support-openrc"):
        raise NonBugError(
            f"ERROR: UI {ui} has pmb:default-openrc without pmb:support-openrc the APKBUILD options!"
        )


# UIs for devices that set deviceinfo_alpine_only. The postmarketos-ui-* meta
# packages depend on postmarketos-base-ui and therefore on the whole
# postmarketOS base system, so they cannot be used. The UI is a plain Alpine
# package name instead, and only the ones Alpine actually builds for the
# device's architecture are offered.
#
# The "recommends" lists are the equivalent of _pmb_recommends in a
# postmarketos-ui-* APKBUILD: the packages that turn a bare compositor or window
# manager into something usable. They are installed unless --no-recommends is
# passed, and silently skipped when Alpine does not build them for the device's
# architecture.


class AlpineUi(NamedTuple):
    pkgname: str
    description: str
    recommends: list[str]
    # Basename of the .desktop file in /usr/share/{wayland-sessions,xsessions}.
    # Only needed when it is not "<pkgname>.desktop": several packages ship a
    # session file named after the binary rather than the package, and some
    # ship both an X11 and a Wayland one.
    session: str = ""


# Audio, portals and fonts are wanted by every graphical session
_common = [
    # Every GTK UI in the table below asks for the Adwaita icon theme through
    # its default xsettings, and hicolor is only a fallback directory spec
    # with no generic icons of its own. Without this, stock and file-type
    # icons resolve to nothing: Thunar, panels and dialogs come up covered in
    # missing-image placeholders and the desktop reads as broken.
    "adwaita-icon-theme",
    "font-dejavu",
    "pipewire",
    "pipewire-alsa",
    "pipewire-pulse",
    # Without this, PipeWire registers no A2DP/HFP endpoints and Bluetooth
    # headsets are invisible to it, even though bluez itself pairs fine
    "pipewire-spa-bluez",
    "wireplumber",
    "xdg-desktop-portal",
    "xdg-desktop-portal-gtk",
]

# wlroots based compositors
_wayland = [
    *_common,
    # The GPU userspace belongs to the session, not to the board. On a device
    # that sets deviceinfo_alpine_only there is no postmarketos-base to pull
    # these in by install_if, and having the device package depend on them
    # would put the whole gallium/LLVM stack into a headless image that never
    # composites anything. mesa-dri-gallium is the VideoCore IV driver;
    # mesa-gbm and mesa-egl/gles are what wlroots links against at runtime.
    "mesa-dri-gallium",
    "mesa-egl",
    "mesa-gbm",
    "mesa-gles",
    "brightnessctl",
    "foot",
    "grim",
    "slurp",
    "wl-clipboard",
    "xdg-desktop-portal-wlr",
    "xwayland",
]

# X11 window managers need a display server and input driver on top
_x11 = [
    *_common,
    "setxkbmap",
    "xf86-input-libinput",
    "xinit",
    "xorg-server",
    "xrandr",
    "xterm",
]

alpine_uis: list[AlpineUi] = [
    AlpineUi(
        "sway",
        "(Wayland) Tiling compositor, drop-in replacement for i3wm",
        [*_wayland, "swaybg", "swayidle", "swaylock", "wmenu"],
    ),
    AlpineUi(
        "labwc",
        "(Wayland) Stacking compositor, inspired by Openbox",
        [*_wayland, "bemenu", "swaybg"],
    ),
    AlpineUi(
        "hyprland",
        "(Wayland) Tiling compositor with eye candy (needs GLES 3.x)",
        [*_wayland, "bemenu"],
    ),
    AlpineUi(
        "weston",
        "(Wayland) Reference compositor",
        # The weston package ships only renderers. Without a backend module and
        # a shell it dies at startup with "failed to create compositor backend",
        # and it ships a session file, so tinydm starts it anyway.
        [*_common, "weston-backend-drm", "weston-shell-desktop", "weston-terminal"],
    ),
    AlpineUi("cage", "(Wayland) Kiosk compositor, runs a single application", _common),
    AlpineUi(
        "xfce4",
        "(Wayland) Lightweight desktop environment, runs inside labwc",
        # startxfce4 --wayland launches XFCE inside labwc (its hardcoded
        # default_compositor), which is wlroots - so the desktop is composited
        # on the GPU instead of going through Xorg's software 2D path.
        [*_wayland, "labwc", "xfce4-terminal"],
        session="xfce-wayland.desktop",
    ),
    AlpineUi(
        "mate-desktop-environment",
        "(X11) Fork of GNOME 2",
        _x11,
        session="mate.desktop",
    ),
    AlpineUi(
        "i3wm",
        "(X11) Tiling window manager",
        [*_x11, "dmenu", "i3status"],
        session="i3.desktop",
    ),
    AlpineUi("awesome", "(X11) Tiling window manager, configured in Lua", _x11),
    AlpineUi("bspwm", "(X11) Tiling window manager", [*_x11, "sxhkd", "dmenu"]),
    AlpineUi("herbstluftwm", "(X11) Manual tiling window manager", [*_x11, "dmenu"]),
    AlpineUi("spectrwm", "(X11) Tiling window manager", [*_x11, "dmenu"]),
    AlpineUi("dwm", "(X11) Dynamic window manager", [*_x11, "dmenu"]),
    AlpineUi("openbox", "(X11) Stacking window manager", [*_x11, "obconf"]),
    AlpineUi("fluxbox", "(X11) Stacking window manager", _x11),
    AlpineUi("icewm", "(X11) Lightweight window manager", _x11),
    AlpineUi("jwm", "(X11) Very lightweight window manager", _x11),
    AlpineUi("cwm", "(X11) Minimal window manager", [*_x11, "dmenu"]),
    AlpineUi(
        "windowmaker",
        "(X11) NeXTSTEP-like window manager",
        _x11,
        session="wmaker.desktop",
    ),
    AlpineUi("retroarch", "Frontend for emulators and game engines", _common),
]


def alpine_ui_recommends(ui: str, arch: Arch) -> list[str]:
    """
    Get the packages that make a given Alpine UI usable, for devices that set
    deviceinfo_alpine_only. This is the equivalent of reading _pmb_recommends
    from a postmarketos-ui-* APKBUILD.

    :param ui: the selected UI, which is an Alpine package name here
    :param arch: device architecture, for which the packages must be available
    :returns: list of Alpine package names, possibly empty
    """
    entry = next((e for e in alpine_uis if e.pkgname == ui), None)
    if not entry:
        logging.warning(
            f"WARNING: {ui} is not a known Alpine UI, so no additional packages are installed"
            " alongside it. It may not be usable on its own."
        )
        return []

    ret = []
    for pkgname in entry.recommends:
        if pmb.parse.apkindex.package(pkgname, arch, must_exist=False):
            ret.append(pkgname)
        else:
            logging.verbose(f"{ui}: skipping {pkgname}, not available for {arch}")
    return ret


ui_none = (
    "none",
    (
        "Bare minimum OS image for testing and manual"
        ' customization. The "console" UI should be selected if'
        " a graphical UI is not desired."
    ),
)


def list_ui_alpine(arch: Arch) -> list[tuple[str, str]]:
    """
    Get the UIs that Alpine provides for a given architecture, for devices that
    set deviceinfo_alpine_only.

    :param arch: device architecture, for which the UIs must be available
    :returns: [("none", "Bare minimum..."), ("sway", "(Wayland) Tiling...")]
    """
    ret = [ui_none]
    ret.extend(
        (entry.pkgname, entry.description)
        for entry in alpine_uis
        if pmb.parse.apkindex.package(entry.pkgname, arch, must_exist=False)
    )
    return ret


def list_ui(arch: Arch) -> list[tuple[str, str]]:
    """
    Get all UIs, for which aports are available with their description.

    :param arch: device architecture, for which the UIs must be available
    :returns: [("none", "No graphical..."), ("weston", "Wayland reference...")]
    """
    if pmb.parse.device_is_alpine_only():
        return list_ui_alpine(arch)

    ret = [ui_none]
    for path in sorted(pkgrepo_iglob("main/postmarketos-ui-*")):
        try:
            apkbuild = pmb.parse.apkbuild(path)
        except FileNotFoundError as exception:
            logging.debug("Skipping UI directory without APKBUILD '%s' (%s)", path, exception)
            continue
        ui = os.path.basename(path).split("-", 2)[2]
        validate_ui_options(ui)
        if arch in Arch.from_arch_field(apkbuild["arch"]):
            ret.append((ui, apkbuild["pkgdesc"]))
    return ret


def check_option(
    ui: str,
    option: str,
    must_exist: bool = True,
    with_extra_repos: WithExtraRepos = WithExtraRepos.DEFAULT,
) -> bool:
    """
    Check if an option, such as pmb:drm, is inside an UI's APKBUILD.

    If must_exist is set to False, False will be returned if the UI doesn't exist.
    """
    if ui == "none":
        # Users can select "none" as UI in "pmbootstrap init", which does not
        # have a UI package.
        return False

    if pmb.parse.device_is_alpine_only():
        # The UI is a plain Alpine package, there is no postmarketos-ui-* one
        # to read pmb: options from
        return False

    pkgname = f"postmarketos-ui-{ui}"
    apkbuild = pmb.helpers.pmaports.get(
        pkgname, must_exist, subpackages=False, with_extra_repos=with_extra_repos
    )
    return option in apkbuild["options"] if apkbuild is not None else False


def alpine_ui_session(ui: str) -> str:
    """
    :returns: the .desktop basename to start for a given Alpine UI, or "" to
              let pmbootstrap guess from the files on disk
    """
    entry = next((e for e in alpine_uis if e.pkgname == ui), None)
    return entry.session if entry else ""
