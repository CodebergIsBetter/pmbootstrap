# Copyright 2023 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
import glob
import os
import re
import shlex
import string
import sys
from collections.abc import Sequence
from pathlib import Path

import pmb.build
import pmb.chroot
import pmb.chroot.apk
import pmb.chroot.initfs
import pmb.config
import pmb.config.pmaports
import pmb.helpers.devices
import pmb.helpers.other
import pmb.helpers.run
import pmb.helpers.ui
import pmb.install
import pmb.install.blockdevice
import pmb.install.recovery
import pmb.install.ui
from pmb.core import Chroot, ChrootType, Config
from pmb.core.arch import Arch
from pmb.core.context import get_context
from pmb.helpers import logging
from pmb.helpers.exceptions import NonBugError
from pmb.helpers.locale import get_xkb_layout
from pmb.helpers.mount import mount_device_rootfs
from pmb.parse.deviceinfo import Deviceinfo
from pmb.types import PartitionLayout, RunOutputTypeDefault

# Keep track of the packages we already visited in get_recommends() to avoid
# infinite recursion
get_recommends_visited: list[str] = []
get_selected_providers_visited: list[str] = []


def get_subpartitions_size(chroot: Chroot) -> tuple[int, int]:
    """
    Calculate the size of the boot and root subpartition.

    :param suffix: the chroot suffix, e.g. "rootfs_qemu-amd64"
    :returns: (boot, root) the size of the boot and root
              partition as integer in MiB
    """
    config = get_context().config
    boot = int(config.boot_size)

    # Estimate root partition size, then add some free space. The size
    # calculation is not as trivial as one may think, and depending on the
    # file system etc it seems to be just impossible to get it right.
    root = pmb.helpers.other.folder_size(chroot.path) / 1024
    root *= 1.20
    root += 50 + int(config.extra_space)
    return (boot, round(root))


def get_nonfree_packages(device: str) -> list[str]:
    """
    Get any legacy non-free subpackages in the APKBUILD.
    Also see: https://postmarketos.org/edge/2024/02/15/default-nonfree-fw/

    :returns: list of non-free packages to be installed. Example:
              ["device-nokia-n900-nonfree-firmware"]
    """
    # Read subpackages
    device_path = pmb.helpers.devices.find_path(device, "APKBUILD")
    if not device_path:
        raise RuntimeError(f"Device package not found for {device}")

    apkbuild = pmb.parse.apkbuild(device_path)
    subpackages = apkbuild["subpackages"]

    # Check for firmware and userland
    ret = []
    prefix = "device-" + device + "-nonfree-"
    if prefix + "firmware" in subpackages:
        ret += [prefix + "firmware"]
    if prefix + "userland" in subpackages:
        ret += [prefix + "userland"]
    return ret


def get_kernel_package(config: Config) -> list[str]:
    """
    Get the device's kernel subpackage based on the user's choice in
    "pmbootstrap init".

    :param device: code name, e.g. "sony-amami"
    :returns: [] or the package in a list, e.g.
              ["device-sony-amami-kernel-mainline"]
    """
    # Empty list: single kernel devices / "none" selected
    kernels = pmb.parse._apkbuild.kernels(config.device)
    if not kernels or config.kernel == "none":
        return []

    # Sanity check
    if config.kernel not in kernels:
        raise NonBugError(
            "Selected kernel (" + config.kernel + ") is not"
            " valid for device " + config.device + ". Please"
            " run 'pmbootstrap init' to select a valid kernel."
        )

    # Selected kernel subpackage
    return ["device-" + config.device + "-kernel-" + config.kernel]


def copy_files_from_chroot(chroot: Chroot, rsync: bool, verbose: bool) -> None:
    """
    Copy all files from the rootfs chroot to /mnt/install, except
    for the home folder (because /home will contain some empty
    mountpoint folders).

    :param suffix: the chroot suffix, e.g. "rootfs_qemu-amd64"
    """
    # Mount the device rootfs
    logging.info(f"(native) copy {chroot} to /mnt/install/")
    mountpoint = mount_device_rootfs(chroot)
    mountpoint_outside = Chroot.native() / mountpoint

    # Remove empty qemu-user binary stub (where the binary was bind-mounted)
    arch_qemu = pmb.parse.deviceinfo().arch.qemu_user()
    qemu_binary = mountpoint_outside / f"usr/bin/qemu-{arch_qemu}-static"
    if os.path.exists(qemu_binary):
        pmb.helpers.run.root(["rm", qemu_binary])

    # Remove apk progress fifo
    fifo = chroot / "tmp/apk_progress_fifo"
    if os.path.exists(fifo):
        pmb.helpers.run.root(["rm", fifo])

    # Get all folders inside the device rootfs (except for home)
    folders: list[str] = []
    for path in mountpoint_outside.glob("*"):
        if path.name == "home":
            continue
        folders.append(path.name)

    # Update or copy all files
    if rsync:
        pmb.chroot.apk.install(["rsync"], Chroot.native())
        rsync_flags = "-a"
        if verbose:
            rsync_flags += "vP"
        pmb.chroot.root(
            ["rsync", rsync_flags, "--delete", *folders, "/mnt/install/"], working_dir=mountpoint
        )
        pmb.chroot.root(["rm", "-rf", "/mnt/install/home"])
    else:
        pmb.chroot.root(["cp", "-a", *folders, "/mnt/install/"], working_dir=mountpoint)

    # Log how much space and inodes we have used
    pmb.chroot.user(["df", "-h", "/mnt/install"])
    pmb.chroot.user(["df", "-i", "/mnt/install"])


def create_home_from_skel(filesystem: str, user: str) -> None:
    """Create /home/{user} from /etc/skel"""
    rootfs = Chroot.native() / "mnt/install"
    # In btrfs, home subvol & home dir is created in format.py
    if filesystem != "btrfs":
        pmb.helpers.run.root(["mkdir", rootfs / "home"])

    home = rootfs / "home" / user
    if (rootfs / "etc/skel").exists():
        pmb.helpers.run.root(["cp", "-a", (rootfs / "etc/skel"), home])
    else:
        pmb.helpers.run.root(["mkdir", home])
    pmb.helpers.run.root(["chown", "-R", "10000", home])


def write_cmdline_txt(
    layout: PartitionLayout | None,
    chroot: Chroot,
    filesystem: str,
    full_disk_encryption: bool,
) -> None:
    """
    Write /boot/cmdline.txt for devices that are installed from Alpine only.

    On a regular postmarketOS install this is done by boot-deploy (which reads
    deviceinfo_generate_cmdline_txt), but boot-deploy is a postmarketOS package
    that alpine_only devices don't install. The kernel command line is
    assembled the same way boot-deploy does it: the fragments that device
    packages drop into /usr/lib/kernel-cmdline.d/, plus deviceinfo_kernel_cmdline,
    plus the root device that we only know after partitioning.

    :param layout: partition layout from get_partition_layout() or None
    """
    root_dev = Path(f"/dev/installp{layout['root']}") if layout else Path("/dev/install")
    root = "/dev/mapper/root" if full_disk_encryption else f"UUID={get_uuid(root_dev)}"

    args: list[str] = []
    cmdline_d = chroot / "usr/lib/kernel-cmdline.d"
    if cmdline_d.exists():
        for fragment in sorted(cmdline_d.glob("*.conf")):
            args += fragment.read_text().split()

    deviceinfo = pmb.parse.deviceinfo()
    if deviceinfo.kernel_cmdline:
        args += deviceinfo.kernel_cmdline.split()

    args += [f"root={root}", f"rootfstype={pmb.install.get_root_filesystem(filesystem)}", "rw"]

    cmdline = " ".join(args) + "\n"
    logging.info(f"({chroot}) write /boot/cmdline.txt")
    logging.debug(f"cmdline.txt: {cmdline.strip()}")
    (chroot / "tmp/cmdline.txt").write_text(cmdline)
    pmb.chroot.root(["mv", "/tmp/cmdline.txt", "/boot/cmdline.txt"], chroot)
    # The file was created by the (non-root) pmbootstrap process, so it is owned
    # by the host user. Copying that onto a FAT boot partition fails with
    # "cp: can't preserve ownership of ...: Operation not permitted".
    pmb.chroot.root(["chown", "root:root", "/boot/cmdline.txt"], chroot)
    pmb.chroot.root(["chmod", "644", "/boot/cmdline.txt"], chroot)


