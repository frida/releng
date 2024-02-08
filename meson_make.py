import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import sys

from . import env
from .meson_configure import configure


def main():
    project_srcroot = Path(sys.argv.pop(1)).resolve()
    build_dir = Path(sys.argv.pop(1)).resolve()

    parser = argparse.ArgumentParser(prog="make")
    parser.add_argument("targets",
                        nargs="*",
                        choices=["all", "clean", "distclean", "install", "test"],
                        default="all")
    options = parser.parse_args()

    targets = options.targets
    if isinstance(targets, str):
        targets = [targets]

    exit_status = make(project_srcroot, build_dir, targets)

    sys.exit(exit_status)


def make(project_srcroot, build_dir, targets):
    if not (build_dir / "build.ninja").exists():
        exit_status = configure(project_srcroot, build_dir)
        if exit_status != 0:
            return exit_status

    env_config = json.loads((build_dir / "frida-env-config.json").read_text(encoding="utf-8"))

    meson_env = {**os.environ, **env_config["env"]}
    meson_env["PATH"] = os.pathsep.join(env_config["paths"]) + os.pathsep + meson_env["PATH"]

    exit_status = 0

    for target in targets:
        if target == "distclean":
            items_to_delete = []

            if not build_dir.is_relative_to(project_srcroot):
                items_to_delete += list(build_dir.iterdir())

            items_to_delete += [
                project_srcroot / "build",
                project_srcroot / "deps",
            ]

            for item in items_to_delete:
                try:
                    shutil.rmtree(item)
                except:
                    pass

            continue

        command = "compile" if target in {"all", "clean"} else target

        options = []
        if target == "clean":
            options += ["--clean"]
        elif target == "test":
            options += shlex.split(os.environ.get("FRIDA_TEST_OPTIONS", "-v"))

        if command == "compile" and os.environ.get("V", None) == "1":
            options += ["-v"]

        exit_status = env.call_meson([command] + options,
                                     use_submodule=env_config["meson"] == "internal",
                                     cwd=build_dir,
                                     env=meson_env).returncode
        if exit_status != 0:
            return exit_status

    return exit_status
