import argparse
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sys

from . import deps, env, machine_spec

OPTION_DEFS_PATTERN = re.compile(r"\boption\s*\(\s*'([^']+)'.*?,(.+?)\)", re.DOTALL)
OPTION_PROP_KEYS_PATTERN = re.compile(r"\b(\w+)\s*:")


def main():
    project_srcroot = Path(sys.argv.pop(1)).resolve()

    parser = argparse.ArgumentParser(prog="configure",
                                     add_help=False)
    opts = parser.add_argument_group(title="generic options")
    opts.add_argument("-h", "--help",
                      help="show this help message and exit",
                      action="help")
    opts.add_argument("--prefix",
                      help="install files in PREFIX",
                      metavar="PREFIX",
                      type=parse_prefix)
    opts.add_argument("--build",
                      help="configure for building on BUILD",
                      metavar="BUILD",
                      type=machine_spec.parse)
    opts.add_argument("--host",
                      help="cross-compile to build binaries to run on HOST",
                      metavar="HOST",
                      type=machine_spec.parse)
    opts.add_argument("--enable-shared",
                      help="enable building shared libraries (default: disabled)",
                      action="store_true")
    opts.add_argument("--with-meson",
                      help="which Meson implementation to use (default: internal)",
                      choices=["internal", "system"],
                      dest="meson",
                      default="internal")
    opts.add_argument(f"--without-prebuilds",
                      help="do not make use of prebuilt bundles",
                      metavar="{" + ",".join(query_supported_bundle_types(include_wildcards=True)) + "}",
                      type=parse_bundle_type_set,
                      default=set())
    opts.add_argument("extra_meson_options",
                      nargs="*",
                      help=argparse.SUPPRESS)

    meson_options_file = project_srcroot / "meson.options"
    if not meson_options_file.exists():
        meson_options_file = project_srcroot / "meson_options.txt"
    if meson_options_file.exists():
        meson_group = parser.add_argument_group(title="project-specific options")
        meson_opts = register_meson_options(meson_options_file.read_text(encoding="utf-8"), meson_group)

    options = parser.parse_args()

    work_dir = Path(os.getcwd())
    if work_dir.is_relative_to(project_srcroot):
        build_dir = project_srcroot / "build"
    else:
        build_dir = work_dir
    if build_dir.exists():
        if (build_dir / "build.ninja").exists():
            print(f"Already configured. Wipe .{os.sep}{build_dir.relative_to(work_dir)} to reconfigure.",
                  file=sys.stderr)
            sys.exit(1)
        for item in build_dir.iterdir():
            shutil.rmtree(item, ignore_errors=True)

    default_library = "shared" if options.enable_shared else "static"

    allowed_prebuilds = set(query_supported_bundle_types(include_wildcards=False)) - options.without_prebuilds

    exit_status = configure(project_srcroot,
                            build_dir,
                            options.prefix,
                            options.build,
                            options.host,
                            default_library,
                            allowed_prebuilds,
                            options.meson,
                            collect_meson_options(options))

    sys.exit(exit_status)