def setup_tinydm(chroot: Chroot, ui: str, user: str) -> None:
    """
    Autostart the selected UI with tinydm, for devices that set
    deviceinfo_alpine_only.

    On a regular postmarketOS install the postmarketos-ui-* package does this in
    its post-install script. Those packages depend on postmarketos-base-ui and
    can therefore not be used here, so pmbootstrap wires up the Alpine
    equivalent itself.

    :param ui: the selected UI, which is an Alpine package name here
    :param user: user to log in automatically
    """
    # Both directories are searched because the UI may be an X11 one
    sessions = pmb.chroot.root(
        [
            "sh",
            "-c",
            (
                "for f in /usr/share/wayland-sessions/*.desktop"
                " /usr/share/xsessions/*.desktop; do"
                ' [ -e "$f" ] && echo "$f"; done'
            ),
        ],
        chroot,
        output_return=True,
        check=False,
    ).split()

    if not sessions:
        logging.warning(
            f"WARNING: {ui} does not install a session file in /usr/share/wayland-sessions"
            " or /usr/share/xsessions, so it will not be started automatically."
            " Start it by hand after logging in."
        )
        return

    # Several packages ship a session file that is not named after the package,
    # and some ship both an X11 and a Wayland one, so the table can name the
    # right one explicitly. Fall back to matching the UI name, then to the
    # first file found.
    wanted = pmb.helpers.ui.alpine_ui_session(ui)
    session = ""
    if wanted:
        session = next((s for s in sessions if Path(s).name == wanted), "")
        if not session:
            logging.warning(
                f"WARNING: {ui} was expected to install {wanted}, but it did not."
                " Falling back to whichever session file is present."
            )
    if not session:
        session = next((s for s in sessions if Path(s).stem == ui), sessions[0])

    if "/xsessions/" in session:
        # tinydm-run-session needs xinit to start an X11 session
        pmb.chroot.apk.install(["tinydm-x11"], chroot)

        # Alpine's Xorg is the setuid Xorg.wrap, which drops root when every
        # DRM card is a KMS device - which is the case on a board whose only
        # GPU is a KMS driver. Unprivileged Xorg then cannot open the VT,
        # because autologin does not chown it to the user, and dies with
        # "xf86OpenConsole: Cannot open virtual console". Alpine's Xorg has no
        # logind support compiled in, so keeping root is the only option.
        # postmarketos-base-ui ships the identical one-line file.
        pmb.chroot.root(["mkdir", "-p", "/etc/X11"], chroot)
        pmb.chroot.root(
            ["sh", "-c", "echo needs_root_rights=yes > /etc/X11/Xwrapper.config"], chroot
        )

    logging.info(f"({chroot}) autostart {Path(session).stem} with tinydm")
    pmb.chroot.root(["tinydm-set-session", "-f", "-s", session], chroot)
    pmb.chroot.root(["rc-update", "add", "tinydm", "default"], chroot, check=False)

    # tinydm autologins AUTOLOGIN_UID, which defaults to 1000. set_user()
    # creates the user with a different UID, so point tinydm at the real one.
    uid = pmb.chroot.root(["id", "-u", user], chroot, output_return=True).strip()
    conf = "/etc/conf.d/tinydm"
    pmb.chroot.root(
        [
            "sh",
            "-c",
            (
                f"if grep -q '^AUTOLOGIN_UID=' {conf}; then"
                f" sed -i 's/^AUTOLOGIN_UID=.*/AUTOLOGIN_UID={uid}/' {conf};"
                f" else echo 'AUTOLOGIN_UID={uid}' >> {conf}; fi"
            ),
        ],
        chroot,
    )

    # tinydm's own init script only declares 'want dbus elogind'. There is no
    # elogind here, and nothing orders it after seatd, so both land in the
    # default runlevel unordered and the compositor can start before seatd is
    # listening. Express the real dependency with OpenRC's rc_need override.
    pmb.chroot.root(
        [
            "sh",
            "-c",
            (
                f"if grep -q '^rc_need=' {conf}; then"
                f" sed -i 's/^rc_need=.*/rc_need=\"seatd dbus\"/' {conf};"
                f" else echo 'rc_need=\"seatd dbus\"' >> {conf}; fi"
            ),
        ],
        chroot,
    )

    # A wayland compositor needs to talk to seatd, and to reach /dev/dri and
    # /dev/input. The device package enables seatd itself.
    #
    # pipewire: Alpine's pipewire package ships
    # /etc/security/limits.d/25-pw-rlimits.conf, which grants rtprio, nice and
    # memlock to members of a "pipewire" group that is created empty. pam_limits
    # is already in the session stack, so without adding the user the limits
    # match nobody: the data thread runs SCHED_OTHER at nice 0 on a
    # single-core ARM1176 that is also compositing, logs "Can't set realtime
    # priority", and audio crackles under any load.
    for group in ["seat", "video", "input", "pipewire"]:
        pmb.chroot.root(["addgroup", user, group], chroot, check=False)


def setup_wifi(chroot: Chroot, ssid: str, psk: str | None, country: str = "US") -> None:
    """
    Write /etc/wpa_supplicant/wpa_supplicant.conf, so that a headless device
    joins a Wi-Fi network on the very first boot and can be reached over SSH
    without ever attaching a screen.

    :param ssid: name of the network
    :param psk: passphrase (8-63 characters) or a 64 character hex PSK. None or
                empty for an open network.
    """
    if not pmb.parse.deviceinfo().alpine_only:
        raise NonBugError(
            "--wifi-ssid is only implemented for devices with deviceinfo_alpine_only."
            " Other devices use NetworkManager, which ignores wpa_supplicant.conf."
        )

    # The value goes into a quoted string in a config file, so refuse anything
    # that could break out of it
    if '"' in ssid or "\\" in ssid or "\n" in ssid:
        raise NonBugError(f"--wifi-ssid must not contain a quote or a backslash: {ssid}")

    if not psk:
        logging.warning(
            f"WARNING: no passphrase for '{ssid}', configuring it as an open network."
            " If that network is in fact encrypted, the device will not be able to join it."
        )
        network = f'network={{\n\tssid="{ssid}"\n\tkey_mgmt=NONE\n\tscan_ssid=1\n}}\n'
    elif len(psk) == 64 and all(c in string.hexdigits for c in psk):
        network = f'network={{\n\tssid="{ssid}"\n\tpsk={psk}\n\tscan_ssid=1\n}}\n'
    else:
        if not 8 <= len(psk) <= 63:
            raise NonBugError(
                "--wifi-psk must be a passphrase of 8 to 63 characters, or a 64 character"
                f" hex PSK (got {len(psk)} characters)"
            )
        # Hash it, so the plaintext passphrase does not end up in the image.
        # wpa_passphrase prints a whole network block, but it also repeats the
        # plaintext in a "#psk=" comment, so take only the hash out of it.
        out = pmb.chroot.root(["wpa_passphrase", ssid, psk], chroot, output_return=True)
        psk_hash = ""
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("psk="):
                psk_hash = line[len("psk=") :]
                break
        if not psk_hash:
            raise RuntimeError(f"wpa_passphrase did not return a PSK:\n{out}")
        network = f'network={{\n\tssid="{ssid}"\n\tpsk={psk_hash}\n\tscan_ssid=1\n}}\n'

    if not re.fullmatch(r"[A-Za-z]{2}", country or ""):
        raise NonBugError(f"--wifi-country must be a two letter ISO 3166-1 code, got: {country}")

    config = (
        "# Generated by pmbootstrap. Edit /boot/wpa_supplicant.conf on the FAT\n"
        "# boot partition to change the network without reflashing.\n"
        f"country={country.upper()}\n"
        "ctrl_interface=/var/run/wpa_supplicant\n"
        "ctrl_interface_group=wheel\n"
        "update_config=1\n\n" + network
    )

    logging.info(f"({chroot}) configure wifi network '{ssid}'")
    (chroot / "tmp/wpa_supplicant.conf").write_text(config)
    pmb.chroot.root(["mkdir", "-p", "/etc/wpa_supplicant"], chroot)
    pmb.chroot.root(
        [
            "install",
            "-Dm600",
            "/tmp/wpa_supplicant.conf",
            "/etc/wpa_supplicant/wpa_supplicant.conf",
        ],
        chroot,
    )
    pmb.chroot.root(["rm", "/tmp/wpa_supplicant.conf"], chroot)

    # Same file on the FAT boot partition, so the network can be changed later
    # from any computer that can read an SD card
    pmb.chroot.root(
        [
            "install",
            "-Dm600",
            "/etc/wpa_supplicant/wpa_supplicant.conf",
            "/boot/wpa_supplicant.conf",
        ],
        chroot,
    )


