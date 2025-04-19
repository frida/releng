import argparse
import os
from pathlib import Path
import pickle
import shlex
import shutil
import sys
from typing import Callable

from . import env
from .meson_configure import configure


STANDARD_TARGET_NAMES = ["all", "clean", "distclean", "install", "test"]


def main():
    print("[DEBUG] meson_make.py main() started", file=sys.stderr)
    default_sourcedir = Path(sys.argv.pop(1)).resolve()
    sourcedir = Path(os.environ.get("MESON_SOURCE_ROOT", default_sourcedir)).resolve()
    print(f"[INFO] Sourcedir: {sourcedir}", file=sys.stderr)

    default_builddir = Path(sys.argv.pop(1)).resolve()
    builddir = Path(os.environ.get("MESON_BUILD_ROOT", default_builddir)).resolve()
    print(f"[INFO] Builddir: {builddir}", file=sys.stderr)

    parser = argparse.ArgumentParser(prog="make")
    parser.add_argument("targets",
                        help="Targets to build, e.g.: " + ", ".join(STANDARD_TARGET_NAMES),
                        nargs="*",
                        default="all")
    options = parser.parse_args()

    targets = options.targets
    if isinstance(targets, str):
        targets = [targets]
    print(f"[INFO] Parsed targets: {targets}", file=sys.stderr)

    try:
        make(sourcedir, builddir, targets)
        print("[DEBUG] meson_make.py main() finished successfully", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] Exception caught in main: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


def make(sourcedir: Path,
         builddir: Path,
         targets: list[str],
         environ: dict[str, str] = os.environ,
         call_meson: Callable = env.call_meson):
    print(f"[INFO] make called with sourcedir={sourcedir}, builddir={builddir}, targets={targets}", file=sys.stderr)
    if not (builddir / "build.ninja").exists():
        print("[INFO] build.ninja not found, calling configure...", file=sys.stderr)
        configure(sourcedir, builddir, environ=environ)
        print("[INFO] configure finished.", file=sys.stderr)

    compile_options = []
    if environ.get("V") == "1":
        print("[DEBUG] Verbose mode enabled (V=1)", file=sys.stderr)
        compile_options += ["-v"]

    test_options = shlex.split(environ.get("FRIDA_TEST_OPTIONS", "-v"))
    print(f"[DEBUG] Test options: {test_options}", file=sys.stderr)

    standard_targets = {
        "all": ["compile"] + compile_options,
        "clean": ["compile", "--clean"] + compile_options,
        "distclean": lambda: distclean(sourcedir, builddir),
        "install": ["install"],
        "test": ["test"] + test_options,
    }
    
    env_state_path = builddir / "frida-env.dat"
    print(f"[DEBUG] Loading env state from: {env_state_path}", file=sys.stderr)
    env_state = pickle.loads(env_state_path.read_bytes())
    print(f"[DEBUG] Env state loaded: meson={env_state.get('meson')}, deps={env_state.get('deps')}, allowed_prebuilds={env_state.get('allowed_prebuilds')}", file=sys.stderr)

    machine_config = env_state["host"]
    if machine_config is None:
        print("[DEBUG] Using build machine config as host config", file=sys.stderr)
        machine_config = env_state["build"]
    meson_env = machine_config.make_merged_environment(environ)
    meson_env["FRIDA_ALLOWED_PREBUILDS"] = ",".join(env_state["allowed_prebuilds"])
    meson_env["FRIDA_DEPS"] = str(env_state["deps"])
    print(f"[DEBUG] Meson env includes FRIDA_ALLOWED_PREBUILDS={meson_env.get('FRIDA_ALLOWED_PREBUILDS')}, FRIDA_DEPS={meson_env.get('FRIDA_DEPS')}", file=sys.stderr)
    # Avoid printing the full environment as it can be very large and contain sensitive info
    # print(f"[DEBUG] Full meson environment prepared: {meson_env}", file=sys.stderr)

    def do_meson_command(args):
        print(f"[INFO] Executing Meson command: {args} in {builddir} with internal meson: {env_state['meson'] == 'internal'}", file=sys.stderr, flush=True)
        call_meson(args,
                   use_submodule=env_state["meson"] == "internal",
                   cwd=builddir,
                   env=meson_env,
                   check=True)
        print(f"[INFO] Meson command finished: {args}", file=sys.stderr)

    pending_targets = targets.copy()
    pending_compile = None

    while pending_targets:
        target = pending_targets.pop(0)
        print(f"[INFO] Processing target: {target}", file=sys.stderr)

        action = standard_targets.get(target, None)
        if action is None:
            meson_command = "compile"
            print(f"[DEBUG] Non-standard target '{target}', treating as compile argument.", file=sys.stderr)
        elif not callable(action):
            meson_command = action[0]
            print(f"[DEBUG] Standard target '{target}' maps to meson command: {action}", file=sys.stderr)
        else:
            meson_command = None
            print(f"[DEBUG] Standard target '{target}' maps to callable action.", file=sys.stderr)

        if meson_command == "compile":
            if pending_compile is None:
                pending_compile = ["compile"]
                print("[DEBUG] Initializing pending compile command.", file=sys.stderr)
            if action is not None:
                pending_compile += action[1:]
            else:
                pending_compile += [target]
            print(f"[DEBUG] Added '{target}' to pending compile command: {pending_compile}", file=sys.stderr)
            continue

        if pending_compile is not None:
            print(f"[INFO] Executing batched compile command: {pending_compile}", file=sys.stderr)
            do_meson_command(pending_compile)
            pending_compile = None
            print("[DEBUG] Cleared pending compile command.", file=sys.stderr)

        if meson_command is not None:
            print(f"[INFO] Executing action for target '{target}': {action}", file=sys.stderr)
            do_meson_command(action)
        else:
            print(f"[INFO] Calling custom action for target '{target}'", file=sys.stderr)
            action()

    if pending_compile is not None:
        print(f"[INFO] Executing final batched compile command: {pending_compile}", file=sys.stderr)
        do_meson_command(pending_compile)
        print("[DEBUG] Finished final batched compile command.", file=sys.stderr)
    print("[INFO] make function finished.", file=sys.stderr)


