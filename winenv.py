import glob
import json
from operator import attrgetter
import os
from pathlib import Path
import platform
import subprocess
if platform.system() == "Windows":
    import winreg


RELENG_DIR = Path(__file__).resolve().parent
ROOT_DIR = RELENG_DIR.parent
DEFAULT_TOOLCHAIN_DIR = ROOT_DIR / "build" / "toolchain-windows"
BOOTSTRAP_TOOLCHAIN_DIR = ROOT_DIR / "build" / "fts-toolchain-windows"

cached_msvs_dir = None
cached_msvc_dir = None
cached_winsdk = None


def detect_msvs_installation_dir():
    global cached_msvs_dir
    if cached_msvs_dir is None:
        vswhere = Path(os.environ.get("ProgramFiles(x86)", os.environ["ProgramFiles"])) \
                / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if not vswhere.exists():
            if DEFAULT_TOOLCHAIN_DIR.exists():
                toolchain_dir = DEFAULT_TOOLCHAIN_DIR
            else:
                toolchain_dir = BOOTSTRAP_TOOLCHAIN_DIR
            vswhere = toolchain_dir / "bin" / "vswhere.exe"
        installations = json.loads(
            subprocess.run([
                               vswhere,
                               "-latest",
                               "-format", "json",
                               "-property", "installationPath"
                           ],
                           capture_output=True,
                           encoding="utf-8",
                           check=True).stdout
        )
        if len(installations) == 0:
            raise MissingDependencyError("Visual Studio is not installed")
        cached_msvs_dir = Path(installations[0]["installationPath"])
    return cached_msvs_dir


def detect_msvc_tool_dir():
    global cached_msvc_dir
    if cached_msvc_dir is None:
        msvs_dir = detect_msvs_installation_dir()
        version = sorted((msvs_dir / "VC" / "Tools" / "MSVC").glob("*.*.*"),
                         key=attrgetter("name"),
                         reverse=True)[0].name
        cached_msvc_dir = msvs_dir / "VC" / "Tools" / "MSVC" / version
    return cached_msvc_dir


def detect_windows_sdk():
    global cached_winsdk
    if cached_winsdk is None:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows Kits\Installed Roots")
            try:
                (install_dir, _) = winreg.QueryValueEx(key, "KitsRoot10")
                install_dir = Path(install_dir)
                version = sorted((install_dir / "Include").glob("*.*.*"),
                                 key=attrgetter("name"),
                                 reverse=True)[0].name
                cached_winsdk = (install_dir, version)
            finally:
                winreg.CloseKey(key)
        except Exception as e:
            raise MissingDependencyError("Windows 10 SDK is not installed")
    return cached_winsdk


def detect_msvs_tool_path(machine, tool):
    if machine.arch == "x86_64":
        return Path(detect_msvc_tool_dir()) / "bin" / "HostX86" / "x64" / tool
    else:
        return Path(detect_msvc_tool_dir()) / "bin" / "HostX86" / "x86" / tool


def detect_msvs_runtime_path(machine, build_machine):
    msvc_platform = msvc_platform_from_arch(machine.arch)
    native_msvc_platform = msvc_platform_from_arch(build_machine.arch)

    msvc_dir = detect_msvc_tool_dir()
    msvc_bindir = msvc_dir / "bin" / ("Host" + native_msvc_platform) / msvc_platform

    msvc_dll_dirs = []
    if msvc_platform != native_msvc_platform:
        msvc_dll_dirs.append(msvc_dir / "bin" / ("Host" + native_msvc_platform) / native_msvc_platform)

    (winsdk_dir, winsdk_version) = detect_windows_sdk()
    winsdk_bindir = winsdk_dir / "Bin" / winsdk_version / msvc_platform

    return [winsdk_bindir, msvc_bindir] + msvc_dll_dirs


def detect_msvs_include_path():
    msvc_dir = detect_msvc_tool_dir()
    vc_dir = detect_msvs_installation_dir() / "VC"

    (winsdk_dir, winsdk_version) = detect_windows_sdk()
    winsdk_inc_dirs = [
        winsdk_dir / "Include" / winsdk_version / "um",
        winsdk_dir / "Include" / winsdk_version / "shared",
    ]

    return [
        msvc_dir / "include",
        msvc_dir / "atlmfc" / "include",
        vc_dir / "Auxiliary" / "VS" / "include",
        winsdk_dir / "Include" / winsdk_version / "ucrt",
    ] + winsdk_inc_dirs


def detect_msvs_library_path(machine):
    msvc_platform = msvc_platform_from_arch(machine.arch)

    msvc_dir = detect_msvc_tool_dir()
    vc_dir = detect_msvs_installation_dir() / "VC"

    (winsdk_dir, winsdk_version) = detect_windows_sdk()
    winsdk_lib_dir = winsdk_dir / "Lib" / winsdk_version / "um" / msvc_platform

    return [
        msvc_dir / "lib" / msvc_platform,
        msvc_dir / "atlmfc" / "lib" / msvc_platform,
        vc_dir / "Auxiliary" / "VS" / "lib" / msvc_platform,
        winsdk_dir / "Lib" / winsdk_version / "ucrt" / msvc_platform,
        winsdk_lib_dir,
    ]


def msvs_platform_from_arch(arch: str) -> str:
    return "x64" if arch == "x86_64" else "Win32"


def msvc_platform_from_arch(arch: str) -> str:
    return "x64" if arch == "x86_64" else "x86"


class MissingDependencyError(Exception):
    pass