def configure_apk(install_local_pkgs: bool) -> None:
    """
    Copy over all official keys, and the keys used to compile local packages
    (unless --no-local-pkgs is set). Then copy the corresponding APKINDEX files
    and remove the /mnt/pmbootstrap/packages repository.
    """
    # Official keys
    keys_dir = pmb.config.apk_keys_path

    # Official keys + local keys
    if install_local_pkgs:
        keys_dir = get_context().config.work / "config_apk_keys"

    alpine_only = pmb.parse.deviceinfo().alpine_only

    # Copy over keys. For alpine_only devices, deliberately leave out
    # build.postmarketos.org.rsa.pub: the rootfs must not trust (and cannot
    # reach) the postmarketOS binary repository.
    rootfs = Chroot.native() / "mnt/install"
    for key in keys_dir.glob("alpine-devel@*.rsa.pub" if alpine_only else "*.pub"):
        pmb.helpers.run.root(["cp", key, rootfs / "etc/apk/keys/"])
    if alpine_only and install_local_pkgs:
        # ...but the locally built device package still needs its key
        for key in (get_context().config.work / "config_abuild").glob("*.pub"):
            pmb.helpers.run.root(["cp", key, rootfs / "etc/apk/keys/"])

    # Copy over the corresponding APKINDEX files from cache
    index_files = pmb.helpers.repo.apkindex_files(
        arch=pmb.parse.deviceinfo().arch, user_repository=False, alpine_only=alpine_only
    )
    for f in index_files:
        pmb.helpers.run.root(["cp", f, rootfs / "var/cache/apk/"])

    # Disable pmbootstrap repository
    pmb.chroot.root(
        ["sed", "-i", r"/\/mnt\/pmbootstrap\/packages/d", "/mnt/install/etc/apk/repositories"]
    )

    # The chroot got a copy of the build host's /etc/resolv.conf so apk could
    # resolve the mirrors (see pmb.chroot.init.copy_resolv_conf). Shipping it
    # is both a leak of the builder's internal addressing and a real fault on
    # the target: musl has no fallback resolver and waits ~5s per query, so
    # until dhcpcd installs a lease - and permanently on any network that
    # hands out no DNS - chronyd and "apk update" stall against a nameserver
    # that does not exist there. Devices with NetworkManager overwrite this
    # file on the first connection; an alpine_only device has dhcpcd, which
    # only rewrites it once it has a lease.
    if alpine_only:
        pmb.chroot.root(["sh", "-c", ": > /mnt/install/etc/resolv.conf"])


def set_user(config: Config) -> None:
    """
    Create user with UID 10000 if it doesn't exist.
    Usually the ID for the first user created is 1000, but higher ID is
    chosen here to not cause issues with existing installations. Historically,
    this was done to avoid conflict with Android UIDs/GIDs, but pmOS has since
    dropped support for hybris/Halium.
    """
    chroot = Chroot.rootfs(config.device)
    if not pmb.chroot.user_exists(config.user, chroot):
        pmb.chroot.root(["adduser", "-D", "-u", "10000", config.user], chroot)

    pmaports_cfg = pmb.config.pmaports.read_config()
    groups = []
    groups += pmaports_cfg.get(
        "install_user_groups", "audio,input,netdev,plugdev,video,wheel"
    ).split(",")
    groups += pmb.install.ui.get_groups(config)

    for group in groups:
        pmb.chroot.root(["addgroup", "-S", group], chroot, check=False)
        pmb.chroot.root(["addgroup", config.user, group], chroot)


def setup_login_chpasswd_user_from_arg(user: str, password: str, chroot: Chroot) -> None:
    """
    Set the user's password from what the user passed as --password. Make an
    effort to not have the password end up in the log file by writing it to
    a temp file, instead of "echo user:$pass | chpasswd". The user should of
    course only use this with a test password anyway, but let's be nice and try
    to have the user protected from accidentally posting their password in
    any case.

    :param suffix: of the chroot, where passwd will be execute (either the
                   rootfs_{args.device} or installer_{args.device}
    """
    path = "/tmp/pmbootstrap_chpasswd_in"
    path_outside = chroot / path

    path_outside.write_text(f"{user}:{password}", encoding="utf-8")

    pmb.chroot.root(["sh", "-c", f"cat {shlex.quote(path)} | chpasswd"], chroot)

    os.unlink(path_outside)


def is_root_locked(chroot: Chroot) -> bool:
    """
    Figure out from /etc/shadow if root is already locked. The output of this
    is stored in the log, so use grep to only log the line for root, not the
    line for the user which contains a hash of the user's password.

    :param suffix: either rootfs_{args.device} or installer_{args.device}
    """
    shadow_root = pmb.chroot.root(
        ["grep", "^root:!:", "/etc/shadow"], chroot, output_return=True, check=False
    )
    return shadow_root.startswith("root:!:")


def setup_login(config: Config, chroot: Chroot, password: str) -> None:
    """
    Loop until the password for user has been set successfully, and disable
    root login.

    :param suffix: of the chroot, where passwd will be execute (either the
                   rootfs_{args.device} or installer_{args.device}
    """
    # User password
    logging.info(f" *** SET LOGIN PASSWORD FOR: '{config.user}' ***")
    setup_login_chpasswd_user_from_arg(config.user, password, chroot)

    # Disable root login
    if is_root_locked(chroot):
        logging.debug(f"({chroot}) root is already locked")
    else:
        logging.debug(f"({chroot}) locking root")
        pmb.chroot.root(["passwd", "-l", "root"], chroot)


def copy_ssh_keys(config: Config) -> None:
    """If requested, copy user's SSH public keys to the device if they exist"""
    if not config.ssh_keys:
        return
    keys = []
    for key in glob.glob(os.path.expanduser(config.ssh_key_glob)):
        with open(key) as infile:
            try:
                keys += infile.readlines()
            except UnicodeDecodeError:
                logging.info(
                    f"WARNING: {key} is not a valid SSH key. "
                    "This file will not be copied to device."
                )

    if not len(keys):
        logging.info(
            "NOTE: Public SSH keys not found. Since no SSH keys "
            "were copied, you will need to use SSH password "
            "authentication!"
        )
        return

    authorized_keys = Chroot.native() / "tmp/authorized_keys"
    with open(authorized_keys, "w") as outfile:
        for key in keys:
            outfile.write(f"{key}")

    target = Chroot.native() / "mnt/install/home/" / config.user / ".ssh"
    pmb.helpers.run.root(["mkdir", target])
    pmb.helpers.run.root(["chmod", "700", target])
    pmb.helpers.run.root(["cp", authorized_keys, target / "authorized_keys"])
    pmb.helpers.run.root(["rm", authorized_keys])
    pmb.helpers.run.root(["chown", "-R", "10000:10000", target])


def setup_keymap(config: Config) -> None:
    """Set the keymap with the setup-keymap utility if the device requires it"""
    chroot = Chroot(ChrootType.ROOTFS, config.device)
    deviceinfo = pmb.parse.deviceinfo(device=config.device)
    if not deviceinfo.keymaps or deviceinfo.keymaps.strip() == "":
        logging.debug("DEBUG: No keymap specified for device")
        return
    options = deviceinfo.keymaps.split(" ")
    if config.keymap != "" and config.keymap in options:
        layout, variant = config.keymap.split("/")
        if pmb.config.is_systemd_selected(config):
            pmb.chroot.root(
                ["systemd-firstboot", "--force", f"--keymap={variant}"],
                chroot,
                output=RunOutputTypeDefault.LOG,
            )
        else:
            pmb.chroot.root(["mkdir", "-p", "/run/openrc"], chroot)
            pmb.chroot.root(["touch", "/run/openrc/softlevel"], chroot)
            pmb.chroot.root(
                ["setup-keymap", layout, variant], chroot, output=RunOutputTypeDefault.INTERACTIVE
            )
            pmb.chroot.root(["rm", "-r", "/run/openrc"], chroot)

        # Check xorg config
        xconfig = None
        if (chroot / "etc/X11/xorg.conf.d").exists():
            xconfig = pmb.chroot.root(
                ["grep", "-rl", "XkbLayout", "/etc/X11/xorg.conf.d/"],
                chroot,
                check=False,
                output_return=True,
            )
        if xconfig:
            # Nokia n900 (RX-51) randomly merges some keymaps so we
            # have to specify a composite keymap for a few countries. See:
            # https://gitlab.freedesktop.org/xkeyboard-config/xkeyboard-config/-/blob/master/symbols/nokia_vndr/rx-51
            match variant:
                case "rx51_fi" | "rx51_se":
                    layout = "fise"
                case "rx51_da" | "rx51_no":
                    layout = "dano"
                case "rx51_pt" | "rx51_es":
                    layout = "ptes"
                case _:
                    pass
            # Multiple files can contain the keyboard layout, take last
            xconfig = xconfig.splitlines()[-1]
            old_text = 'Option *\\"XkbLayout\\" *\\".*\\"'
            new_text = 'Option \\"XkbLayout\\" \\"' + layout + '\\"'
            pmb.chroot.root(["sed", "-i", "s/" + old_text + "/" + new_text + "/", xconfig], chroot)
    else:
        logging.warning("WARNING: Configured keymap is not supported by device")


