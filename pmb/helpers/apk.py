# Copyright 2023 Johannes Marbach, Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
import os
import shlex
from collections.abc import Sequence
from pathlib import Path

import pmb.config.pmaports
import pmb.helpers.cli
import pmb.helpers.repo
import pmb.helpers.run
import pmb.helpers.run_core
import pmb.parse.version
from pmb.core.arch import Arch
from pmb.core.chroot import Chroot
from pmb.core.context import get_context
from pmb.helpers import logging
from pmb.types import PathString, RunOutputTypePopen


def update_repository_list(
    root: Path,
    user_repository: bool = False,
    alpine_only: bool = False,
) -> None:
    """
    Update /etc/apk/repositories, if it is outdated (when the user changed the
    --mirror-alpine or --mirror-pmOS parameters).

    :param root: the root directory to operate on
    :param alpine_only: only write Alpine's mirrors, for devices that set
                        deviceinfo_alpine_only
    """
    # Read old entries or create folder structure
    path = root / "etc/apk/repositories"
    lines_old: list[str] = []
    if path.exists():
        # Read all old lines
        lines_old = []
        with path.open() as handle:
            for line in handle:
                lines_old.append(line[:-1])
    else:
        pmb.helpers.run.root(["mkdir", "-p", path.parent])

    user_repo_dir = Path("/mnt/pmbootstrap/packages") if user_repository else None

    # Up to date: Save cache, return
    lines_new = pmb.helpers.repo.get_repos_from_config(
        user_repository=user_repo_dir, alpine_only=alpine_only
    )
    if lines_old == lines_new:
        return

    # Update the file
    logging.debug(f"({root.name}) update /etc/apk/repositories")
    if path.exists():
        pmb.helpers.run.root(["rm", path])
    for line in lines_new:
        pmb.helpers.run.root(["sh", "-c", f"echo {shlex.quote(line)} >> {path}"])

    # Verify that we properly wrote the data
    with path.open() as handle:
        lines_old = handle.read().splitlines()
        if lines_old != lines_new:
            raise RuntimeError(f"Failed to update: {path}: old: {lines_old}, new: {lines_new}")


def _prepare_fifo() -> Path:
    """
    Prepare the progress fifo for reading / writing.

    :param chroot: whether to run the command inside the chroot or on the host
    :param suffix: chroot suffix. Only applies if the "chroot" parameter is
                   set to True.
    :returns: A tuple consisting of the path to the fifo as needed by apk to
              write into it (relative to the chroot, if applicable) and the
              path of the fifo as needed by cat to read from it (always
              relative to the host)
    """
    pmb.helpers.run.root(["mkdir", "-p", get_context().config.work / "tmp"])
    fifo = get_context().config.work / "tmp/apk_progress_fifo"
    if os.path.exists(fifo):
        pmb.helpers.run.root(["rm", "-f", fifo])

    pmb.helpers.run.root(["mkfifo", fifo])
    return fifo


def _create_command_with_progress(command: Sequence[str], fifo: Path) -> list[str]:
    """
    Build a full apk command from a subcommand, set up to redirect progress into a fifo.

    :param command: apk subcommand in list form
    :param fifo: path of the fifo
    :returns: full command in list form
    """
    flags = ["--progress-fd", "3"]
    command_full = [command[0], *flags, *command[1:]]
    command_flat = pmb.helpers.run_core.flat_cmd([command_full])
    command_flat = f"exec 3>{fifo}; {command_flat}"
    return ["sh", "-c", command_flat]


def _compute_progress(line: str) -> float:
    """
    Compute the progress as a number between 0 and 1.

    :param line: line as read from the progress fifo
    :returns: progress as a number between 0 and 1
    """
    if not line:
        return 1
    cur_tot = line.split(" ", maxsplit=1)[0].rstrip().split("/")
    if len(cur_tot) != 2:
        return 0
    cur = float(cur_tot[0])
    tot = float(cur_tot[1])
    return cur / tot if tot > 0 else 0


def _apk_with_progress(command: Sequence[str]) -> None:
    """
    Run an apk subcommand while printing a progress bar to STDOUT.

    :param command: apk subcommand in list form
    :raises RuntimeError: when the apk command fails
    """
    fifo = _prepare_fifo()
    command_with_progress = _create_command_with_progress(command, fifo)
    log_msg = " ".join(command)
    with (
        pmb.helpers.run.root(["cat", fifo], output=RunOutputTypePopen.PIPE) as p_cat,
        pmb.helpers.run.root(command_with_progress, output=RunOutputTypePopen.BACKGROUND) as p_apk,
    ):
        while p_apk.poll() is None:
            p_cat_stdout = p_cat.stdout
            if p_cat_stdout is None:
                raise RuntimeError("cat process had no stdout?")
            line = p_cat_stdout.readline().decode("utf-8")
            progress = _compute_progress(line)
            pmb.helpers.cli.progress_print(progress)
        pmb.helpers.cli.progress_flush()
        pmb.helpers.run_core.check_return_code(p_apk.returncode, log_msg)


