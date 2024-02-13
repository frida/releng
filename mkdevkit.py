#!/usr/bin/env python3

import argparse
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
from releng import devkit, env, machine_spec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kit")
    parser.add_argument("machine",
                        type=machine_spec.parse)
    parser.add_argument("outdir",
                        type=Path)
    parser.add_argument("-t", "--thin",
                        help="build without cross-arch support",
                        action="store_const",
                        dest="flavor",
                        const="_thin",
                        default="")
    parser.add_argument("--cc",
                        help="C compiler to use",
                        type=parse_array_option_value)
    machine_options = dict.fromkeys(["c_args", "lib", "libtool", "ar", "nm", "objcopy", "pkg_config", "pkg_config_path"])
    for name in machine_options.keys():
        pretty_name = name.replace("_", "-")
        parser.add_argument("--" + pretty_name,
                            help=f"The {pretty_name} to use",
                            type=parse_array_option_value)

    options = parser.parse_args()

    kit = options.kit
    machine = options.machine
    outdir = options.outdir.resolve()
    flavor = options.flavor

    cc = options.cc
    if cc is not None:
        meson_config = {"c": cc}
        for k, v in vars(options).items():
            if k in machine_options and v is not None:
                meson_config[k] = v
    else:
        build_dir = REPO_ROOT / "build"

        if flavor == "":
            fat_machine_file = env.query_machine_file_path(machine, flavor, build_dir)
            if not fat_machine_file.exists() \
                    and env.query_machine_file_path(machine, "_thin", build_dir).exists():
                flavor = "_thin"

        meson_config = env.load_meson_config(machine, flavor, build_dir)

    try:
        app = devkit.CompilerApplication(kit, machine, flavor, meson_config, outdir)
        app.run()
    except subprocess.CalledProcessError as e:
        print(e, file=sys.stderr)
        print("Output:", e.output, file=sys.stderr)
        sys.exit(1)


def parse_array_option_value(raw_array):
    if raw_array == "":
        return None
    return raw_array.split("!")


if __name__ == "__main__":
    main()