def setup_locale(chroot: Chroot, locale: str) -> None:
    """Set locale-related settings such as $LANG and keyboard layout"""
    # 10locale-pmos.sh gets sourced before 20locale.sh from
    # alpine-baselayout by /etc/profile. Since they don't override the
    # locale if it exists, it warranties we have preference
    pmb.chroot.root(["sh", "-c", f"echo LANG={shlex.quote(locale)} > /etc/locale.conf"], chroot)
    # musl-locales doesn't read from /etc/locale.conf, only from environment variables
    # TODO: once musl implements locales (hopefully with /etc/locale.conf support) the following should be removed
    pmb.chroot.root(
        ["sh", "-c", "echo source /etc/locale.conf > /etc/profile.d/10locale-pmos.sh"], chroot
    )
    # add keyboard layout related to locale and layout switcher
    xkb_layout = get_xkb_layout(locale)
    xkb_vars = xkb_layout.get_profile_vars()
    if xkb_vars:
        xkb_vars = xkb_vars.replace("\n", "\\n")
        pmb.chroot.root(
            ["sed", "-i", "$a\\" + xkb_vars, "/etc/profile.d/10locale-pmos.sh"],
            chroot,
        )
    if (chroot / "etc/X11").exists() and (kb_config := xkb_layout.get_keyboard_config()):
        config_name = "99-keyboard.conf"
        config_tmp_path = f"/tmp/{config_name}"
        config_path = f"/etc/X11/xorg.conf.d/{config_name}"
        pmb.chroot.root(["mkdir", "-p", "/etc/X11/xorg.conf.d"], chroot)
        (chroot / config_tmp_path).write_text(kb_config)
        pmb.chroot.root(["mv", config_tmp_path, config_path], chroot)
        pmb.chroot.root(["chown", "root:root", config_path], chroot)


def setup_hostname(device: str, hostname: str | None) -> None:
    """Set the hostname and update localhost address in /etc/hosts"""
    # Default to device name. If device name is not a valid hostname then
    # default to a static default.
    if not hostname:
        hostname = pmb.helpers.other.normalize_hostname(device)
        if not pmb.helpers.other.validate_hostname(hostname):
            # A valid host name, see:
            # https://datatracker.ietf.org/doc/html/rfc1035#section-2.3.1
            hostname = "postmarketos-device"
    elif not pmb.helpers.other.validate_hostname(hostname):
        # Invalid hostname set by the user e.g., via pmb init, this should
        # fail so they can fix it
        raise RuntimeError(
            "Hostname '" + hostname + "' is not valid, please"
            " run 'pmbootstrap init' to configure it."
        )

    suffix = Chroot(ChrootType.ROOTFS, device)
    # Generate /etc/hostname
    pmb.chroot.root(["sh", "-c", "echo " + shlex.quote(hostname) + " > /etc/hostname"], suffix)
    # Update /etc/hosts
    regex = (
        r"s/^127\.0\.0\.1.*/127.0.0.1\t" + re.escape(hostname) + " localhost.localdomain localhost/"
    )
    pmb.chroot.root(["sed", "-i", "-e", regex, "/etc/hosts"], suffix)


def setup_appstream(offline: bool, chroot: Chroot) -> None:
    """
    If alpine-appstream-downloader has been downloaded, execute it to have
    update AppStream data on new installs
    """
    installed_pkgs = pmb.chroot.apk.installed(chroot)

    if "alpine-appstream-downloader" not in installed_pkgs or offline:
        return

    if not pmb.chroot.root(
        ["alpine-appstream-downloader", "/mnt/appstream-data"], chroot, check=False
    ):
        pmb.chroot.root(["mkdir", "-p", "/var/lib/swcatalog"], chroot)
        pmb.chroot.root(
            [
                "cp",
                "-r",
                "/mnt/appstream-data/icons",
                "/mnt/appstream-data/xml",
                "-t",
                "/var/lib/swcatalog",
            ],
            chroot,
        )


def print_sshd_info(no_sshd: bool) -> None:
    logging.info("")  # make the note stand out
    logging.info("*** SSH DAEMON INFORMATION ***")

    if no_sshd:
        logging.info("SSH daemon is disabled (--no-sshd).")
    else:
        logging.info("SSH daemon is enabled (disable with --no-sshd).")
        logging.info(
            f"Login as '{get_context().config.user}' with the password given during installation."
        )


def disable_service_systemd(chroot: Chroot, service_name: str) -> None:
    preset = f"/usr/lib/systemd/system-preset/80-pmbootstrap-install-disable-{service_name}.preset"
    preset_content = f"disable {service_name}.service"
    pmb.chroot.root(
        ["sh", "-c", f"echo {shlex.quote(preset_content)} > {shlex.quote(preset)}"], chroot
    )
    pmb.chroot.root(["systemctl", "preset", f"{service_name}.service"], chroot)

    # Use the output instead of exit code to be more explicit and so we can put
    # it in the error message
    output = pmb.chroot.root(
        ["systemctl", "is-enabled", f"{service_name}.service"],
        chroot,
        output_return=True,
        check=False,
    )
    output = output.rstrip()
    if output != "disabled":
        raise RuntimeError(f"Failed to disable {service_name} service (systemd preset): {output}")


def disable_service_openrc(chroot: Chroot, service_name: str) -> None:
    # check=False: rc-update doesn't exit with 0 if already disabled
    pmb.chroot.root(["rc-update", "del", service_name, "default"], chroot, check=False)

    # Verify that it's gone
    runlevel_files = pmb.helpers.run.root(
        ["find", "-name", service_name], output_return=True, working_dir=chroot / "etc/runlevels"
    )
    if runlevel_files:
        raise RuntimeError(f"Failed to disable service {service_name} (openrc): {runlevel_files}")


def disable_service(config: Config, chroot: Chroot, service_name: str) -> None:
    if pmb.config.is_systemd_selected(config):
        disable_service_systemd(chroot, service_name)
    else:
        disable_service_openrc(chroot, service_name)


def print_firewall_info(disabled: bool) -> None:
    if pmb.parse.deviceinfo().alpine_only:
        # The firewall comes from postmarketos-base-nftables, which is not
        # installed on a device that only uses Alpine's packages
        logging.info("")
        logging.info("*** FIREWALL INFORMATION ***")
        logging.info(
            "No firewall is installed (deviceinfo_alpine_only). Install and configure"
            " Alpine's nftables package if you want one."
        )
        return

    pmaports_cfg = pmb.config.pmaports.read_config()
    pmaports_ok = pmaports_cfg.get("supported_firewall", None) == "nftables"

    # Find kernel pmaport (will not be found if Alpine kernel is used)
    apkbuild_found = False
    apkbuild_has_opt = False

    kernel = get_kernel_package(get_context().config)
    if kernel:
        _, kernel_apkbuild = pmb.build.get_apkbuild(kernel[0])
        if kernel_apkbuild:
            opts = kernel_apkbuild["options"]
            apkbuild_has_opt = "pmb:kconfigcheck-nftables" in opts
            apkbuild_found = True

    # Print the note and make it stand out
    logging.info("")
    logging.info("*** FIREWALL INFORMATION ***")

    if not pmaports_ok:
        logging.info("Firewall is not supported in checked out pmaports branch.")
    elif disabled:
        logging.info("Firewall is disabled (--no-firewall).")
    elif not apkbuild_found:
        logging.info(
            "Firewall is enabled, but may not work (couldn't"
            " determine if kernel supports nftables)."
        )
    elif apkbuild_has_opt:
        logging.info("Firewall is enabled and supported by kernel.")
    else:
        logging.info(
            "Firewall is enabled, but will not work (no support in kernel config for nftables)."
        )
        logging.info("If/when the kernel supports it in the future, it will work automatically.")

    logging.info("For more information: https://postmarketos.org/firewall")