def _prepare_cmd(command: Sequence[PathString], chroot: Chroot | None) -> list[str]:
    """
    Prepare the apk command.

    Returns a tuple of the first part of the command with generic apk flags, and the second part
    with the subcommand and its arguments.
    """
    config = get_context().config
    # Our _apk_with_progress() wrapper also need --no-progress, since all that does is
    # prevent apk itself from rendering progress bars. We instead want it to tell us
    # the progress so we can render it. So we always set --no-progress.
    command_: list[str] = [str(config.work / "apk.static"), "--no-progress"]
    if chroot:
        cache_dir = config.work / f"cache_apk_{chroot.arch}"
        command_.extend(
            [
                "--root",
                str(chroot.path),
                "--arch",
                str(chroot.arch),
            ]
        )

        if os.getenv("PMB_APK_NO_CACHE") == "1":
            command_.extend(["--no-cache"])
        else:
            command_.extend(["--cache-dir", str(cache_dir)])

    local_repos = pmb.helpers.repo.get_repos_from_config(
        user_repository=config.work / "packages", mirrors_exclude=True
    )
    for repo in local_repos:
        command_.extend(["--repository", repo])

    if get_context().offline:
        command_.append("--no-network")

    for c in command:
        command_.append(os.fspath(c))

        # Always be non-interactive
        if c == "add":
            command_.append("--no-interactive")

    return command_


def run(command: Sequence[PathString], chroot: Chroot, with_progress: bool = True) -> None:
    """
    Run an apk subcommand.

    :param command: apk subcommand in list form
    :param with_progress: whether to print a progress bar
    :param chroot: chroot to run the command in
    :raises RuntimeError: when the apk command fails
    """
    command_ = _prepare_cmd(command, chroot)

    # Sanity checks. We should avoid accidentally writing to
    # /var/cache/apk on the host!
    if "add" in command:
        if "--no-interactive" not in command_:
            raise RuntimeError(
                "Encountered an 'apk add' command without --no-interactive! This is a bug."
            )
        if os.getenv("PMB_APK_NO_CACHE") == "1" and "--no-cache" not in command_:
            raise RuntimeError(
                "Encountered an 'apk add' command without --no-cache! This is a bug."
            )
        if os.getenv("PMB_APK_NO_CACHE") != "1" and "--cache-dir" not in command_:
            raise RuntimeError(
                "Encountered an 'apk add' command without --cache-dir! This is a bug."
            )

    if with_progress:
        _apk_with_progress(command_)
    else:
        pmb.helpers.run.root(command_)


def cache_clean(arch: Arch) -> None:
    """Clean the APK cache for a specific architecture."""
    work = get_context().config.work
    cache_dir = work / f"cache_apk_{arch}"
    if not cache_dir.exists():
        return

    # The problem here is that apk's "cache clean" command really
    # expects to be run against an apk-managed rootfs and will return
    # errors if it can't access certain paths (even though it will
    # actually clean the cache like we want).
    # We could just ignore the return value, but then we wouldn't know
    # if something actually went wrong with apk...
    # So we do this dance of creating a rootfs with only the files that
    # APK needs to be happy
    tmproot = work / "tmp_apk_root"  # pmb#2491: not using tmp/apk_root
    if not (tmproot / "etc/apk/repositories").exists():
        tmproot.mkdir(exist_ok=True)
        (tmproot / "var/cache").mkdir(exist_ok=True, parents=True)
        (tmproot / "etc/apk").mkdir(exist_ok=True, parents=True)
        (tmproot / "lib/apk/db").mkdir(exist_ok=True, parents=True)

        (tmproot / "etc/apk/world").touch(exist_ok=True)
        (tmproot / "lib/apk/db/installed").touch(exist_ok=True)
        (tmproot / "lib/apk/db/triggers").touch(exist_ok=True)

        (tmproot / "etc/apk/keys").symlink_to(work / "config_apk_keys")

        # Our fake rootfs needs a valid repositories file for apk
        # to have something to compare the cache against
        update_repository_list(tmproot)

    # Point our tmproot cache dir to the real cache dir
    # this is much simpler than passing --cache-dir to apk
    # since even with that flag apk will also check it's
    # "static cache".
    (tmproot / "var/cache/apk").unlink(missing_ok=True)
    (tmproot / "var/cache/apk").symlink_to(cache_dir)

    command: list[PathString] = [
        "-v",
        "--root",
        tmproot,
        "--arch",
        str(arch),
    ]

    command += ["cache", "clean"]
    command_ = _prepare_cmd(command, None)

    pmb.helpers.apk_static.init()
    pmb.helpers.run.root(command_)


def check_outdated(version_installed: str) -> None:
    """
    Check if the provided alpine version is outdated.

    This depends on the alpine mirrordir (edge, v3.12, ...) related to currently checked out
    pmaports branch.

    :param version_installed: currently installed apk version, e.g. "2.12.1-r0"
    :raises: RuntimeError if the version is outdated
    """
    channel_cfg = pmb.config.pmaports.read_config_channel()
    mirrordir_alpine = channel_cfg["mirrordir_alpine"]
    version_min = pmb.config.apk_tools_min_version[mirrordir_alpine]

    if pmb.parse.version.compare(version_installed, version_min) >= 0:
        return

    raise RuntimeError(
        f"Found an outdated version of the 'apk' package manager ({version_installed}, expected at least: {version_min})"
    )