def configure(project_srcroot,
              build_dir,
              prefix=None,
              build_machine=None,
              host_machine=None,
              default_library="static",
              allowed_prebuilds=None,
              meson="internal",
              extra_meson_options=[]):
    if prefix is None:
        prefix = env.detect_native_default_prefix()

    if build_machine is None:
        build_machine = env.detect_native_machine()

    if host_machine is None:
        host_machine = build_machine

    if host_machine.os == "windows":
        vs_arch = os.environ.get("VSCMD_ARG_TGT_ARCH", None)
        if vs_arch == "x86":
            host_machine = machine_spec.MachineSpec("windows", "x86", host_machine.config)
        if build_machine.os == "windows" and build_machine.arch == "x86_64" and host_machine.arch == "x86":
            build_machine = host_machine

    if allowed_prebuilds is None:
        allowed_prebuilds = set(query_supported_bundle_types(include_wildcards=False))

    meson_options = [
        f"--prefix={prefix}",
        f"--default-library={default_library}",
        "-Doptimization=s",
        "-Db_vscrt=mt",
    ]
    extra_paths = []

    if len(allowed_prebuilds) != 0:
        raw_deps_dir = os.environ.get("FRIDA_DEPS", None)
        if raw_deps_dir is not None:
            deps_dir = Path(raw_deps_dir)
        else:
            deps_dir = project_srcroot / "deps"

    allow_prebuilt_toolchain = "toolchain" in allowed_prebuilds
    if allow_prebuilt_toolchain:
        try:
            toolchain_prefix = env.ensure_toolchain(build_machine, deps_dir)
        except deps.BundleNotFoundError as e:
            print_toolchain_not_found_error(e)
            return 1
        except Exception as e:
            print_toolchain_unknown_error(e)
            return 2
        extra_paths += [toolchain_prefix / "bin"]
    else:
        toolchain_prefix = None

    is_cross_build = host_machine != build_machine

    build_sdk_prefix = None
    required = {"sdk:build"}
    if not is_cross_build:
        required.add("sdk:host")
    if allowed_prebuilds.issuperset(required):
        try:
            build_sdk_prefix = env.ensure_sdk(build_machine, deps_dir)
        except deps.BundleNotFoundError as e:
            print_sdk_not_found_error(e)
            return 3
        except Exception as e:
            print_sdk_unknown_error(e)
            return 4

    host_sdk_prefix = None
    if is_cross_build and "sdk:host" in allowed_prebuilds:
        try:
            host_sdk_prefix = env.ensure_sdk(host_machine, deps_dir)
        except deps.BundleNotFoundError as e:
            print_sdk_not_found_error(e)
            return 5
        except Exception as e:
            print_sdk_unknown_error(e)
            return 6

    if "sdk:build" in allowed_prebuilds and "sdk:host" in allowed_prebuilds:
        meson_options += ["-Dwrap_mode=nofallback"]

    call_selected_meson = lambda argv, *args, **kwargs: env.call_meson(argv,
                                                                       use_submodule=meson == "internal",
                                                                       *args, **kwargs)
    try:
        native_file, cross_file, machine_paths, machine_env = \
                env.generate_machine_files(build_machine, build_sdk_prefix,
                                           host_machine, host_sdk_prefix,
                                           toolchain_prefix, default_library,
                                           call_selected_meson, build_dir)
    except Exception as e:
        print(f"Unable to generate machine files: {e}", file=sys.stderr)
        return 7
    meson_options += [f"--native-file={native_file}"]
    if cross_file is not None:
        meson_options += [f"--cross-file={cross_file}"]
    extra_paths += machine_paths

    raw_extra_paths = [str(p) for p in extra_paths]

    meson_env = {**os.environ, **machine_env}
    meson_env["PATH"] = os.pathsep.join(raw_extra_paths) + os.pathsep + meson_env["PATH"]

    process = call_selected_meson(["setup"] + meson_options + extra_meson_options + [build_dir],
                                  cwd=project_srcroot,
                                  env=meson_env)

    makefile_path = build_dir / "Makefile"
    if not makefile_path.exists():
        in_tree = (project_srcroot / "Makefile").read_text(encoding="utf-8")
        out_of_tree = in_tree \
                .replace('"$(shell pwd)"', shlex.quote(str(project_srcroot))) \
                .replace('./build', ".")
        makefile_path.write_text(out_of_tree)

        if platform.system() == "Windows":
            in_tree = (project_srcroot / "make.bat").read_text(encoding="utf-8")
            out_of_tree = in_tree \
                    .replace('"%dp0%"', '"' + str(project_srcroot) + '"') \
                    .replace('.\\build', ".")
            (build_dir / "make.bat").write_text(out_of_tree)

    env_config = {
        "meson": meson,
        "paths": raw_extra_paths,
        "env": machine_env,
    }
    (build_dir / "frida-env-config.json").write_text(json.dumps(env_config, indent=2), encoding="utf-8")

    return process.returncode


def print_toolchain_not_found_error(e):
    print(f"Unable to download toolchain: {e}", file=sys.stderr)
    print(f"Specify --without-prebuilds=toolchain to only use tools on your PATH.", file=sys.stderr)


def print_toolchain_unknown_error(e):
    print(f"Unable to prepare toolchain: {e}", file=sys.stderr)


def print_sdk_not_found_error(e):
    print(f"Unable to download SDK: {e}", file=sys.stderr)
    print(f"Specify --without-prebuilds=sdk[:{{build|host}}] to build dependencies from source code.", file=sys.stderr)


def print_sdk_unknown_error(e):
    print(f"Unable to prepare SDK: {e}", file=sys.stderr)


def parse_prefix(raw_prefix):
    prefix = Path(raw_prefix)
    if not prefix.is_absolute():
        prefix = Path(os.getcwd()) / prefix
    return prefix


