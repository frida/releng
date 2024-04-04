import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from typing import List

from . import env
from .meson_configure import configure


STANDARD_TARGET_NAMES = ["all", "clean", "distclean", "install", "test"]


def main():
    default_sourcedir = Path(sys.argv.pop(1)).resolve()
    sourcedir = Path(os.environ.get("FRIDA_SOURCEDIR", default_sourcedir)).resolve()

    default_builddir = Path(sys.argv.pop(1)).resolve()
    builddir = Path(os.environ.get("FRIDA_BUILDDIR", default_builddir)).resolve()

    parser = argparse.ArgumentParser(prog="make")
    parser.add_argument("targets",
                        help="Targets to build, e.g.: " + ", ".join(STANDARD_TARGET_NAMES),
                        nargs="*",
                        default="all")
    options = parser.parse_args()

    targets = options.targets
    if isinstance(targets, str):
        targets = [targets]

    exit_status = make(sourcedir, builddir, targets)

    sys.exit(exit_status)


def make(sourcedir: Path, builddir: Path, targets: List[str]):
    if not (builddir / "build.ninja").exists():
        exit_status = configure(sourcedir, builddir)
        if exit_status != 0:
            return exit_status

    env_config = json.loads((builddir / "frida-env-config.json").read_text(encoding="utf-8"))

    meson_env = {**os.environ, **env_config["env"]}
    paths = env_config["paths"]
    if paths:
        meson_env["PATH"] = os.pathsep.join(paths) + os.pathsep + meson_env["PATH"]

    compile_options = []
    if os.environ.get("V", None) == "1":
        compile_options += ["-v"]

    test_options = shlex.split(os.environ.get("FRIDA_TEST_OPTIONS", "-v"))

    standard_targets = {
        "all": ["compile"] + compile_options,
        "clean": ["compile", "--clean"] + compile_options,
        "distclean": lambda: distclean(sourcedir, builddir),
        "install": ["install"],
        "test": ["test"] + test_options,
    }

    def do_meson_command(args):
        return env.call_meson(args,
                              use_submodule=env_config["meson"] == "internal",
                              cwd=builddir,
                              env=meson_env).returncode

    exit_status = 0
    pending_targets = targets.copy()
    pending_compile = None

    while pending_targets:
        target = pending_targets.pop(0)

        action = standard_targets.get(target, None)
        if action is None:
            meson_command = "compile"
        elif not callable(action):
            meson_command = action[0]
        else:
            meson_command = None

        if meson_command == "compile":
            if pending_compile is None:
                pending_compile = ["compile"]
            if action is not None:
                pending_compile += action[1:]
            else:
                pending_compile += [target]
            continue

        if pending_compile is not None:
            exit_status = do_meson_command(pending_compile)
            pending_compile = None
            if exit_status != 0:
                break

        if meson_command is not None:
            exit_status = do_meson_command(action)
            if exit_status != 0:
                break
        else:
            action()

    if exit_status == 0 and pending_compile is not None:
        exit_status = do_meson_command(pending_compile)

    return exit_status


def distclean(sourcedir: Path, builddir: Path):
    items_to_delete = []

    if not builddir.is_relative_to(sourcedir):
        items_to_delete += list(builddir.iterdir())

    items_to_delete += [
        sourcedir / "build",
        sourcedir / "deps",
    ]

    for item in items_to_delete:
        try:
            shutil.rmtree(item)
        except:
            pass