def generate_binary_list(chroot: Chroot, step: int) -> list[tuple[str, int]]:
    """
    Perform three checks prior to writing binaries to disk: 1) that binaries
    exist, 2) that binaries do not extend into the first partition, 3) that
    binaries do not overlap each other.

    :param suffix: of the chroot, which holds the firmware files (either the
                   rootfs_{args.device} or installer_{args.device}
    :param step: partition step size in bytes
    """
    binary_ranges: dict[int, int] = {}
    binary_list = []
    binaries = (pmb.parse.deviceinfo().sd_embed_firmware or "").split(",")

    for binary_offset in binaries:
        binary, offset_ = binary_offset.split(":")
        try:
            offset = int(offset_)
        except ValueError as exception:
            raise RuntimeError(
                f"Value for firmware binary offset is not valid: {offset_}"
            ) from exception
        binary_path_list = list((chroot / "usr/share").glob(binary))
        if len(binary_path_list) == 0:
            raise RuntimeError(
                "The following firmware binary does not "
                f"exist in the {chroot} chroot: "
                f"/usr/share/{binary}"
            )
        elif len(binary_path_list) != 1:
            raise NonBugError(
                "The following firmware binary pattern is ambiguous "
                f"and matches multiple files in the {chroot} chroot: "
                f"/usr/share/{binary} "
            )
        binary_path = binary_path_list[0]
        # Insure that embedding the firmware will not overrun the
        # first partition
        boot_part_start = pmb.parse.deviceinfo().boot_part_start or "2048"
        max_size = (int(boot_part_start) * 512) - (offset * step)
        binary_size = os.path.getsize(binary_path)
        if binary_size > max_size:
            raise RuntimeError(
                f"The firmware is too big to embed in the disk image {binary_size}B > {max_size}B"
            )
        # Insure that the firmware does not conflict with any other firmware
        # that will be embedded
        binary_start = offset * step
        binary_end = binary_start + binary_size
        for start, end in binary_ranges.items():
            if (binary_start >= start and binary_start < end) or (
                binary_end > start and binary_end <= end
            ):
                raise RuntimeError(
                    f"The firmware overlaps with at least one other firmware image: {binary}"
                )

        binary_ranges[binary_start] = binary_end

        binary_path_str = str(binary_path.relative_to(chroot / "usr/share"))
        binary_list.append((binary_path_str, offset))

    return binary_list


def embed_firmware(suffix: Chroot) -> None:
    """
    This method will embed firmware, located at /usr/share, that are specified
    by the "sd_embed_firmware" deviceinfo parameter into the SD card image
    (e.g. u-boot). Binaries that would overwrite the first partition are not
    accepted, and if multiple binaries are specified then they will be checked
    for collisions with each other.

    :param suffix: of the chroot, which holds the firmware files (either the
                   rootfs_{args.device} or installer_{args.device}
    """
    if not pmb.parse.deviceinfo().sd_embed_firmware:
        return

    step = 1024
    if pmb.parse.deviceinfo().sd_embed_firmware_step_size:
        try:
            step = int(pmb.parse.deviceinfo().sd_embed_firmware_step_size or "invalid")
        except ValueError as exception:
            raise RuntimeError(
                f"Value for deviceinfo_sd_embed_firmware_step_size is not valid: {step}"
            ) from exception

    device_rootfs = mount_device_rootfs(suffix)
    binary_list = generate_binary_list(suffix, step)

    # Write binaries to disk
    for binary, offset in binary_list:
        binary_file = os.path.join("/usr/share", binary)
        logging.info(
            f"Embed firmware {binary} in the SD card image at offset {offset} with step size {step}"
        )
        filename = os.path.join(device_rootfs, binary_file.lstrip("/"))
        pmb.chroot.root(
            ["dd", "if=" + filename, "of=/dev/install", "bs=" + str(step), "seek=" + str(offset)]
        )


def write_cgpt_kpart(layout: PartitionLayout, suffix: Chroot, install_cgpt: bool) -> None:
    """
    Write the kernel to the ChromeOS kernel partition.

    :param layout: partition layout from get_partition_layout()
    :param suffix: of the chroot, which holds the image file to be flashed
    """
    if not pmb.parse.deviceinfo().cgpt_kpart or not install_cgpt:
        return

    device_rootfs = mount_device_rootfs(suffix)
    filename = f"{device_rootfs}{pmb.parse.deviceinfo().cgpt_kpart}"
    pmb.chroot.root(["dd", f"if={filename}", f"of=/dev/installp{layout['kernel']}"])


def sanity_check_boot_size() -> None:
    default = Config().boot_size
    config = get_context().config
    if int(config.boot_size) >= int(default):
        return
    logging.error(
        "ERROR: your pmbootstrap has a small/invalid boot_size of"
        f" {config.boot_size} configured, probably because the config"
        " has been created with an old version."
    )
    logging.error(f"This can lead to problems later on, we recommend setting it to {default} MiB.")
    logging.error(f"Run 'pmbootstrap config boot_size {default}' and try again.")
    sys.exit(1)


def sanity_check_disk(device: Path) -> None:
    device_name = os.path.basename(device)
    if not os.path.exists(device):
        raise RuntimeError(f"{device} doesn't exist, is the disk plugged?")
    if os.path.isdir(f"/sys/class/block/{device_name}"):
        ro = Path(f"/sys/class/block/{device_name}/ro").read_text()
        if ro == "1\n":
            raise RuntimeError(f"{device} is read-only, maybe a locked SD card?")


def sanity_check_disk_size(device: Path) -> None:
    devpath = os.path.realpath(device)
    sysfs = "/sys/class/block/{}/size".format(devpath.replace("/dev/", ""))
    if not os.path.isfile(sysfs):
        # This is a best-effort sanity check, continue if it's not checkable
        return

    raw = Path(sysfs).read_text()

    # Size is in 512-byte blocks
    size = int(raw.strip())
    human = f"{size / 2 / 1024 / 1024:.2f} GiB"

    # Warn if the size is larger than 100GiB
    if (
        not get_context().assume_yes
        and size > (100 * 2 * 1024 * 1024)
        and not pmb.helpers.cli.confirm(
            f"WARNING: The target disk ({devpath}) "
            "is larger than a usual SD card "
            "(>100GiB). Are you sure you want to "
            f"overwrite this {human} disk?",
            no_assumptions=True,
        )
    ):
        raise RuntimeError("Aborted.")


def get_partition_layout(kernel: bool, prep: bool | None) -> PartitionLayout:
    """
    :param kernel: create a separate kernel partition before all other
                   partitions, e.g. for the ChromeOS devices with cgpt
    :param prep: create a PReP Boot partition before root, e.g. for PowerVM
                 or OpenFirmware POWER machines
    :returns: the partition layout, e.g. without reserve and kernel:
              {"kernel": None, "boot": 1, "reserve": None, "root": 2}
    """
    ret: PartitionLayout = {
        "kernel": None,
        "prep": None,
        "boot": 1,
        "reserve": None,
        "root": 2,
    }

    if prep:
        ret["prep"] = 1
        ret["boot"] = 2
        ret["root"] = 3
    else:
        if kernel:
            ret["kernel"] = 1
            ret["boot"] = 2
            ret["root"] += 1

    return ret


def get_uuid(partition: Path) -> str:
    """
    Get UUID of a partition

    :param partition: block device for getting UUID from
    """
    return pmb.chroot.root(
        [
            "blkid",
            "-s",
            "UUID",
            "-o",
            "value",
            partition,
        ],
        output_return=True,
    ).rstrip()


def create_crypttab(layout: PartitionLayout | None, chroot: Chroot) -> None:
    """
    Create /etc/crypttab config

    :param layout: partition layout from get_partition_layout() or None
    :param suffix: of the chroot, which crypttab will be created to
    """
    root_dev = Path(f"/dev/installp{layout['root']}") if layout else Path("/dev/install")

    luks_uuid = get_uuid(root_dev)
    crypttab = f"root UUID={luks_uuid} none luks\n"

    (chroot / "tmp/crypttab").open("w").write(crypttab)
    pmb.chroot.root(["mv", "/tmp/crypttab", "/etc/crypttab"], chroot)


