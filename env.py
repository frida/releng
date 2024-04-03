from collections import OrderedDict
from configparser import ConfigParser
import io
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Callable, Literal, Optional, Sequence, Tuple

from . import deps, env_android, env_apple, env_generic, machine_file
from .machine_file import bool_to_meson, str_to_meson, strv_to_meson
from .machine_spec import MachineSpec


DefaultLibrary = Literal["shared", "static"]


def call_meson(argv, use_submodule, *args, **kwargs):
    return subprocess.run(query_meson_entrypoint(use_submodule) + argv, *args, **kwargs)


def query_meson_entrypoint(use_submodule):
    if use_submodule:
        return [sys.executable, str(Path(__file__).parent / "meson" / "meson.py")]
    return ["meson"]


def load_meson_config(machine: MachineSpec, flavor: str, build_dir: Path):
    return machine_file.load(query_machine_file_path(machine, flavor, build_dir))


def query_machine_file_path(machine: MachineSpec, flavor: str, build_dir: Path) -> Path:
    return build_dir / f"frida{flavor}-{machine.identifier}.txt"


def enumerate_build_dirs(build_dir: Path):
    return build_dir.glob("tmp-*/*")


def detect_machine() -> MachineSpec:
    bos = detect_os()
    config = "release" if bos == "windows" else None
    return MachineSpec(bos, detect_arch(), config)


def detect_os() -> str:
    bos = platform.system().lower()
    if bos == "darwin":
        bos = "macos"
    return bos


def detect_arch() -> str:
    arch = platform.machine().lower()
    if arch == "amd64":
        arch = "x86_64"
    return arch


def detect_default_prefix() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ["ProgramFiles"]) / "Frida"
    return Path("/usr/local")


def generate_machine_files(build_machine: MachineSpec,
                           build_sdk_prefix: Optional[Path],
                           host_machine: MachineSpec,
                           host_sdk_prefix: Optional[Path],
                           toolchain_prefix: Optional[Path],
                           default_library: DefaultLibrary,
                           call_selected_meson: Callable,
                           build_dir: Path):
    is_cross_build = host_machine != build_machine

    build_config, build_machine_path, build_machine_env = \
            generate_machine_config(build_machine,
                                    build_sdk_prefix,
                                    build_machine,
                                    is_cross_build,
                                    toolchain_prefix,
                                    default_library,
                                    call_selected_meson)

    if is_cross_build:
        host_config, host_machine_path, host_machine_env = \
                generate_machine_config(host_machine,
                                        host_sdk_prefix,
                                        build_machine,
                                        is_cross_build,
                                        toolchain_prefix,
                                        default_library,
                                        call_selected_meson)
    else:
        host_config = None
        host_machine_path = []
        host_machine_env = {}

    build_dir.mkdir(parents=True, exist_ok=True)
    build_file = write_machine_file(build_machine, build_config, build_dir)
    host_file = write_machine_file(host_machine, host_config, build_dir)

    return (
        build_file,
        host_file,
        build_machine_path + host_machine_path,
        {**build_machine_env, **host_machine_env},
    )


def write_machine_file(machine: MachineSpec,
                       config: Optional[str],
                       build_dir: Path) -> Path:
    if config is None:
        return None

    f = build_dir / f"frida-{machine.identifier}.txt"
    f.write_text(config, encoding="utf-8")

    return f


def generate_machine_config(machine: MachineSpec,
                            sdk_prefix: Optional[Path],
                            build_machine: MachineSpec,
                            is_cross_build: bool,
                            toolchain_prefix: Optional[Path],
                            default_library: DefaultLibrary,
                            call_selected_meson: Callable) -> Optional[str]:
    config = ConfigParser(dict_type=OrderedDict)
    config["constants"] = OrderedDict()
    config["binaries"] = OrderedDict()
    config["built-in options"] = OrderedDict()
    config["properties"] = OrderedDict([
        ("needs_exe_wrapper", bool_to_meson(needs_exe_wrapper(machine, build_machine))),
    ])
    config["host_machine"] = OrderedDict([
        ("system", str_to_meson(machine.system)),
        ("subsystem", str_to_meson(machine.subsystem)),
        ("kernel", str_to_meson(machine.kernel)),
        ("cpu_family", str_to_meson(machine.cpu_family)),
        ("cpu", str_to_meson(machine.cpu)),
        ("endian", str_to_meson(machine.endian)),
    ])

    if machine.is_apple:
        impl = env_apple
    elif machine.os == "android":
        impl = env_android
    else:
        impl = env_generic

    machine_path, machine_env = impl.init_machine_config(machine,
                                                         sdk_prefix,
                                                         build_machine,
                                                         is_cross_build,
                                                         call_selected_meson,
                                                         config)

    if toolchain_prefix is not None:
        binaries = config["binaries"]

        toolchain_bindir = toolchain_prefix / "bin"
        exe_suffix = build_machine.executable_suffix

        for (tool_name, filename_suffix) in {("gdbus-codegen", ""),
                                             ("gio-querymodules", exe_suffix),
                                             ("glib-compile-resources", exe_suffix),
                                             ("glib-compile-schemas", exe_suffix),
                                             ("glib-genmarshal", ""),
                                             ("glib-mkenums", "")}:
            tool_path = toolchain_bindir / (tool_name + filename_suffix)
            if not tool_path.exists():
                tool_path = shutil.which(tool_name)
            if tool_path is not None:
                binaries[tool_name] = strv_to_meson([str(tool_path)])

        pkg_config_binary = toolchain_bindir / f"pkg-config{exe_suffix}"
        if not pkg_config_binary.exists():
            pkg_config_binary = shutil.which("pkg-config")
        if pkg_config_binary is not None:
            pkg_config = [
                str(pkg_config_binary),
            ]
            if default_library == "static":
                pkg_config += ["--static"]
            if sdk_prefix is not None:
                pkg_config += [f"--define-variable=frida_sdk_prefix={sdk_prefix}"]
            binaries["pkg-config"] = strv_to_meson(pkg_config)

        vala_compiler = detect_toolchain_vala_compiler(toolchain_prefix, build_machine)
        if vala_compiler is not None:
            valac, vapidir = vala_compiler
            binaries["vala"] = strv_to_meson([
                str(valac),
                f"--vapidir={vapidir}",
            ])

        if sdk_prefix is not None:
            config["built-in options"]["vala_args"] = strv_to_meson([
                "--vapidir=" + str(sdk_prefix / "share" / "vala" / "vapi")
            ])

    if sdk_prefix is not None:
        pkg_config_path = [str(sdk_prefix / machine.libdatadir / "pkgconfig")]
        config["built-in options"]["pkg_config_path"] = strv_to_meson(pkg_config_path)

    sink = io.StringIO()
    config.write(sink)

    return (sink.getvalue(), machine_path, machine_env)


def needs_exe_wrapper(machine: MachineSpec,
                      build_machine: MachineSpec) -> bool:
    if os.environ.get("FRIDA_CAN_RUN_HOST_BINARIES", "no") == "yes":
        return False
    return machine != build_machine


def detect_toolchain_vala_compiler(toolchain_prefix: Path,
                                   build_machine: MachineSpec) -> Optional[Tuple[Path, Path]]:
    datadir = next((toolchain_prefix / "share").glob("vala-*"), None)
    if datadir is None:
        return None

    api_version = datadir.name.split("-", maxsplit=1)[1]

    valac = toolchain_prefix / "bin" / f"valac-{api_version}{build_machine.executable_suffix}"
    vapidir = datadir / "vapi"
    return (valac, vapidir)
