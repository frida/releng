import argparse
import os
from pathlib import Path
import pickle
import platform
import shlex
import shutil
import subprocess
import sys
from typing import Any, Callable, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).parent / "meson"))
import mesonbuild.interpreter
from mesonbuild.coredata import UserArrayOption, UserBooleanOption, \
        UserComboOption, UserFeatureOption, UserOption, UserStringOption

from . import deps, env
from .machine_spec import MachineSpec


def main():
    default_sourcedir = Path(sys.argv.pop(1))
    sourcedir = Path(os.environ.get("FRIDA_SOURCEDIR", default_sourcedir)).resolve()

    workdir = Path(os.getcwd())
    if workdir.is_relative_to(sourcedir):
        default_builddir = sourcedir / "build"
    else:
        default_builddir = workdir
    builddir = Path(os.environ.get("FRIDA_BUILDDIR", default_builddir)).resolve()

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
                      type=MachineSpec.parse)
    opts.add_argument("--host",
                      help="cross-compile to build binaries to run on HOST",
                      metavar="HOST",
                      type=MachineSpec.parse)
    opts.add_argument("--enable-symbols",
                      help="build binaries with debug symbols included (default: disabled)",
                      action="store_true")
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

    meson_options_file = sourcedir / "meson.options"
    if not meson_options_file.exists():
        meson_options_file = sourcedir / "meson_options.txt"
    if meson_options_file.exists():
        meson_group = parser.add_argument_group(title="project-specific options")
        meson_opts = register_meson_options(meson_options_file, meson_group)

    options = parser.parse_args()

    if builddir.exists():
        if (builddir / "build.ninja").exists():
            print(f"Already configured. Wipe .{os.sep}{builddir.relative_to(workdir)} to reconfigure.",
                  file=sys.stderr)
            sys.exit(1)

    default_library = "shared" if options.enable_shared else "static"

    allowed_prebuilds = set(query_supported_bundle_types(include_wildcards=False)) - options.without_prebuilds

    exit_status = configure(sourcedir,
                            builddir,
                            options.prefix,
                            options.build,
                            options.host,
                            default_library,
                            allowed_prebuilds,
                            options.meson,
                            collect_meson_options(options))

    sys.exit(exit_status)