def create_fstab(
    layout: PartitionLayout | None,
    chroot: Chroot,
    filesystem: str,
    full_disk_encryption: bool,
) -> None:
    """
    Create /etc/fstab config

    :param layout: partition layout from get_partition_layout() or None
    :param chroot: of the chroot, which fstab will be created to
    """
    if layout:
        boot_dev = Path(f"/dev/installp{layout['boot']}")
        root_dev = Path(f"/dev/installp{layout['root']}")
    else:
        boot_dev = None
        root_dev = Path("/dev/install")

    root_mount_point = "/dev/mapper/root" if full_disk_encryption else f"UUID={get_uuid(root_dev)}"
    root_filesystem = pmb.install.get_root_filesystem(filesystem)

    if root_filesystem == "btrfs":
        # btrfs gets separate subvolumes for root, var and home
        fstab = f"""
# <file system> <mount point> <type> <options> <dump> <pass>
{root_mount_point} / btrfs subvol=@,compress=zstd:2,ssd 0 0
{root_mount_point} /home btrfs subvol=@home,compress=zstd:2,ssd 0 0
{root_mount_point} /root btrfs subvol=@root,compress=zstd:2,ssd 0 0
{root_mount_point} /srv btrfs subvol=@srv,compress=zstd:2,ssd 0 0
{root_mount_point} /var btrfs subvol=@var,ssd 0 0
{root_mount_point} /.snapshots btrfs subvol=@snapshots,compress=zstd:2,ssd 0 0
""".lstrip()

    else:
        fstab = f"""
# <file system> <mount point> <type> <options> <dump> <pass>
{root_mount_point} / {root_filesystem} defaults 0 1
""".lstrip()

    if boot_dev:
        boot_mount_point = f"UUID={get_uuid(boot_dev)}"
        boot_options = "nodev,nosuid,noexec"
        boot_filesystem = (
            pmb.parse.deviceinfo().boot_filesystem
            or pmb.parse.deviceinfo_schema_default_boot_filesystem()
        )
        if boot_filesystem in ("fat16", "fat32"):
            boot_filesystem = "vfat"
            boot_options += ",umask=0077,nosymfollow,codepage=437,iocharset=ascii"
        fstab += f"{boot_mount_point} /boot {boot_filesystem} {boot_options} 0 2\n"

    with (chroot / "tmp/fstab").open("w") as f:
        f.write(fstab)
    pmb.chroot.root(["mv", "/tmp/fstab", "/etc/fstab"], chroot)


def install_system_image(
    cipher: str,
    filesystem: str,
    full_disk_encryption: bool,
    install_cgpt: bool,
    install_local_pkgs: bool,
    iter_time: str,
    rsync: bool,
    sector_size: int | None,
    sparse: bool | None,
    verbose: bool,
    chroot: Chroot,
    step: int,
    steps: int,
    boot_label: str = "pmOS_boot",
    root_label: str = "pmOS_root",
    split: bool = False,
    single_partition: bool = False,
    disk: Path | None = None,
) -> None:
    """
    :param size_reserve: empty partition between root and boot in MiB (pma#463)
    :param suffix: the chroot suffix, where the rootfs that will be installed
                   on the device has been created (e.g. "rootfs_qemu-amd64")
    :param step: next installation step
    :param steps: total installation steps
    :param boot_label: label of the boot partition (e.g. "pmOS_boot")
    :param root_label: label of the root partition (e.g. "pmOS_root")
    :param split: create separate images for boot and root partitions
    :param disk: path to disk block device (e.g. /dev/mmcblk0) or None
    """
    config = get_context().config
    device = chroot.name
    # Partition and fill image file/disk block device
    logging.info(f"*** ({step}/{steps}) PREPARE INSTALL BLOCKDEVICE ***")
    pmb.helpers.mount.umount_all(chroot.path)
    (size_boot, size_root) = get_subpartitions_size(chroot)
    if not single_partition:
        layout = get_partition_layout(
            bool(pmb.parse.deviceinfo().cgpt_kpart) and install_cgpt,
            pmb.parse.deviceinfo().create_prep_boot,
        )
    else:
        layout = None
    if not rsync:
        # If disk is set, we already handled this earlier.
        if not disk:
            pmb.install.blockdevice.create(sector_size, size_boot, size_root, split)
        if not split and layout:
            if pmb.parse.deviceinfo().cgpt_kpart and install_cgpt:
                pmb.install.partition_cgpt(layout, size_boot)
            elif pmb.parse.deviceinfo().create_prep_boot:
                pmb.install.partition_prep(layout, size_boot)
            else:
                pmb.install.partition(layout, size_boot)

    # Inform kernel about changed partition table in case parted couldn't
    pmb.chroot.root(["partprobe", "/dev/install"], check=False)

    if not split and not single_partition:
        if layout is None:
            raise AssertionError  # Initialized above for not single_partition case (mypy needs this)
        pmb.install.partitions_mount(device, layout, disk)

    pmb.install.format(
        layout,
        boot_label,
        root_label,
        disk,
        rsync,
        filesystem,
        full_disk_encryption,
        cipher,
        iter_time,
    )

    # Since we shut down the chroot we need to mount it again
    pmb.chroot.mount(chroot)

    # Create /etc/fstab and /etc/crypttab
    logging.info("(native) create /etc/fstab")
    create_fstab(layout, chroot, filesystem, full_disk_encryption)
    if full_disk_encryption:
        logging.info("(native) create /etc/crypttab")
        create_crypttab(layout, chroot)

    # Run mkinitfs to pass UUIDs to cmdline
    pmb.chroot.initfs.run_mkinitfs(chroot)

    # boot-deploy is a postmarketOS package, so on alpine_only devices nothing
    # would write /boot/cmdline.txt and the firmware would boot the kernel
    # without a root= argument.
    if pmb.parse.deviceinfo().alpine_only and pmb.parse.deviceinfo().generate_cmdline_txt:
        write_cmdline_txt(layout, chroot, filesystem, full_disk_encryption)

    # Clean up after running mkinitfs in chroot
    pmb.helpers.mount.umount_all(chroot.path)
    pmb.helpers.run.root(["rm", chroot / "in-pmbootstrap"])
    pmb.chroot.remove_mnt_pmbootstrap(chroot)

    # Just copy all the files
    logging.info(f"*** ({step + 1}/{steps}) FILL INSTALL BLOCKDEVICE ***")
    copy_files_from_chroot(chroot, rsync, verbose)
    create_home_from_skel(filesystem, config.user)
    configure_apk(install_local_pkgs)
    copy_ssh_keys(config)

    # Don't try to embed firmware and cgpt on split images since there's no
    # place to put it and it will end up in /dev of the chroot instead
    if not split and not single_partition:
        if layout is None:
            raise AssertionError  # Initialized above for not single_partition case (mypy needs this)
        embed_firmware(chroot)
        write_cgpt_kpart(layout, chroot, install_cgpt)

    # Install powervm-boot to the PReP boot partition
    if pmb.parse.deviceinfo().create_prep_boot and layout:
        pmb.chroot.root(
            [
                "dd",
                "if=/mnt/install/usr/lib/powervm-boot/powervm-boot",
                f"of=/dev/installp{layout['prep']}",
                "bs=512",
                "conv=notrunc",
            ]
        )

    if disk:
        logging.info(f"Unmounting disk {disk} (this may take a while to sync, please wait)")
    pmb.chroot.shutdown(True)

    # Convert rootfs to sparse using img2simg
    if sparse is None:
        sparse = pmb.parse.deviceinfo().flash_sparse == "true"

    if sparse and not split and not disk:
        workdir = Path("/home/pmos/rootfs")
        logging.info("(native) make sparse rootfs")
        pmb.chroot.apk.install(["android-tools-img2simg"], Chroot.native())
        sys_image = device + ".img"
        sys_image_sparse = device + "-sparse.img"
        pmb.chroot.user(["img2simg", sys_image, sys_image_sparse], working_dir=workdir)
        pmb.chroot.user(["mv", "-f", sys_image_sparse, sys_image], working_dir=workdir)

        # patch sparse image for Samsung devices if specified
        samsungify_strategy = pmb.parse.deviceinfo().flash_sparse_samsung_format
        if samsungify_strategy:
            logging.info("(native) convert sparse image into Samsung's sparse image format")
            pmb.chroot.apk.install(["sm-sparse-image-tool"], Chroot.native())
            sys_image = f"{device}.img"
            sys_image_patched = f"{device}-patched.img"
            pmb.chroot.user(
                [
                    "sm_sparse_image_tool",
                    "samsungify",
                    "--strategy",
                    samsungify_strategy,
                    sys_image,
                    sys_image_patched,
                ],
                working_dir=workdir,
            )
            pmb.chroot.user(["mv", "-f", sys_image_patched, sys_image], working_dir=workdir)