def distclean(sourcedir: Path, builddir: Path):
    print(f"[INFO] Starting distclean for sourcedir={sourcedir}, builddir={builddir}", file=sys.stderr)
    items_to_delete = []

    if not builddir.is_relative_to(sourcedir):
        print(f"[DEBUG] Build directory {builddir} is outside source directory {sourcedir}, adding its contents to delete list.", file=sys.stderr)
        items_to_delete += list(builddir.iterdir())
    else:
        print(f"[DEBUG] Build directory {builddir} is inside source directory {sourcedir}, not deleting its contents directly.", file=sys.stderr)


    items_to_delete += [
        sourcedir / "build",
        sourcedir / "deps",
    ]
    print(f"[DEBUG] Items targeted for deletion: {items_to_delete}", file=sys.stderr)

    for item in items_to_delete:
        print(f"[INFO] Attempting to delete: {item}", file=sys.stderr)
        try:
            if item.is_dir():
                 shutil.rmtree(item)
                 print(f"[INFO] Successfully deleted directory: {item}", file=sys.stderr)
            elif item.is_file():
                 item.unlink()
                 print(f"[INFO] Successfully deleted file: {item}", file=sys.stderr)
            else:
                 print(f"[WARNING] Item not found or not a file/directory: {item}", file=sys.stderr)
        except Exception as ex:
            print(f"[WARNING] Failed to delete {item}: {ex}", file=sys.stderr)
    print("[INFO] distclean finished.", file=sys.stderr)

# Ensure main execution context if script is run directly
if __name__ == "__main__":
    main()
