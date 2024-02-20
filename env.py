from configparser import ConfigParser
import io
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Callable, Literal, Optional, Sequence

from . import deps, env_android, env_apple, env_generic, machine_file
from .machine_file import str_to_meson, strv_to_meson
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


def query_devkit_output_dir(name: str, build_dir: Path) -> Path:
    return build_dir / "devkits" / name


def detect_native_machine() -> MachineSpec:
    nos = detect_native_os()
    config = "release" if nos == "windows" else None
    return MachineSpec(nos, detect_native_arch(), config)


def detect_native_os() -> str:
    nos = platform.system().lower()
    if nos == "darwin":
        nos = "macos"
    return nos


def detect_native_arch() -> str:
    arch = platform.machine().lower()
    if arch == "amd64":
        arch = "x86_64"
    return arch


def detect_native_default_prefix() -> Path:
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
                            native_machine: MachineSpec,
                            is_cross_build: bool,
                            toolchain_prefix: Optional[Path],
                            default_library: DefaultLibrary,
                            call_selected_meson: Callable) -> Optional[str]:
    config = ConfigParser()

    config["host_machine"] = {
        "system": str_to_meson(machine.system),
        "subsystem": str_to_meson(machine.subsystem),
        "kernel": str_to_meson(machine.kernel),
        "cpu_family": str_to_meson(machine.cpu_family),
        "cpu": str_to_meson(machine.cpu),
        "endian": str_to_meson(machine.endian),
    }

    if machine.is_apple:
        impl = env_apple
    elif machine.os == "android":
        impl = env_android
    else:
        impl = env_generic

    machine_path, machine_env = impl.init_machine_config(machine,
                                                         sdk_prefix,
                                                         native_machine,
                                                         is_cross_build,
                                                         call_selected_meson,
                                                         config)

    if toolchain_prefix is not None:
        binaries = config["binaries"]

        toolchain_bindir = toolchain_prefix / "bin"
        exe_suffix = native_machine.executable_suffix

        for (tool_name, filename_suffix) in {("gdbus-codegen", ""),
                                             ("gio-querymodules", exe_suffix),
                                             ("glib-compile-resources", exe_suffix),
                                             ("glib-compile-schemas", exe_suffix),
                                             ("glib-genmarshal", ""),
                                             ("glib-mkenums", "")}:
            tool_path = toolchain_bindir / (tool_name + filename_suffix)
            if tool_path.exists():
                binaries[tool_name] = strv_to_meson([str(tool_path)])

        pkg_config = [
            str(toolchain_bindir / f"pkg-config{exe_suffix}"),
        ]
        if default_library == "static":
            pkg_config += ["--static"]
        if sdk_prefix is not None:
            pkg_config += [f"--define-variable=frida_sdk_prefix={sdk_prefix}"]
        binaries["pkg-config"] = strv_to_meson(pkg_config)

        valac_datadir = next((toolchain_prefix / "share").glob("vala-*"))
        vala_api_version = valac_datadir.name.split("-", maxsplit=1)[1]

        vapi_dirs = []
        if sdk_prefix is not None:
            vapi_dirs += [sdk_prefix / "share" / "vala" / "vapi"]
        vapi_dirs += [valac_datadir / "vapi"]

        valac = [
            str(toolchain_bindir / f"valac-{vala_api_version}{exe_suffix}"),
        ]
        valac += [f"--vapidir={d}" for d in vapi_dirs]
        binaries["vala"] = strv_to_meson(valac)

    if sdk_prefix is not None:
        libdatadir = "libdata" if machine.os == "freebsd" else "lib"
        pkg_config_path = [str(sdk_prefix / libdatadir / "pkgconfig")]
        config["built-in options"]["pkg_config_path"] = str_to_meson(os.pathsep.join(pkg_config_path))

    sink = io.StringIO()
    config.write(sink)

    return (sink.getvalue(), machine_path, machine_env)


def query_toolchain_prefix(machine: MachineSpec, deps_dir: Path) -> Path:
    return deps_dir / f"toolchain-{machine.identifier}"


def ensure_toolchain(machine: MachineSpec, deps_dir: Path) -> Path:
    toolchain_prefix = query_toolchain_prefix(machine, deps_dir)
    deps.sync(deps.Bundle.TOOLCHAIN, machine, toolchain_prefix)
    return toolchain_prefix


def query_sdk_prefix(machine: MachineSpec, deps_dir: Path) -> Path:
    if machine.os == "windows":
        return deps_dir / "sdk-windows" / f"{machine.msvs_platform}-{machine.config.title()}"
    return deps_dir / f"sdk-{machine.identifier}"


def ensure_sdk(machine: MachineSpec, deps_dir: Path) -> Path:
    sdk_prefix = query_sdk_prefix(machine, deps_dir)
    if machine.os == "windows":
        sdk_dir = sdk_prefix.parent
    else:
        sdk_dir = sdk_prefix
    deps.sync(deps.Bundle.SDK, machine, sdk_dir)
    return sdk_prefix