def print_flash_info(
    device: str, deviceinfo: Deviceinfo, split: bool, have_disk: bool, single_partition: bool
) -> None:
    """
    Print flashing information, based on the deviceinfo data and the
    pmbootstrap arguments.
    """
    logging.info("")  # make the note stand out
    logging.info("*** FLASHING INFORMATION ***")

    # System flash information
    method = deviceinfo.flash_method
    flasher = pmb.config.flashers.get(method, {})
    flasher_actions = flasher.get("actions", {})
    if not isinstance(flasher_actions, dict):
        raise TypeError(f"flasher actions must be a dictionary, got: {flasher_actions}")
    requires_split = flasher.get("split", False)

    if method == "none":
        logging.info(
            "Refer to the installation instructions of your device,"
            " or the generic install instructions in the wiki."
        )
        logging.info("https://wiki.postmarketos.org/wiki/Installation_guide#pmbootstrap_flash")
        return

    logging.info("Run the following to flash your installation to the target device:")

    if "flash_rootfs" in flasher_actions and not have_disk and bool(split) == requires_split:
        logging.info("* pmbootstrap flasher flash_rootfs")
        logging.info("  Flashes the generated rootfs image to your device:")
        if split:
            logging.info(f"  {Chroot.native() / 'home/pmos/rootfs' / device}-root.img")
        else:
            logging.info(f"  {Chroot.native() / 'home/pmos/rootfs' / device}.img")
            if not single_partition:
                logging.info(
                    "  (NOTE: This file has a partition table, which"
                    " contains /boot and / subpartitions. That way we"
                    " don't need to change the partition layout on your"
                    " device.)"
                )

    # if current flasher supports vbmeta and partition is explicitly specified
    # in deviceinfo
    if "flash_vbmeta" in flasher_actions and (
        deviceinfo.flash_fastboot_partition_vbmeta or deviceinfo.flash_heimdall_partition_vbmeta
    ):
        logging.info("* pmbootstrap flasher flash_vbmeta")
        logging.info("  Flashes vbmeta image with verification disabled flag.")

    # if current flasher supports dtbo and partition is explicitly specified
    # in deviceinfo
    if "flash_dtbo" in flasher_actions and (
        deviceinfo.flash_fastboot_partition_dtbo or deviceinfo.flash_heimdall_partition_dtbo
    ):
        logging.info("* pmbootstrap flasher flash_dtbo")
        logging.info("  Flashes dtbo image.")

    # Most flash methods operate independently of the boot partition.
    # (e.g. an Android boot image is generated). In that case, "flash_kernel"
    # works even when partitions are split or installing to disk. This is not
    # possible if the flash method requires split partitions.
    if "flash_kernel" in flasher_actions and (not requires_split or split):
        logging.info("* pmbootstrap flasher flash_kernel")
        logging.info("  Flashes the kernel + initramfs to your device:")
        if requires_split:
            logging.info(f"  {Chroot.native() / 'home/pmos/rootfs' / device}-boot.img")
        else:
            logging.info(f"  {Chroot(ChrootType.ROOTFS, device) / 'boot'}")

    if "flash_boot" in flasher_actions and (Chroot.rootfs(device) / "boot/boot.img").exists():
        logging.info("* pmbootstrap flasher flash_boot")
        logging.info("  Flashes the generated Android boot image to your device:")
        logging.info(f"  {Chroot.rootfs(device) / 'boot/boot.img'}")
        logging.info("  (NOTE: This is not necessary if using a custom bootloader like U-Boot)")

    if "boot" in flasher_actions or "boot_gki" in flasher_actions:
        logging.info(
            "  (NOTE: " + method + " also supports booting"
            " the kernel/initramfs directly without flashing."
            " Use 'pmbootstrap flasher boot' to do that.)"
        )

    if (
        "flash_lk2nd" in flasher_actions
        and (Chroot(ChrootType.ROOTFS, device) / "boot/lk2nd.img").exists()
    ):
        logging.info(
            "* Your device supports and may even require"
            " flashing lk2nd. You should flash it before"
            " flashing anything else. Use 'pmbootstrap flasher"
            " flash_lk2nd' to do that."
        )

    # Export information
    logging.info(
        "* If the above steps do not work, you can also create"
        " symlinks to the generated files with 'pmbootstrap export'"
        " and flash outside of pmbootstrap."
    )


def install_recovery_zip(
    cipher: str,
    cmdline: str | None,
    full_disk_encryption: bool,
    no_reboot: bool,
    partition: str | None,
    recovery_flash_kernel: bool,
    recovery_install_partition: str,
    resume: bool,
    device: str,
    arch: Arch,
    steps: int,
) -> None:
    logging.info(f"*** ({steps}/{steps}) CREATING RECOVERY-FLASHABLE ZIP ***")
    chroot = Chroot(ChrootType.BUILDROOT, arch)
    pmb.chroot.init(chroot)
    mount_device_rootfs(Chroot.rootfs(device), chroot)
    pmb.install.recovery.create_zip(
        cipher,
        cmdline,
        full_disk_encryption,
        no_reboot,
        partition,
        recovery_flash_kernel,
        recovery_install_partition,
        resume,
        chroot,
        device,
    )

    # Flash information
    logging.info("*** FLASHING INFORMATION ***")
    logging.info("Flashing with the recovery zip is explained here:")
    logging.info("https://postmarketos.org/recoveryzip")


def get_selected_providers(packages: list[str]) -> list[str]:
    """
    Look through the specified packages and see which providers were selected
    in "pmbootstrap init". Install those as extra packages to select them
    instead of the default provider. This function is called recursively on the
    dependencies of the given packages.

    :param packages: the packages that have selectable providers (_pmb_select)
    :param initial: used internally when the function calls itself
    :return: additional provider packages to install
    """
    global get_selected_providers_visited

    ret = []

    for package in packages:
        if package in get_selected_providers_visited:
            logging.verbose(f"get_selected_providers: {package}: already visited")
            continue
        get_selected_providers_visited += [package]

        # Note that this ignores packages that don't exist. This means they
        # aren't in pmaports. This is fine, with the assumption that
        # installation will fail later in some other method if they truly don't
        # exist in any repo.
        apkbuild = pmb.helpers.pmaports.get(package, subpackages=False, must_exist=False)
        if not apkbuild:
            continue
        for select in apkbuild["_pmb_select"]:
            if select in get_context().config.providers:
                ret += [get_context().config.providers[select]]
                logging.verbose(f"{package}: install selected_providers: {', '.join(ret)}")
            else:
                for default in apkbuild["_pmb_default"]:
                    # default: e.g. "postmarketos-base-ui-audio-pipewire"
                    # select: e.g. "postmarketos-base-ui-audio"
                    if default.startswith(f"{select}-"):
                        ret += [default]
        # Also iterate through dependencies to collect any providers they have
        depends = apkbuild["depends"]
        if depends:
            ret += get_selected_providers(depends)

    return ret


def get_recommends(packages: list[str], install_recommends: bool) -> Sequence[str]:
    """
    Look through the specified packages and collect additional packages
    specified under _pmb_recommends in them. This is recursive, so it will dive
    into packages that are listed under recommends to collect any packages they
    might also have listed under their own _pmb_recommends.

    Recursion is only done into packages found in pmaports.

    If running with pmbootstrap install --no-recommends, this function returns
    an empty list.

    :param packages: list of packages of which we want to get the recommends
    :param initial: used internally when the function calls itself
    :returns: list of pkgnames, e.g. ["chatty", "gnome-contacts"]
    """
    global get_recommends_visited

    ret: list[str] = []
    if not install_recommends:
        return ret

    for package in packages:
        if package in get_recommends_visited:
            logging.verbose(f"get_recommends: {package}: already visited")
            continue
        get_recommends_visited += [package]

        # Note that this ignores packages that don't exist. This means they
        # aren't in pmaports. This is fine, with the assumption that
        # installation will fail later in some other method if they truly don't
        # exist in any repo.
        apkbuild = pmb.helpers.pmaports.get(package, must_exist=False)
        if not apkbuild:
            continue
        if package in apkbuild["subpackages"]:
            # Just focus on the subpackage
            apkbuild = apkbuild["subpackages"][package]
            # The subpackage is None if the subpackage does not have a function
            # in the APKBUILD (uses the default function), e.g. for most openrc
            # subpackages. See pmb.parse._apkbuild._parse_subpackage().
            if not apkbuild:
                continue
        recommends = apkbuild["_pmb_recommends"]
        if recommends:
            logging.debug(f"{package}: install _pmb_recommends: {', '.join(recommends)}")
            ret += recommends
            # Call recursively in case recommends have pmb_recommends of their
            # own.
            ret += get_recommends(recommends, install_recommends)
        # Also iterate through dependencies to collect any recommends they have
        depends = apkbuild["depends"]
        if depends:
            ret += get_recommends(depends, install_recommends)

    return ret


