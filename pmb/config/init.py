# Copyright 2023 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import glob
import heapq
import json
import os
import shutil
import subprocess
import urllib
from pathlib import Path
from typing import Any, Final, NamedTuple

import pmb.aportgen
import pmb.chroot.zap
import pmb.config
import pmb.config.other
import pmb.config.pmaports
import pmb.helpers.cli
import pmb.helpers.devices
import pmb.helpers.git
import pmb.helpers.http
import pmb.helpers.other
import pmb.helpers.pmaports
import pmb.helpers.ui
import pmb.parse._apkbuild
import pmb.parse.deviceinfo
from pmb.config.pmaports import DEVELOPMENT_CHANNEL
from pmb.core import Config
from pmb.core.apk_package import Apkbuild
from pmb.core.chroot import Chroot
from pmb.core.config import ServiceManagerConfig
from pmb.core.context import Context, get_context
from pmb.core.pkgrepo import pkgrepo_default_path
from pmb.helpers import logging
from pmb.helpers.exceptions import NonBugError
from pmb.parse.deviceinfo import Deviceinfo
from pmb.types import PmbArgs


def require_programs() -> None:
    missing = []
    for program in pmb.config.required_programs:
        # Debian: some programs are in /usr/sbin, which is not in PATH
        # unless using sudo
        prog = shutil.which(program, path=pmb.config.host_path)
        if not prog:
            missing.append(program)
        else:
            pmb.config.required_programs[program] = prog

    losetup_missing_json = False

    if "losetup" not in missing:
        # Check if losetup supports the --json argument. Use the absolute path
        # here, so it works in Debian too without using sudo.
        # FIXME: we use subprocess directly here since pmb.helpers.run.user() requires
        # global context to be initialized but we want this to run early in pytest.
        try:
            subprocess.run(
                [pmb.config.required_programs["losetup"], "--json"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        except subprocess.CalledProcessError:
            losetup_missing_json = True

    error_message = ""

    if missing:
        error_message += f"Can't find all programs required to run pmbootstrap. Please install the following: {', '.join(missing)}"

    if missing and losetup_missing_json:
        error_message += "\n\nAdditionally, your"
    elif losetup_missing_json:
        error_message += "Your"

    if losetup_missing_json:
        error_message += " system's losetup implementation is missing --json support. If you are using BusyBox, try installing losetup from util-linux."

    if error_message:
        raise NonBugError(error_message)


def ask_for_username(default_user: str) -> str:
    """
    Ask for a reasonable username for the non-root user.

    :returns: the username
    """
    while True:
        ret = pmb.helpers.cli.ask("Username", None, default_user, False, "[a-z_][a-z0-9_-]*")
        if ret == "root":
            logging.fatal(
                'ERROR: don\'t put "root" here. This is about'
                " creating an additional non-root user. Don't worry,"
                " the root user will also be created ;)"
            )
            continue
        return ret


def ask_for_work_path(default: Path | None) -> tuple[Path, bool]:
    """
    Ask for the work path, until we can create it (when it does not exist) and write into it.

    :returns: (path, exists)
        * path: is the full path, with expanded ~ sign
        * exists: is True when the folder existed before we tested whether we can create it
    """
    logging.info(
        "Location of the 'work' path. Multiple chroots"
        " (native, device arch, device rootfs) will be created"
        " in there."
    )
    while True:
        try:
            work = (
                Path(
                    pmb.helpers.cli.ask(
                        "Work path", None, str(default) if default is not None else default, False
                    )
                )
                .expanduser()
                .resolve()
            )
            exists = work.exists()

            # Work must not be inside the pmbootstrap path
            if work.is_relative_to(pmb.config.pmb_src):
                logging.fatal(
                    "ERROR: The work path must not be inside the pmbootstrap"
                    " path. Please specify another location."
                )
                continue

            # Create the folder with a version file
            work.mkdir(mode=0o700, parents=True, exist_ok=True)

            # If the version file doesn't exists yet because we either just
            # created the work directory or the user has deleted it for
            # whatever reason then we need to write initialize it.
            work_version_file = work / "version"
            if not os.path.isfile(work_version_file):
                work_version_file.write_text(f"{pmb.config.work_version}\n")

            # Create cache_git dir, so it is owned by the host system's user
            # (otherwise pmb.helpers.mount.bind would create it as root)
            work.joinpath("cache_git").mkdir(mode=0o700, parents=True, exist_ok=True)
            return (work, exists)
        except OSError:
            logging.fatal(
                "ERROR: Could not create this folder, or write inside it! Please try again."
            )


def ask_for_pmaports_path(default: Path) -> Path:
    """
    Ask for a path for pmaports, until it can either be created or already exists.

    :returns: full path to the directory, with a leading ~ expanded.
    """
    logging.info("Location of the 'pmaports' path, containing package definitions.")
    while True:
        try:
            raw = os.path.expanduser(
                pmb.helpers.cli.ask(
                    "pmaports path",
                    None,
                    str(default),
                    False,
                )
            )
            path = Path(raw).resolve()

            if path.is_relative_to(pmb.config.pmb_src):
                logging.error(
                    "ERROR: The pmaports path must not be inside the"
                    " pmbootstrap path. Please specify another location."
                )
                continue

            if not path.exists():
                try:
                    path.parent.mkdir(0o700, parents=True, exist_ok=True)
                except (FileExistsError, FileNotFoundError) as e:
                    logging.error("ERROR: Could not create parent directories: %s", str(e))
                    continue

            return path
        except OSError:
            logging.fatal(
                "ERROR: Could not create this folder, or write inside it! Please try again."
            )


def ask_for_channel(config: Config) -> str:
    """
    Ask for the postmarketOS release channel.
    The channel dictates, which pmaports branch pmbootstrap will check out,
    and which repository URLs will be used when initializing chroots.

    :returns: channel name (e.g. "edge", "v21.03")
    """
    channels_cfg = pmb.helpers.git.parse_channels_cfg(pkgrepo_default_path())
    count = len(channels_cfg["channels"])

    # list channels
    logging.info("Choose the postmarketOS release channel.")
    logging.info(f"Available ({count}):")
    # Only show the first 3 releases. This includes edge, the latest supported
    # release plus one. Should be a good solution until new needs arrive when
    # we might want to have a custom channels.cfg attribute.
    for channel, channel_data in list(channels_cfg["channels"].items())[:3]:
        logging.info(f"* {channel}: {channel_data['description']}")

    # Default for first run: "recommended" from channels.cfg
    # Otherwise, if valid: channel from pmaports.cfg of current branch
    # The actual channel name is not saved in pmbootstrap_v3.cfg, because then
    # we would need to sync it with what is checked out in pmaports.git.
    default = pmb.config.pmaports.read_config(add_systemd_prefix=False)["channel"]
    choices = channels_cfg["channels"].keys()
    if config.is_default_channel or default not in choices:
        default = channels_cfg["meta"]["recommended"]

    # Ask until user gives valid channel
    while True:
        ret = pmb.helpers.cli.ask("Channel", None, default, complete=choices)
        if ret in choices:
            return ret
        logging.fatal("ERROR: Invalid channel specified, please type in one from the list above.")


def ask_for_ui(deviceinfo: Deviceinfo) -> str:
    ui_list = pmb.helpers.ui.list_ui(deviceinfo.arch)
    hidden_ui_count = 0
    if not deviceinfo.drm:
        for i in reversed(range(len(ui_list))):
            pkgname = f"postmarketos-ui-{ui_list[i][0]}"
            apkbuild = pmb.helpers.pmaports.get(pkgname, subpackages=False, must_exist=False)
            if apkbuild and (
                "pmb:drm" in apkbuild["options"] or "pmb:gpu-accel" in apkbuild["options"]
            ):
                ui_list.pop(i)
                hidden_ui_count += 1

    # Get default
    default: Any = get_context().config.ui
    if default not in dict(ui_list):
        default = pmb.config.defaults["ui"]

    logging.info(f"Available user interfaces ({len(ui_list) - 1}): ")
    ui_completion_list = []
    for ui in ui_list:
        logging.info(f"* {ui[0]}: {ui[1]}")
        ui_completion_list.append(ui[0])
    if hidden_ui_count > 0:
        logging.info(
            f"NOTE: {hidden_ui_count} UIs are hidden because"
            ' "deviceinfo_drm" is not set (see'
            " https://postmarketos.org/deviceinfo)."
        )
    while True:
        ret = pmb.helpers.cli.ask(
            "User interface", None, default, True, complete=ui_completion_list
        )
        if ret in dict(ui_list):
            return ret
        logging.fatal(
            "ERROR: Invalid user interface specified, please type in one from the list above."
        )


def ask_for_ui_extras(config: Config, ui: str) -> bool:
    apkbuild = pmb.helpers.pmaports.get(
        f"postmarketos-ui-{ui}", subpackages=False, must_exist=False
    )
    if not apkbuild:
        return False

    extra = apkbuild["subpackages"].get(f"postmarketos-ui-{ui}-extras")
    if extra is None:
        return False

    logging.info(f"This user interface has an extra package: {extra['pkgdesc']}")

    return pmb.helpers.cli.confirm("Enable this package?", default=config.ui_extras)


def ask_for_headless(config: Config, deviceinfo: Deviceinfo) -> bool:
    """
    Ask whether the device will be set up without a screen attached.

    Only asked for devices that set deviceinfo_alpine_only, because that is
    where pmbootstrap can configure Wi-Fi at install time. Everything else
    configures the network with NetworkManager after the first boot.
    """
    if not deviceinfo.alpine_only:
        return False

    logging.info(
        "A headless setup has no screen or keyboard attached: the device joins a Wi-Fi network"
        " on the first boot and is administered over SSH."
    )
    headless = pmb.helpers.cli.confirm("Set this device up headless?", default=config.headless)

    if headless:
        logging.info(
            "OK. 'pmbootstrap install' will ask for the user password and for the Wi-Fi network"
            " name and passphrase, and write them into the image."
        )

    return headless


def ask_for_service_manager(config: Config, ui: str) -> ServiceManagerConfig:
    default, available, reason = pmb.config.other.service_managers_from_packaging(ui)

    if len(available) == 1:
        logging.info(f"{default} will be used as service manager ({reason})")
        # The user doesn't have a choice here, so don't change the config. This
        # way, when switching between UIs that have 2 and 1 service manager,
        # the selection will not change when going back to the UI with 2
        # service managers.
        return config.service_manager

    logging.info(f"Based on your UI selection, 'default' will result in choosing {default}.")

    choices = ServiceManagerConfig.choices()
    answer = pmb.helpers.cli.ask(
        "Which service manager should be used?",
        choices,
        str(config.service_manager),
        validation_regex=f"^({'|'.join(choices)})$",
        complete=choices,
    )
    return ServiceManagerConfig(answer)


def ask_for_keymaps(config: Config, deviceinfo: Deviceinfo) -> str:
    if not deviceinfo.keymaps or deviceinfo.keymaps.strip() == "":
        return ""
    options = deviceinfo.keymaps.split(" ")
    logging.info(f"Available keymaps for device ({len(options)}): {', '.join(options)}")
    if config.keymap == "":
        config.keymap = options[0]

    while True:
        ret = pmb.helpers.cli.ask("Keymap", None, config.keymap, True, complete=options)
        if ret in options:
            return ret
        logging.fatal("ERROR: Invalid keymap specified, please type in one from the list above.")


def ask_for_timezone() -> str:
    localtimes = ["/etc/zoneinfo/localtime", "/etc/localtime"]
    zoneinfo_path = "/usr/share/zoneinfo/"
    for localtime in localtimes:
        if not os.path.exists(localtime):
            continue
        tz = ""
        if os.path.exists(localtime):
            tzpath = os.path.realpath(localtime)
            tzpath = tzpath.rstrip()
            if os.path.exists(tzpath):
                with contextlib.suppress(BaseException):
                    _, tz = tzpath.split(zoneinfo_path)
        if tz:
            logging.info(f"Your host timezone: {tz}")
            if pmb.helpers.cli.confirm("Use this timezone instead of GMT?", default=True):
                return tz
    logging.info("WARNING: Unable to determine timezone configuration on host, using GMT.")
    return "GMT"


def ask_for_provider_select(apkbuild: Apkbuild, providers_cfg: dict[str, str]) -> None:
    """
    Ask for selectable providers that are specified using "_pmb_select" in a APKBUILD.

    :param apkbuild: the APKBUILD with the _pmb_select
    :param providers_cfg: the configuration section with previously selected
                          providers. Updated with new providers after selection
    """
    for select in apkbuild["_pmb_select"]:
        providers = pmb.helpers.pmaports.find_providers(select, apkbuild["_pmb_default"])
        provider_count = len(providers)

        if provider_count == 0:
            logging.warning(
                f"WARNING: Skipping provider selection for {select}, no providers found! This is probably a packaging bug."
            )
            continue

        logging.info(f"Available providers for {select} ({provider_count}):")

        has_default = False
        providers_short = {}
        last_selected = providers_cfg.get(select, "default")

        for pkgname, pkg in providers:
            # Strip provider prefix if possible
            short = pkgname
            short = short.removeprefix(f"{select}-")

            # Allow selecting the package using both short and long name
            providers_short[pkgname] = pkgname
            providers_short[short] = pkgname

            if pkgname == last_selected:
                last_selected = short

            if not has_default and pkg.get("provider_priority", 0) != 0:
                # Display as default provider
                styles = pmb.config.styles
                logging.info(
                    f"* {short}: {pkg['pkgdesc']} {styles['BOLD']}(default){styles['END']}"
                )
                has_default = True
            else:
                logging.info(f"* {short}: {pkg['pkgdesc']}")

        while True:
            ret = pmb.helpers.cli.ask(
                "Provider", None, last_selected, True, complete=providers_short.keys()
            )

            if has_default and ret == "default":
                # Selecting default means to not select any provider explicitly
                # In other words, apk chooses it automatically based on
                # "provider_priority"
                providers_cfg.pop(select, None)
                break
            if ret in providers_short:
                providers_cfg[select] = providers_short[ret]
                break
            logging.fatal(
                "ERROR: Invalid provider specified, please type in one from the list above."
            )


def ask_for_provider_select_pkg(pkgname: str, providers_cfg: dict[str, str]) -> None:
    """
    Look up the APKBUILD for the specified pkgname and ask for selectable
    providers that are specified using "_pmb_select".

    :param pkgname: name of the package to search APKBUILD for
    :param providers_cfg: the configuration section with previously selected
                          providers. Updated with new providers after selection
    """
    apkbuild = pmb.helpers.pmaports.get(pkgname, subpackages=False, must_exist=False)
    if not apkbuild:
        return

    ask_for_provider_select(apkbuild, providers_cfg)


def ask_for_device_kernel(config: Config, device: str) -> str:
    """
    Ask for the kernel that should be used with the device.

    :param device: code name, e.g. "lg-mako"

    :returns: None if the kernel is hardcoded in depends without subpackages

    :returns: kernel type ("downstream", "stable", "mainline", ...)

    """
    # Get kernels
    kernels = pmb.parse._apkbuild.kernels(device)
    if not kernels:
        return config.kernel

    # Get default
    default = config.kernel
    if default not in kernels:
        default = next(iter(kernels.keys()))

    # Ask for kernel (extra message when downstream and upstream are available)
    logging.info("Which kernel do you want to use with your device?")
    if "downstream" in kernels:
        logging.info("Downstream kernels are typically the outdated Android kernel forks.")
    if "downstream" in kernels and len(kernels) > 1:
        logging.info(
            "Upstream kernels (mainline, stable, ...) get security updates, but may have less"
            " working features than downstream kernels."
        )

    # list kernels
    logging.info(f"Available kernels ({len(kernels)}):")
    for type in sorted(kernels.keys()):
        logging.info(f"* {type}: {kernels[type]}")
    while True:
        ret = pmb.helpers.cli.ask("Kernel", None, default, True, complete=kernels)
        if ret in kernels:
            return ret
        logging.fatal("ERROR: Invalid kernel specified, please type in one from the list above.")


def ask_for_mainline_downstream() -> pmb.helpers.devices.DeviceCategory:
    logging.info("What type of port are you creating?")
    logging.info(
        "* mainline: Port using upstream/mainline kernel, compatible with upstream user space."
    )
    logging.info(
        "* downstream: Port using downstream kernel, using the original (e.g. Android) kernel "
        "sources, at least partially incompatible with upstream user space."
    )
    port_type = pmb.helpers.cli.ask(
        question="Type?",
        choices=["mainline", "downstream"],
        default=None,
        validation_regex="mainline|downstream",
        complete=["mainline", "downstream"],
    )

    match port_type:
        # Mainline ports start out in the testing category
        case "mainline":
            return pmb.helpers.devices.DeviceCategory.TESTING
        case "downstream":
            return pmb.helpers.devices.DeviceCategory.DOWNSTREAM
        case _:
            raise ValueError(f"Unexpected port_type {port_type}")


def switch_channel(config: Config, channel: str) -> None:
    """
    Wrapper around `pmb.config.pmaports.switch_to_channel_branch(channel)` that also handles
    installing git hooks if main is selected.
    """
    pmb.config.pmaports.switch_to_channel_branch(channel)
    # FIXME: ???
    config.is_default_channel = False

    # Copy the git hooks if main was checked out. (Don't symlink them and
    # only do it on main, so the git hooks don't change unexpectedly when
    # having a random branch checked out.)
    branch_current = pmb.helpers.git.rev_parse(pkgrepo_default_path(), extra_args=["--abbrev-ref"])
    if branch_current == "main":
        logging.info("NOTE: pmaports is on main branch, copying git hooks.")
        pmb.config.pmaports.install_githooks()


class ChosenDevice(NamedTuple):
    device: str
    """<vendor>-<codename> string for device."""
    device_is_new: bool
    """Whether the device just was created."""
    device_path: Path
    """Path to the device's device package directory."""
    kernel: str
    """Type of kernel (downstream, etc)."""


def ask_for_device(context: Context, channel: str) -> ChosenDevice:
    """
    Prompt for the device vendor, model, and kernel.

    :returns: NamedTuple with the relevant metadata.
    """
    styles = pmb.config.styles

    def new_port_message(has_codename: bool = True) -> str:
        """Return the message for new ports."""
        return (
            "If you're trying to select a device that is supported, check the following:\n\n"
            f"* Make sure you spelled the vendor name{' and codename' if has_codename else ''} correctly. \n"
            "* Check if you're on the right release branch. Devices in the 'downstream' category are only"
            " available on the 'edge' branch; if you selected a stable branch, run pmbootstrap init again"
            " and select 'edge'.\n"
            "* If the device is supported by a generic port, type in the vendor and codename of the generic port.\n\n"
            "If you want to create a new device port, follow the guide at <https://postmarketos.org/porting/>."
        )

    vendors = sorted(pmb.helpers.devices.list_vendors())
    logging.info(
        "Choose your target device vendor (either an existing one, or a new one for porting)."
    )
    logging.info(f"Available vendors ({len(vendors)}): {', '.join(vendors)}")

    current_vendor = None
    current_codename = None
    if context.config.device:
        current_vendor = context.config.device.split("-", 1)[0]
        current_codename = context.config.device.split("-", 1)[1]

    device_is_new = False

    while True:
        vendor = pmb.helpers.cli.ask("Vendor", None, current_vendor, False, r"[a-z0-9]+", vendors)

        new_vendor = vendor not in vendors
        codenames = []
        if new_vendor:
            logging.info(
                f"{styles['BOLD']}The specified vendor ({vendor}) could not be found in existing ports.{styles['END']}\n"
                + new_port_message(has_codename=False)
            )
            if not pmb.helpers.cli.confirm(
                "Do you want to create a new device port?", default=True
            ):
                continue
        else:
            device_list = "Devices are categorised as follows, from best to worst:\n"
            for category in pmb.helpers.devices.DeviceCategory.shown():
                device_list += f"* {category.color()}{str(category).capitalize()}{styles['END']}: {category.explain()}.\n"
            device_entries = pmb.helpers.devices.list_codenames(vendor)
            # Sort devices alphabetically.
            device_entries = sorted(
                device_entries, key=pmb.helpers.devices.DeviceEntry.codename_without_vendor
            )
            device_count = len(device_entries)
            device_list += f"\nAvailable devices by codename ({device_count}): "
            device_strings = []
            for device_entry in device_entries:
                codenames.append(device_entry.codename_without_vendor())
                device_strings.append(str(device_entry))

            device_list += ", ".join(device_strings)
            logging.info(device_list)

        if current_vendor != vendor:
            current_codename = ""
        codename = pmb.helpers.cli.ask(
            "Device codename", None, current_codename, False, r"[a-z0-9\._-]+", codenames
        )

        device = f"{vendor}-{codename}"
        device_path = pmb.helpers.devices.find_path(device, "deviceinfo")
        device_category = None
        if device_path is not None:
            device_category = pmb.helpers.devices.get_device_category_by_apkbuild_path(device_path)
        if device_path is None:
            if device == context.config.device:
                raise NonBugError(
                    "This device does not exist anymore, check <https://postmarketos.org/renamed>"
                    " to see if it was renamed"
                )

            channel_is_improper = channel != DEVELOPMENT_CHANNEL

            logging.info(
                f"{styles['BOLD']}The specified device ('{device}') could not be found in existing ports.{styles['END']}\n"
                + new_port_message(has_codename=True)
            )
            if channel_is_improper:
                logging.info(
                    f"Earlier you selected the '{channel}' channel. All new ports and packages must "
                    f"go through '{DEVELOPMENT_CHANNEL}', so it will be switched to if you confirm."
                )
            if not pmb.helpers.cli.confirm(
                "Do you want to create a new device port?", default=True
            ):
                current_vendor = vendor
                continue
            if channel_is_improper:
                switch_channel(context.config, DEVELOPMENT_CHANNEL)

            device_category = ask_for_mainline_downstream()

            # New port creation confirmed
            logging.info(f"Generating new aports for: {device}...")
            device_is_new = True
            device_dir = pmb.aportgen.generate(f"device-{device}", device_category=device_category)
            device_path = device_dir / "deviceinfo"
            if device_category == pmb.helpers.devices.DeviceCategory.DOWNSTREAM:
                pmb.aportgen.generate(f"linux-{device}", device_category=device_category)
        elif device_category == pmb.helpers.devices.DeviceCategory.ARCHIVED:
            apkbuild = device_path.parent / "APKBUILD"
            archived = pmb.parse._apkbuild.archived(apkbuild) or "No reason given (this is a bug)"
            logging.info(f"WARNING: {device} is archived: {archived}")
            if not pmb.helpers.cli.confirm():
                continue
        elif device_category == pmb.helpers.devices.DeviceCategory.DOWNSTREAM:
            logging.info(
                f"WARNING: {device} is a downstream port! Expect missing and broken features. See https://wiki.postmarketos.org/wiki/Downstream_kernel_specific_package"
            )
            if not pmb.helpers.cli.confirm():
                continue
        break

    kernel = ask_for_device_kernel(context.config, device)
    return ChosenDevice(device, device_is_new, device_path, kernel)


def ask_for_additional_options(config: Config) -> None:
    context = pmb.core.context.get_context()
    # Allow to skip additional options
    logging.info(
        "Additional options:"
        f" extra free space: {config.extra_space} MB,"
        f" boot partition size: {config.boot_size} MB,"
        f" parallel jobs: {config.jobs},"
        f" ccache per arch: {config.ccache_size},"
        f" sudo timer: {context.sudo_timer},"
        f" mirror: {config.mirrors['pmaports']}"
    )

    if not pmb.helpers.cli.confirm("Change them?", default=False):
        return

    # Extra space
    logging.info(
        "Set extra free space to 0, unless you ran into a 'No space left on device' error. In that"
        " case, the size of the rootfs could not be calculated properly on your machine, and we"
        " need to add extra free space to make the image big enough to fit the rootfs"
        " (pmbootstrap#1904). How much extra free space do you want to add to the image (in MB)?"
    )
    # TODO: The __setattr__ implementation in Config does handle the conversions here,
    # but mypy doesn't understand this (yet?), so we have to do it explicitly.
    answer = pmb.helpers.cli.ask(
        "Extra space size", None, config.extra_space, validation_regex="^[0-9]+$"
    )
    config.extra_space = int(answer)

    # Boot size
    logging.info("What should be the boot partition size (in MB)?")
    answer = pmb.helpers.cli.ask(
        "Boot size", None, config.boot_size, validation_regex="^[1-9][0-9]*$"
    )
    config.boot_size = int(answer)

    # Parallel job count
    logging.info("How many jobs should run parallel on this machine, when compiling?")
    answer = pmb.helpers.cli.ask("Jobs", None, config.jobs, validation_regex="^[1-9][0-9]*$")
    config.jobs = int(answer)

    # Ccache size
    logging.info(
        "We use ccache to speed up building the same code multiple times. How much space should the"
        " ccache folder take up per architecture? After init is through, you can check the current"
        " usage with 'pmbootstrap stats'. Answer with 0 for infinite."
    )
    regex = "0|[0-9]+(k|M|G|T|Ki|Mi|Gi|Ti)"
    answer = pmb.helpers.cli.ask(
        "Ccache size", None, config.ccache_size, lowercase_answer=False, validation_regex=regex
    )
    config.ccache_size = answer

    # Sudo timer
    logging.info(
        "pmbootstrap does everything in Alpine Linux chroots, so your host system does not get"
        " modified. In order to work with these chroots, pmbootstrap calls 'sudo' internally. For"
        " long running operations, it is possible that you'll have to authorize sudo more than"
        " once."
    )
    answer_background_timer = pmb.helpers.cli.confirm(
        "Enable background timer to prevent repeated sudo authorization?",
        default=context.sudo_timer,
    )
    config.sudo_timer = answer_background_timer

    # Mirrors
    # prompt for mirror change
    logging.info(f"Selected mirror: {context.config.mirrors['pmaports']}")
    if pmb.helpers.cli.confirm("Change mirror?", default=False):
        mirror = ask_for_mirror()
        config.mirrors["pmaports"] = mirror
        config.mirrors["systemd"] = os.path.join(mirror, "extra-repos/systemd/")


def ask_for_mirror() -> str:
    regex = "^(?:[1-9][0-9]*|best)$"  # single non-zero number only

    s = pmb.helpers.http.download(
        "https://postmarketos.org/mirrors.json", "pmos_mirrors", cache=False
    ).read_text()

    logging.info("list of available mirrors:")
    mirrors = json.loads(s)
    keys = mirrors.keys()
    for i, key in enumerate(keys, start=1):
        logging.info(f"[{i}]\t{key} ({mirrors[key]['location']})")
    logging.info("choose 'best' to select the one closest to you")

    urls = []
    for key in keys:
        # accept only http:// or https:// urls
        http_count = 0  # remember if we saw any http:// only URLs
        link_list = []
        for k in mirrors[key]["urls"]:
            if k.startswith("http"):
                link_list.append(k)
            if k.startswith("http://"):
                http_count += 1
        # remove all https urls if there is more that one URL and one of
        #     them was http://
        if http_count > 0 and len(link_list) > 1:
            link_list = [k for k in link_list if not k.startswith("https")]
        if len(link_list) > 0:
            urls.append(link_list[0])

    mirror_index = "best"
    mirror = get_context().config.mirrors["pmaports"]
    for i in range(len(urls)):
        if urls[i] == mirror:
            mirror_index = str(i + 1)
            break
    mirror = ""
    # require one valid mirror index selected by user
    while len(mirror) == 0:
        answer = pmb.helpers.cli.ask("Select a mirror", None, mirror_index, validation_regex=regex)
        if answer == "best":
            timings: list[tuple[float, str]] = []
            # determine the best available mirror
            for url in urls:
                try:
                    heapq.heappush(timings, (pmb.helpers.http.measure_latency(url), url))
                except urllib.error.HTTPError:
                    logging.warning(f"{url} was unavailable, skipping!")
                    continue
            try:
                latency, mirror = timings[0]
                logging.info(
                    f"Best mirror was {mirror} with a latency of {round(latency * 1000, 2)}ms"
                )
            except IndexError:
                logging.error(
                    "No mirror was available! Please check your internet connection. Falling back to the main mirror"
                )
                mirror = urls[0]
        else:
            i = int(answer)
            if i < 1 or i > len(urls):
                logging.info("You must select one valid mirror!")
            mirror = urls[i - 1]

    return mirror


def ask_for_hostname(default: str | None, device: str) -> str:
    if device:
        device = pmb.helpers.other.normalize_hostname(device)
    while True:
        ret = pmb.helpers.cli.ask(
            "Device hostname (short form, e.g. 'foo')", None, (default or device), True
        )
        if not pmb.helpers.other.validate_hostname(ret):
            continue
        # Don't store device name in user's config (gets replaced in install)
        if ret == device:
            return ""
        return ret


def ask_for_ssh_keys(ssh_key_glob: str, default: bool) -> bool:
    keys = glob.glob(os.path.expanduser(ssh_key_glob))
    if not keys:
        logging.info("NOTE: No SSH public keys found to copy to the device.")
        logging.info("See https://postmarketos.org/ssh-key-glob for more information.")
        return False
    logging.info(f"SSH public keys found ({len(keys)}):")
    for key in keys:
        logging.info(f"* {key}")
    logging.info("See https://postmarketos.org/ssh-key-glob for more information.")
    return pmb.helpers.cli.confirm(
        "Would you like to copy these public keys to the device?", default=default
    )


def ask_build_pkgs_on_install(default: bool) -> bool:
    logging.info(
        "After pmaports are changed, the binary packages may be outdated. If you want to install"
        " postmarketOS without changes, reply 'n' for a faster installation."
    )
    return pmb.helpers.cli.confirm(
        "Build outdated packages during 'pmbootstrap install'?", default=default
    )


def get_locales() -> list[str]:
    ret = []
    list_path = pmb.config.pmb_src / "pmb/data/locales"
    with open(list_path) as handle:
        for line in handle:
            ret += [line.rstrip()]
    return ret


def ask_for_locale(current_locale: str) -> str:
    locales = get_locales()
    logging.info(
        "Choose your preferred locale, like e.g. en_US. Only UTF-8 is supported, it gets appended"
        " automatically. Use tab-completion if needed."
    )

    while True:
        ret = pmb.helpers.cli.ask(
            "Locale",
            choices=None,
            default=current_locale.replace(".UTF-8", ""),
            lowercase_answer=False,
            complete=locales,
        )
        ret = ret.replace(".UTF-8", "")
        if ret not in locales:
            logging.info("WARNING: this locale is not in the list of known valid locales.")
            if pmb.helpers.cli.ask() != "y":
                # Ask again
                continue

        return f"{ret}.UTF-8"


def print_systemd_warning(device_is_new: bool, apkbuild: Apkbuild, kernel: str) -> None:
    kernel_version = None
    if not device_is_new:
        linuxdep = next((pkg for pkg in apkbuild["depends"] if pkg.startswith("linux-")), None)
        if linuxdep is None:
            kernel_subpkg = next(
                subpkg for subpkg in apkbuild["subpackages"] if subpkg.endswith("-" + kernel)
            )
            linuxdep = next(
                (
                    dep
                    for dep in apkbuild["subpackages"][kernel_subpkg]["depends"]
                    if dep.startswith("linux-")
                ),
                None,
            )
        if linuxdep:
            linux_apkbuild = pmb.helpers.pmaports.get(linuxdep, must_exist=False)
            if linux_apkbuild:
                kernel_version = linux_apkbuild["pkgver"]

    pmaports_cfg = pmb.config.pmaports.read_config()
    systemd_req = pmaports_cfg.get("systemd_linux_min_version", "5.4")
    systemd_recommended = pmaports_cfg.get("systemd_linux_recommended_version", "5.7")
    systemd_warning = device_is_new or (
        kernel_version
        and pmb.parse.version.compare(
            kernel_version,
            systemd_recommended,
        )
        == -1
    )
    if systemd_warning:
        warning_text = ""
        if kernel_version is None:
            warning_text = (
                f"WARNING: systemd requires kernel version {systemd_req}."
                + " Installing systemd with older kernel may result in non-bootable system."
            )
        elif pmb.parse.version.compare(kernel_version, systemd_req) == -1:
            warning_text = (
                f"WARNING: Kernel version {kernel_version} "
                + f"is lower than systemd's minimal requirement ({systemd_req})."
                + " Choosing systemd may result in non-bootable system."
            )
        else:
            warning_text = (
                f"WARNING: Kernel version {kernel_version} "
                + f"is lower than systemd's recommended kernel version ({systemd_recommended}). "
                + "Some systemd features may be not available."
            )
        systemd_readme = "https://github.com/systemd/systemd/blob/main/README"
        warning_text += f" Get more information for systemd requirements at {systemd_readme}"
        logging.warning(warning_text)


PACKAGE_SPEC_PATTERN: Final[str] = r"[-.+:\w]+"


def frontend(args: PmbArgs) -> None:
    # Work folder (needs to be first, so we can create chroots early)
    config = get_context().config

    using_default_pmaports = config.aports.is_relative_to(config.work)

    config.work, work_exists = ask_for_work_path(config.work)

    # If the work dir changed then we need to update the pmaports path
    # to be relative to the new workdir
    if using_default_pmaports:
        config.aports = config.work / "cache_git/pmaports"

    config.aports = ask_for_pmaports_path(config.aports)

    # Update args and save config (so chroots and 'pmbootstrap log' work)
    # pmb.helpers.args.update_work(args, config.work)
    pmb.config.save(args.config, config)

    # Migrate work dir if necessary
    pmb.helpers.other.migrate_work_folder()

    # Clone pmaports
    pmb.config.pmaports.init(args.shallow_initial_clone)

    # Choose release channel, possibly switch pmaports branch
    channel = ask_for_channel(config)
    switch_channel(config, channel)

    # Device
    device, device_is_new, deviceinfo_path, kernel = ask_for_device(get_context(), channel)
    config.device = device
    config.kernel = kernel

    deviceinfo = pmb.parse.deviceinfo(device)
    apkbuild = pmb.parse.apkbuild(deviceinfo_path.parent / "APKBUILD")
    ask_for_provider_select(apkbuild, config.providers)

    # Device keymap
    config.keymap = ask_for_keymaps(config, deviceinfo)

    config.user = ask_for_username(config.user)
    ask_for_provider_select_pkg("postmarketos-base", config.providers)
    ask_for_provider_select_pkg("postmarketos-base-ui", config.providers)
    ask_for_provider_select_pkg("postmarketos-usb-moded", config.providers)

    # UI and various build options
    ui = ask_for_ui(deviceinfo)
    config.ui = ui
    config.ui_extras = ask_for_ui_extras(config, ui)

    config.headless = ask_for_headless(config, deviceinfo)

    # service manager
    print_systemd_warning(device_is_new, apkbuild, config.kernel)
    config.service_manager = ask_for_service_manager(config, ui)

    ask_for_provider_select_pkg(f"postmarketos-ui-{ui}", config.providers)
    ask_for_additional_options(config)

    # Extra packages to be installed to rootfs
    logging.info(
        "Additional packages that will be installed to rootfs. Specify them in a comma separated"
        ' list (e.g.: vim,file) or "none"'
    )
    extra = pmb.helpers.cli.ask(
        "Extra packages",
        None,
        config.extra_packages,
        validation_regex=rf"^({PACKAGE_SPEC_PATTERN})(,{PACKAGE_SPEC_PATTERN})*$",
    )
    config.extra_packages = extra

    # Configure timezone info
    config.timezone = ask_for_timezone()

    # Locale
    config.locale = ask_for_locale(config.locale)

    # Hostname
    config.hostname = ask_for_hostname(config.hostname, device)

    # SSH keys
    config.ssh_keys = ask_for_ssh_keys(config.ssh_key_glob, config.ssh_keys)

    # pmaports path (if users change it with: 'pmbootstrap --aports=... init')
    config.aports = get_context().config.aports

    # Build outdated packages in pmbootstrap install
    config.build_pkgs_on_install = ask_build_pkgs_on_install(config.build_pkgs_on_install)

    # Save config
    pmb.config.save(args.config, config)

    # Zap existing chroots
    if (
        work_exists
        and len(list(Chroot.iter_patterns()))
        and pmb.helpers.cli.confirm("Zap existing chroots to apply configuration?", default=True)
    ):
        args.deviceinfo = deviceinfo

        # Do not zap any existing packages or cache_http directories
        pmb.chroot.zap(confirm=False)

    logging.info("DONE!")