def configure(sourcedir: Path,
              builddir: Path,
              prefix: Optional[str] = None,
              build_machine: Optional[MachineSpec] = None,
              host_machine: Optional[MachineSpec] = None,
              default_library: str = "static",
              allowed_prebuilds: Sequence[str] = None,
              meson: str = "internal",
              extra_meson_options: List[str] = []):
    if prefix is None:
        prefix = env.detect_default_prefix()

    if build_machine is None:
        build_machine = MachineSpec.make_from_local_system()

    if host_machine is None:
        host_machine = build_machine

    if host_machine.os == "windows":
        vs_arch = os.environ.get("VSCMD_ARG_TGT_ARCH", None)
        if vs_arch == "x86":
            host_machine = MachineSpec("windows", "x86", host_machine.config)

    build_machine = build_machine.maybe_adapt_to_host(host_machine)

    if allowed_prebuilds is None:
        allowed_prebuilds = set(query_supported_bundle_types(include_wildcards=False))

    call_selected_meson = lambda argv, *args, **kwargs: env.call_meson(argv,
                                                                       use_submodule=meson == "internal",
                                                                       *args, **kwargs)

    meson_options = [
        f"-Dprefix={prefix}",
        f"-Ddefault_library={default_library}",
        "-Doptimization=s",
        "-Db_ndebug=true",
        "-Db_vscrt=mt",
    ]

    deps_dir = deps.detect_cache_dir(sourcedir)

    allow_prebuilt_toolchain = "toolchain" in allowed_prebuilds
    if allow_prebuilt_toolchain:
        try:
            toolchain_prefix, _ = deps.ensure_toolchain(build_machine, deps_dir)
        except deps.BundleNotFoundError as e:
            print_toolchain_not_found_error(e)
            return 1
        except Exception as e:
            print_toolchain_unknown_error(e)
            return 2
    else:
        if project_depends_on_vala_compiler(sourcedir):
            toolchain_prefix = deps.query_toolchain_prefix(build_machine, deps_dir)
            vala_compiler = deps.detect_toolchain_vala_compiler(toolchain_prefix, build_machine)
            if vala_compiler is None:
                try:
                    build_vala_compiler(toolchain_prefix, deps_dir, call_selected_meson)
                except subprocess.CalledProcessError as e:
                    print(e, file=sys.stderr)
                    print("Output:\n\t| " + "\n\t| ".join(e.output.strip().split("\n")), file=sys.stderr)
                    return 70
        else:
            toolchain_prefix = None

    is_cross_build = host_machine != build_machine

    build_sdk_prefix = None
    required = {"sdk:build"}
    if not is_cross_build:
        required.add("sdk:host")
    if allowed_prebuilds.issuperset(required):
        try:
            build_sdk_prefix, _ = deps.ensure_sdk(build_machine, deps_dir)
        except deps.BundleNotFoundError as e:
            print_sdk_not_found_error(e)
            return 3
        except Exception as e:
            print_sdk_unknown_error(e)
            return 4

    host_sdk_prefix = None
    if is_cross_build and "sdk:host" in allowed_prebuilds:
        try:
            host_sdk_prefix, _ = deps.ensure_sdk(host_machine, deps_dir)
        except deps.BundleNotFoundError as e:
            print_sdk_not_found_error(e)
            return 5
        except Exception as e:
            print_sdk_unknown_error(e)
            return 6

    try:
        build_config, host_config = \
                env.generate_machine_configs(build_machine,
                                             host_machine,
                                             os.environ,
                                             toolchain_prefix,
                                             build_sdk_prefix,
                                             host_sdk_prefix,
                                             call_selected_meson,
                                             default_library,
                                             builddir)
    except Exception as e:
        print(f"Unable to generate machine configurations: {e}", file=sys.stderr)
        return 7
    meson_options += [f"--native-file={build_config.machine_file}"]
    if host_config is not build_config:
        meson_options += [f"--cross-file={host_config.machine_file}"]

    process = call_selected_meson(["setup"] + meson_options + extra_meson_options + [builddir],
                                  cwd=sourcedir,
                                  env=host_config.make_merged_environment(os.environ))

    makefile_path = builddir / "Makefile"
    if not makefile_path.exists():
        in_tree = (sourcedir / "Makefile").read_text(encoding="utf-8")
        out_of_tree = in_tree \
                .replace('"$(shell pwd)"', shlex.quote(str(sourcedir))) \
                .replace("./build", ".") \
                .replace("releng/meson/meson.py",
                         shlex.quote(str(sourcedir / "releng" / "meson" / "meson.py")))
        makefile_path.write_text(out_of_tree)

        if platform.system() == "Windows":
            in_tree = (sourcedir / "make.bat").read_text(encoding="utf-8")
            out_of_tree = in_tree \
                    .replace('"%dp0%"', '"' + str(sourcedir) + '"') \
                    .replace('.\\build', ".")
            (builddir / "make.bat").write_text(out_of_tree)

    (builddir / "frida-env.dat").write_bytes(pickle.dumps({
        "meson": meson,
        "build": build_config,
        "host": host_config if host_config is not build_config else None,
        "deps": deps_dir,
    }))

    return process.returncode


def print_toolchain_not_found_error(e: deps.BundleNotFoundError):
    print(f"Unable to download toolchain: {e}", file=sys.stderr)
    print(f"Specify --without-prebuilds=toolchain to only use tools on your PATH.", file=sys.stderr)


def print_toolchain_unknown_error(e: Exception):
    print(f"Unable to prepare toolchain: {e}", file=sys.stderr)


def print_sdk_not_found_error(e: deps.BundleNotFoundError):
    print(f"Unable to download SDK: {e}", file=sys.stderr)
    print(f"Specify --without-prebuilds=sdk[:{{build|host}}] to build dependencies from source code.", file=sys.stderr)


def print_sdk_unknown_error(e: Exception):
    print(f"Unable to prepare SDK: {e}", file=sys.stderr)


def parse_prefix(raw_prefix: str) -> Path:
    prefix = Path(raw_prefix)
    if not prefix.is_absolute():
        prefix = Path(os.getcwd()) / prefix
    return prefix


def query_supported_bundle_types(include_wildcards: bool) -> List[str]:
    for e in deps.Bundle:
        identifier = e.name.lower()
        if e == deps.Bundle.SDK:
            if include_wildcards:
                yield identifier
            yield identifier + ":build"
            yield identifier + ":host"
        else:
            yield identifier


def query_supported_bundle_type_values() -> List[deps.Bundle]:
    return [e for e in deps.Bundle]


def parse_bundle_type_set(raw_array: str) -> List[str]:
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