def create_device_rootfs(
    step: int,
    steps: int,
    add: str,
    password: str,
    install_base: bool,
    install_recommends: bool,
    full_disk_encryption: bool,
    no_sshd: bool,
    no_firewall: bool,
    wifi_ssid: str | None = None,
    wifi_psk: str | None = None,
    wifi_country: str | None = None,
) -> None:
    # list all packages to be installed (including the ones specified by --add)
    # and upgrade the installed packages/apkindexes
    context = get_context()
    config = context.config
    device = context.config.device
    logging.info(f'*** ({step}/{steps}) CREATE DEVICE ROOTFS ("{device}") ***')

    chroot = Chroot(ChrootType.ROOTFS, device)
    pmb.chroot.init(chroot)
    # Create user before installing packages, so post-install scripts of
    # pmaports can figure out the username (legacy reasons: pmaports#820)
    set_user(context.config)

    alpine_only = pmb.parse.deviceinfo().alpine_only

    # Fill install_packages
    if alpine_only:
        # Everything postmarketOS-specific is skipped here on purpose: the base
        # and UI meta packages, their providers and _pmb_recommends, the kernel
        # and firmware subpackages of the device package (alpine_only devices
        # depend on an Alpine kernel directly) and postmarketos-base-nofde /
        # postmarketos-base-systemd. What is left is Alpine's base system plus
        # the device package.
        install_packages = [*pmb.config.install_device_packages_alpine_only, "device-" + device]

        # The postmarketos-ui-* meta packages are postmarketOS packages, so the
        # selected UI is used as a plain Alpine package name instead: ui=sway
        # installs Alpine's sway, ui=weston installs Alpine's weston. tinydm is
        # Alpine's tiny display manager, which starts the session on boot (it is
        # what the postmarketos-ui-* packages use too).
        if config.ui.lower() != "none":
            install_packages += [config.ui, "tinydm", "tinydm-openrc"]

            # Equivalent of _pmb_recommends in a postmarketos-ui-* APKBUILD:
            # the terminal, launcher, audio stack, portals and so on that turn
            # a bare compositor into a usable session.
            if install_recommends:
                install_packages += pmb.helpers.ui.alpine_ui_recommends(
                    config.ui, pmb.parse.deviceinfo().arch
                )

        if context.config.extra_packages.lower() != "none":
            install_packages += context.config.extra_packages.split(",")
        if add:
            install_packages += add.split(",")

        pmb.helpers.repo.update(pmb.parse.deviceinfo().arch, alpine_only=True)
    else:
        install_packages = [*pmb.config.install_device_packages, "device-" + device]
        if not install_base:
            install_packages = [p for p in install_packages if p != "postmarketos-base"]
        ui_package_name = f"postmarketos-ui-{config.ui}"
        if config.ui.lower() != "none":
            install_packages += [ui_package_name]

        # Add additional providers of base/device/UI package
        install_packages += get_selected_providers(install_packages)

        install_packages += get_kernel_package(config)
        install_packages += get_nonfree_packages(device)
        if context.config.ui.lower() != "none":
            ui_package = pmb.helpers.pmaports.get(
                ui_package_name, subpackages=False, must_exist=False
            )
            if ui_package and context.config.ui_extras:
                extra = f"postmarketos-ui-{config.ui}-extras"
                extra_package = ui_package["subpackages"].get(extra)
                if extra_package:
                    install_packages += [extra]

        if context.config.extra_packages.lower() != "none":
            install_packages += context.config.extra_packages.split(",")
        if add:
            install_packages += add.split(",")

        # postmarketos-base supports a dummy package for blocking unl0kr install
        # when not required
        if not full_disk_encryption:
            install_packages += ["postmarketos-base-nofde"]

        pmb.helpers.repo.update(pmb.parse.deviceinfo().arch)

        # Install uninstallable "dependencies" by default
        install_packages += get_recommends(install_packages, install_recommends)

        # Install the base-systemd package first to make sure presets are
        # available when services are installed later
        if pmb.config.other.is_systemd_selected(context.config):
            pmb.chroot.apk.install(["postmarketos-base-systemd"], chroot)

    # Install all packages to device rootfs chroot (and rebuild the initramfs,
    # because that doesn't always happen automatically yet, e.g. when the user
    # installed a hook without pmbootstrap - see #69 for more info)
    # Packages will be built if necessary as part of this step
    pmb.chroot.apk.install(install_packages, chroot)
    pmb.chroot.initfs.build(chroot)

    # Set the user password
    setup_login(config, chroot, password)

    # Set the keymap if the device requires it
    setup_keymap(config)

    # Set timezone
    pmb.chroot.root(["setup-timezone", "-i", config.timezone], chroot)

    # Set locale
    setup_locale(chroot, config.locale)

    # Set the hostname as the device name
    setup_hostname(device, config.hostname)

    setup_appstream(context.offline, chroot)

    if wifi_ssid:
        setup_wifi(chroot, wifi_ssid, wifi_psk, wifi_country)

    if alpine_only and config.ui.lower() != "none":
        setup_tinydm(chroot, config.ui, config.user)

    if no_sshd:
        disable_service(config, chroot, "sshd")
    if no_firewall:
        disable_service(config, chroot, "nftables")


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
    verbose: bool,
    zap: bool,
    is_split: bool,
    wifi_ssid: str | None = None,
    wifi_psk: str | None = None,
    wifi_country: str | None = None,
) -> None:
    device = get_context().config.device
    chroot = Chroot(ChrootType.ROOTFS, device)
    deviceinfo = pmb.parse.deviceinfo()
    # Sanity checks
    sanity_check_boot_size()
    if not android_recovery_zip and disk:
        sanity_check_disk(disk)
        sanity_check_disk_size(disk)

    # --single-partition implies --no-split. There is nothing to split if
    # there is only a single partition.
    if single_partition:
        is_split = False
        if deviceinfo.create_initfs_extra:
            raise RuntimeError("--single-partition does not work for devices with initramfs-extra")

    # Number of steps for the different installation methods.
    if no_image:
        steps = 2
    elif android_recovery_zip:
        steps = 3
    else:
        steps = 4

    if zap:
        pmb.chroot.zap(False)

    # Install required programs in native chroot
    step = 1
    logging.info(f"*** ({step}/{steps}) PREPARE NATIVE CHROOT ***")
    pmb.chroot.init(Chroot.native())
    if disk:
        pmb.install.blockdevice.handle_disk_mount(disk)
    pmb.chroot.apk.install(pmb.config.install_native_packages, Chroot.native(), build=False)
    step += 1

    create_device_rootfs(
        step,
        steps,
        add,
        password,
        install_base,
        install_recommends,
        full_disk_encryption,
        no_sshd,
        no_firewall,
        wifi_ssid=wifi_ssid,
        wifi_psk=wifi_psk,
        wifi_country=wifi_country,
    )
    step += 1

    if no_image:
        return
    elif android_recovery_zip:
        return install_recovery_zip(
            cipher,
            cmdline,
            full_disk_encryption,
            no_reboot,
            partition,
            recovery_flash_kernel,
            recovery_install_partition,
            resume,
            device,
            deviceinfo.arch,
            steps,
        )

    # The creation of the device rootfs may have built packages, which in turn
    # would result in zapping of the native chroot.
    # FIXME: Ideally we'd be building the packages in an earlier stage, but I
    # believe that would require a complete overhaul of pmbootstrap
    if not Chroot.native().exists():
        pmb.chroot.init(Chroot.native())
        if disk:
            pmb.install.blockdevice.handle_disk_mount(disk)
        pmb.chroot.apk.install(pmb.config.install_native_packages, Chroot.native(), build=False)

    install_system_image(
        cipher,
        filesystem,
        full_disk_encryption,
        install_cgpt,
        install_local_pkgs,
        iter_time,
        rsync,
        sector_size,
        sparse,
        verbose,
        chroot,
        step,
        steps,
        split=is_split,
        single_partition=single_partition,
        disk=disk,
    )

    print_flash_info(
        device,
        deviceinfo,
        is_split,
        bool(disk and disk.is_absolute()),
        single_partition,
    )
    print_sshd_info(no_sshd)
    print_firewall_info(no_firewall)

    # Leave space before 'chroot still active' note
    logging.info("")