def query_supported_bundle_types(include_wildcards):
    for e in deps.Bundle:
        identifier = e.name.lower()
        if e == deps.Bundle.SDK:
            if include_wildcards:
                yield identifier
            yield identifier + ":build"
            yield identifier + ":host"
        else:
            yield identifier


def query_supported_bundle_type_values():
    return [e for e in deps.Bundle]


def parse_bundle_type_set(raw_array):
    supported_types = list(query_supported_bundle_types(include_wildcards=True))
    result = set()
    for element in raw_array.split(","):
        bundle_type = element.strip()
        if bundle_type not in supported_types:
            pretty_choices = "', '".join(supported_types)
            raise argparse.ArgumentTypeError(f"invalid bundle type: '{bundle_type}' (choose from '{pretty_choices}')")
        if bundle_type == "sdk":
            result.add("sdk:build")
            result.add("sdk:host")
        else:
            result.add(bundle_type)
    return result


def register_meson_options(meson_option_defs, group):
    hidden_constants = {
        "true": True,
        "false": False,
    }

    for match in OPTION_DEFS_PATTERN.finditer(meson_option_defs):
        name = match.group(1)
        pretty_name = name.replace("_", "-")

        raw_spec = OPTION_PROP_KEYS_PATTERN.sub(lambda m: '"' + m.group(1) + '":', match.group(2)) \
                .replace("\n", " ")
        spec = eval("{" + raw_spec + "}", hidden_constants)

        option_type = spec["type"]
        if option_type in {"boolean", "feature"}:
            default_value = spec.get("value", None)

            if option_type == "boolean":
                value_when_enabled = "true"
                value_when_disabled = "false"
            else:
                value_when_enabled = "enabled"
                value_when_disabled = "disabled"

            if default_value != value_when_enabled:
                action = "enable"
                value_to_set = value_when_enabled
            else:
                action = "disable"
                value_to_set = value_when_disabled

            group.add_argument(f"--{action}-{pretty_name}",
                               action="append_const",
                               const=f"-D{name}={value_to_set}",
                               dest="main_meson_options",
                               **parse_option_meta(name, spec))
            if option_type == "feature" and default_value == "auto":
                group.add_argument(f"--disable-{pretty_name}",
                                   action="append_const",
                                   const=f"-D{name}=disabled",
                                   dest="main_meson_options",
                                   **parse_option_meta(name, spec))
        elif option_type == "combo":
            group.add_argument(f"--with-{pretty_name}",
                               choices=spec["choices"],
                               dest="meson_option:" + name,
                               **parse_option_meta(name, spec))
        elif option_type == "array":
            group.add_argument(f"--with-{pretty_name}",
                               dest="meson_option:" + name,
                               type=make_array_option_value_parser(spec),
                               **parse_option_meta(name, spec))
        else:
            group.add_argument(f"--with-{pretty_name}",
                               dest="meson_option:" + name,
                               **parse_option_meta(name, spec))


def parse_option_meta(name, spec):
    params = {}

    otype = spec["type"]

    desc = spec.get("description", None)
    if desc is not None:
        val = spec.get("value", None)
        if val is None:
            if otype == "string":
                val = ""
            elif otype == "boolean":
                val = True
            elif otype == "combo":
                val = spec["choices"][0]
            elif otype == "integer":
                val = 0
            elif otype == "array":
                val = []
            elif otype == "feature":
                val = "auto"
        params["help"] = f"{desc} (default: {str(val).lower()})"

    choices = spec.get("choices", None)
    if choices is not None:
        delimiter = "|" if otype == "combo" else ","
        metavar = "{" + delimiter.join(choices) + "}"
    else:
        metavar = name.upper()
    params["metavar"] = metavar

    return params


def collect_meson_options(options):
    result = []

    for raw_name, raw_val in vars(options).items():
        if raw_val is None:
            continue
        if raw_name == "main_meson_options":
            result += raw_val
        if raw_name.startswith("meson_option:"):
            name = raw_name[13:]
            val = raw_val if isinstance(raw_val, str) else ",".join(raw_val)
            result += [f"-D{name}={val}"]

    result += options.extra_meson_options

    return result


def make_array_option_value_parser(option_spec):
    return lambda v: parse_array_option_value(v, option_spec)


def parse_array_option_value(v, option_spec):
    vals = [v.strip() for v in v.split(",")]

    choices = option_spec.get("choices", None)
    if choices is not None:
        for v in vals:
            if v not in choices:
                pretty_choices = "', '".join(choices)
                raise argparse.ArgumentTypeError(f"invalid array value: '{v}' (choose from '{pretty_choices}')")

    return vals