def register_meson_options(meson_option_file: Path, group: argparse._ArgumentGroup):
    interpreter = mesonbuild.optinterpreter.OptionInterpreter(subproject="")
    interpreter.process(meson_option_file)

    for key, opt in interpreter.options.items():
        name = key.name
        pretty_name = name.replace("_", "-")

        if isinstance(opt, UserFeatureOption):
            if opt.value != "enabled":
                action = "enable"
                value_to_set = "enabled"
            else:
                action = "disable"
                value_to_set = "disabled"
            group.add_argument(f"--{action}-{pretty_name}",
                               action="append_const",
                               const=f"-D{name}={value_to_set}",
                               dest="main_meson_options",
                               **parse_option_meta(name, action, opt))
            if opt.value == "auto":
                group.add_argument(f"--disable-{pretty_name}",
                                   action="append_const",
                                   const=f"-D{name}=disabled",
                                   dest="main_meson_options",
                                   **parse_option_meta(name, "disable", opt))
        elif isinstance(opt, UserBooleanOption):
            if not opt.value:
                action = "enable"
                value_to_set = "true"
            else:
                action = "disable"
                value_to_set = "false"
            group.add_argument(f"--{action}-{pretty_name}",
                               action="append_const",
                               const=f"-D{name}={value_to_set}",
                               dest="main_meson_options",
                               **parse_option_meta(name, action, opt))
        elif isinstance(opt, UserComboOption):
            group.add_argument(f"--with-{pretty_name}",
                               choices=opt.choices,
                               dest="meson_option:" + name,
                               **parse_option_meta(name, "with", opt))
        elif isinstance(opt, UserArrayOption):
            group.add_argument(f"--with-{pretty_name}",
                               dest="meson_option:" + name,
                               type=make_array_option_value_parser(opt),
                               **parse_option_meta(name, "with", opt))
        else:
            group.add_argument(f"--with-{pretty_name}",
                               dest="meson_option:" + name,
                               **parse_option_meta(name, "with", opt))


def parse_option_meta(name: str,
                      action: str,
                      opt: UserOption[Any]):
    params = {}

    if isinstance(opt, UserStringOption):
        default_value = repr(opt.value)
        metavar = name.upper()
    elif isinstance(opt, UserArrayOption):
        default_value = ",".join(opt.value)
        metavar = "{" + ",".join(opt.choices) + "}"
    elif isinstance(opt, UserComboOption):
        default_value = opt.value
        metavar = "{" + "|".join(opt.choices) + "}"
    else:
        default_value = str(opt.value).lower()
        metavar = name.upper()

    if not (isinstance(opt, UserFeatureOption) \
            and opt.value == "auto" \
            and action == "disable"):
        text = f"{help_text_from_meson(opt.description)} (default: {default_value})"
        if action == "disable":
            text = "do not " + text
        params["help"] = text
    params["metavar"] = metavar

    return params


def help_text_from_meson(description: str) -> str:
    if description:
        return description[0].lower() + description[1:]
    return description


def collect_meson_options(options: argparse.Namespace) -> List[str]:
    result = []

    if not options.enable_symbols:
        machine = options.host
        if machine is None:
            machine = MachineSpec.make_from_local_system()
        if machine.toolchain_can_strip:
            result += ["-Dstrip=true"]

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


def make_array_option_value_parser(opt: UserOption[Any]) -> Callable[[str], List[str]]:
    return lambda v: parse_array_option_value(v, opt)


def parse_array_option_value(v: str, opt: UserArrayOption) -> List[str]:
    vals = [v.strip() for v in v.split(",")]

    choices = opt.choices
    for v in vals:
        if v not in choices:
            pretty_choices = "', '".join(choices)
            raise argparse.ArgumentTypeError(f"invalid array value: '{v}' (choose from '{pretty_choices}')")

    return vals


def project_depends_on_vala_compiler(sourcedir: Path) -> bool:
    return "'vala'" in (sourcedir / "meson.build").read_text(encoding="utf-8")


def build_vala_compiler(toolchain_prefix: Path, deps_dir: Path, call_selected_meson: Callable):
    print("Building Vala compiler...", flush=True)

    workdir = deps_dir / "src"
    workdir.mkdir(parents=True, exist_ok=True)

    run_kwargs = {
        "check": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "encoding": "utf-8",
    }

    vala_checkout = workdir / "vala"
    if vala_checkout.exists():
        shutil.rmtree(vala_checkout)
    subprocess.run(["git", "clone", "--depth", "1", "https://github.com/frida/vala.git", vala_checkout.name],
                   cwd=vala_checkout.parent,
                   **run_kwargs)

    call_selected_meson([
                            "setup",
                            f"--prefix={toolchain_prefix}",
                            "-Doptimization=2",
                            "build",
                        ],
                        cwd=vala_checkout,
                        **run_kwargs)

    call_selected_meson(["install"],
                        cwd=vala_checkout / "build",
                        **run_kwargs)
