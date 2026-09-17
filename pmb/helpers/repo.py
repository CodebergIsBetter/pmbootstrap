# Copyright 2023 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Functions that work with binary package repos.

See also:
- pmb/helpers/pmaports.py (work with pmaports)
- pmb/helpers/package.py (work with both)
"""

import hashlib
import os
from pathlib import Path
from typing import Literal

import pmb.config.pmaports
import pmb.helpers.http
import pmb.helpers.run
from pmb.core.apkindex import Apkindex
from pmb.core.arch import Arch
from pmb.core.context import get_context
from pmb.core.pkgrepo import pkgrepo_names
from pmb.helpers import logging
from pmb.helpers.exceptions import NonBugError
from pmb.meta import Cache


def apkindex_hash(url: str, length: int = 8) -> Path:
    r"""
    Generate the hash that APK adds to the APKINDEX and apk packages in its apk cache folder.

    It is the "12345678" part in this example:
    "APKINDEX.12345678.tar.gz".

    :param length: The length of the hash in the output file.

    See also: official implementation in apk-tools:
    <https://git.alpinelinux.org/cgit/apk-tools/>

    blob.c: apk_blob_push_hexdump(), "const char \\*xd"
    apk_defines.h: APK_CACHE_CSUM_BYTES
    database.c: apk_repo_format_cache_index()
    """
    binary = hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).digest()
    xd = "0123456789abcdefghijklmnopqrstuvwxyz"
    csum_bytes = int(length / 2)

    ret = ""
    for i in range(csum_bytes):
        ret += xd[(binary[i] >> 4) & 0xF]
        ret += xd[binary[i] & 0xF]

    return Path(f"APKINDEX.{ret}.tar.gz")


# FIXME: make config.mirrors a normal dict
# mypy: disable-error-code="literal-required"
@Cache("user_repository", "mirrors_exclude", "alpine_only")
def get_repos_from_config(
    user_repository: Path | None = None,
    mirrors_exclude: list[str] | Literal[True] | None = None,
    alpine_only: bool = False,
) -> list[str]:
    """
    Get a list of repository URLs, as they are in /etc/apk/repositories.

    :param user_repository: add /mnt/pmbootstrap/packages
    :param mirrors_exclude: mirrors to exclude (see pmb.core.config.Mirrors) or true to exclude
                            all mirrors and only return the local repos
    :param alpine_only: only return Alpine's mirrors, for devices that set
                        deviceinfo_alpine_only (see pmb.parse.deviceinfo)
    :returns: list of mirror strings, like ["/mnt/pmbootstrap/packages",
                                            "http://...", ...]
    """
    # Never mutate the caller's list below (see mirrors_exclude.append()); the
    # @Cache key is built from the arguments before this function runs, so a
    # mutated argument would also poison the cache.
    if mirrors_exclude is None:
        mirrors_exclude = []
    elif mirrors_exclude is not True:
        mirrors_exclude = list(mirrors_exclude)

    ret: list[str] = []

    # Local user repository (for packages compiled with pmbootstrap)
    if user_repository:
        channels = pmb.config.pmaports.all_channels()
        if alpine_only:
            # No systemd channel: alpine_only devices are always OpenRC
            channels = [c for c in channels if not c.startswith("systemd-")]
        ret.extend(str(user_repository / channel) for channel in channels)

    if mirrors_exclude is True:
        return ret

    config = get_context().config

    # Get mirrordirs from channels.cfg (postmarketOS mirrordir is the same as
    # the pmaports branch of the channel, no need to make it more complicated)
    channel_cfg = pmb.config.pmaports.read_config_channel()
    mirrordir_pmos = channel_cfg["branch_pmaports"]
    mirrordir_alpine = channel_cfg["mirrordir_alpine"]

    # Don't add the systemd mirror if systemd is disabled
    if not alpine_only and not pmb.config.is_systemd_selected(config):
        mirrors_exclude.append("systemd")

    # ["pmaports", "systemd", "alpine", "plasma-nightly"], or just ["alpine"]
    # for alpine_only devices. This is an allowlist on purpose: excluding the
    # known postmarketOS repos by name would silently let a future extra-repo
    # leak back into an "Alpine only" rootfs.
    repos = ["alpine"] if alpine_only else [*pkgrepo_names(), "alpine"]
    for repo in repos:
        if repo in mirrors_exclude:
            continue

        # Allow adding a custom mirror in front of the real mirror. This is used
        # in bpo to build with a WIP repository in addition to the final
        # repository.
        for suffix in ["_custom", ""]:
            mirror = config.mirrors[f"{repo}{suffix}"]

            # During bootstrap / bpo testing we run without a pmOS binary repo
            if mirror.lower() == "none":
                if suffix != "_custom":
                    logging.warn_once(
                        f"NOTE: Skipping mirrors.{repo} for /etc/apk/repositories (is configured"
                        ' as "none")'
                    )
                continue

            mirrordirs = []
            if repo == "alpine":
                # FIXME: This is a bit of a mess
                mirrordirs = [f"{mirrordir_alpine}/main", f"{mirrordir_alpine}/community"]
                if mirrordir_alpine == "edge":
                    mirrordirs.append(f"{mirrordir_alpine}/testing")
            else:
                mirrordirs = [mirrordir_pmos]

            for mirrordir in mirrordirs:
                url = os.path.join(mirror, mirrordir)
                if url not in ret:
                    ret.append(url)

    return ret


def apkindex_files(
    arch: Arch | None = None,
    user_repository: bool = True,
    exclude_mirrors: list[str] | None = None,
    alpine_only: bool = False,
) -> list[Apkindex]:
    """
    Get a list of outside paths to all resolved APKINDEX.tar.gz files for a specific arch.

    :param arch: defaults to native
    :param user_repository: add path to index of locally built packages
    :param exclude_mirrors: list of mirrors to exclude (e.g. ["alpine", "pmaports"])
    :param alpine_only: only consider Alpine's mirrors
    :returns: list of absolute APKINDEX.tar.gz file paths
    """
    if not arch:
        arch = Arch.native()

    ret: list[Apkindex] = []
    # Local user repository (for packages compiled with pmbootstrap)
    if user_repository:
        channels = pmb.config.pmaports.all_channels()
        if alpine_only:
            channels = [c for c in channels if not c.startswith("systemd-")]
        ret.extend(
            Apkindex(get_context().config.work / "packages" / channel / arch / "APKINDEX.tar.gz")
            for channel in channels
        )

    # Resolve the APKINDEX.$HASH.tar.gz files
    ret.extend(
        Apkindex(file)
        for url in get_repos_from_config(None, exclude_mirrors, alpine_only)
        if (file := get_context().config.work / f"cache_apk_{arch}" / apkindex_hash(url)).exists()
    )

    return ret


@Cache("arch", "alpine_only", force=False)
def update(
    arch: Arch | None = None,
    force: bool = False,
    existing_only: bool = False,
    alpine_only: bool = False,
) -> bool:
    """
    Download the APKINDEX files for all URLs depending on the architectures.

    :param arch: * one Alpine architecture name ("x86_64", "armhf", ...)
                 * None for all architectures
    :param force: even update when the APKINDEX file is fairly recent
    :param existing_only: only update the APKINDEX files that already exist,
                          this is used by "pmbootstrap update"
    :param alpine_only: only update the indexes of Alpine's mirrors

    :returns: True when files have been downloaded, False otherwise
    """
    # Skip in offline mode, only show once
    if get_context().offline:
        logging.warn_once("NOTE: skipping package index update (offline mode)")
        return False

    # Architectures and retention time
    supported_binary = Arch.supported_binary()
    architectures = [arch] if arch else supported_binary
    retention_hours = pmb.config.apkindex_retention_time
    retention_seconds = retention_hours * 3600

    # Find outdated APKINDEX files. Formats:
    # outdated: {URL: apkindex, ... }
    # outdated_arches: ["armhf", "x86_64", ... ]
    outdated = {}
    outdated_arches: list[Arch] = []
    for url in get_repos_from_config(None, alpine_only=alpine_only):
        for architecture in architectures:
            # APKINDEX file name from the URL
            url_full = f"{url}/{architecture}/APKINDEX.tar.gz"
            cache_apk_outside = get_context().config.work / f"cache_apk_{architecture}"
            apkindex = cache_apk_outside / f"{apkindex_hash(url)}"

            # Find update reason, possibly skip non-existing or known 404 files
            reason = None
            if not os.path.exists(apkindex):
                if existing_only:
                    continue
                reason = "file does not exist yet"
            elif force:
                reason = "forced update"
            elif pmb.helpers.file.is_older_than(apkindex, retention_seconds):
                reason = "older than " + str(retention_hours) + "h"
            if not reason:
                continue

            # Update outdated and outdated_arches
            logging.debug("APKINDEX outdated (" + reason + "): " + url_full)
            outdated[url_full] = apkindex
            if architecture not in outdated_arches:
                outdated_arches.append(architecture)

    # Bail out or show log message
    if not len(outdated):
        return False
    logging.info(
        "Update package index for "
        + ", ".join([str(a) for a in outdated_arches])
        + " ("
        + str(len(outdated))
        + " file(s))"
    )

    # Download and move to right location
    missing_ignored = False
    for i, (url, target) in enumerate(outdated.items()):
        pmb.helpers.cli.progress_print(i / len(outdated))
        temp = pmb.helpers.http.download(url, "APKINDEX", False, logging.DEBUG, True, True)
        if not temp:
            if (
                os.environ.get("PMB_APK_FORCE_MISSING_REPOSITORIES") == "1"
                or arch not in supported_binary
            ):
                missing_ignored = True
                continue
            else:
                logging.info("NOTE: check the [mirrors] section in 'pmbootstrap config'")
                raise NonBugError("getting APKINDEX from binary package mirror failed!")
        target_folder = os.path.dirname(target)
        if not os.path.exists(target_folder):
            pmb.helpers.run.root(["mkdir", "-p", target_folder])
        pmb.helpers.run.root(["cp", temp, target])
    pmb.helpers.cli.progress_flush()

    if missing_ignored:
        logging.warn_once(
            "NOTE: ignoring missing APKINDEX due to PMB_APK_FORCE_MISSING_REPOSITORIES=1 or architecture without binary repo (fine during bootstrap)"
        )

    return True


def alpine_apkindex(repo: str = "main", arch: Arch | None = None) -> Apkindex:
    """
    Get the path to a specific Alpine APKINDEX file on disk and download it if necessary.

    :param repo: Alpine repository name (e.g. "main")
    :param arch: Alpine architecture (e.g. "armhf"), defaults to native arch.
    :returns: full path to the APKINDEX file
    """
    # Repo sanity check
    if repo not in ["main", "community", "testing", "non-free"]:
        raise RuntimeError(f"Invalid Alpine repository: {repo}")

    # Download the file
    arch = arch or Arch.native()
    update(arch)

    # Find it on disk
    channel_cfg = pmb.config.pmaports.read_config_channel()
    repo_link = f"{get_context().config.mirrors['alpine']}{channel_cfg['mirrordir_alpine']}/{repo}"
    cache_folder = get_context().config.work / (f"cache_apk_{arch}")
    return Apkindex(cache_folder / apkindex_hash(repo_link))
