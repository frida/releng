from dataclasses import dataclass
import re
from typing import Optional


KERNELS = {
    "windows": "nt",

    "macos":   "xnu",
    "ios":     "xnu",
    "watchos": "xnu",
    "tvos":    "xnu",

    "qnx":     "nto",
}

CPU_FAMILIES = {
    "armbe8":     "arm",
    "armeabi":    "arm",
    "armhf":      "arm",

    "arm64":      "aarch64",
    "arm64e":     "aarch64",
    "arm64eoabi": "aarch64",

    "mipsel":     "mips",
    "mips64el":   "mips64",
}

CPU_TYPES = {
    "arm":        "armv7",
    "armbe8":     "armv6",
    "armhf":      "armv7hf",
    "armeabi":    "armv7eabi",

    "arm64":      "aarch64",
    "arm64e":     "aarch64",
    "arm64eoabi": "aarch64",
}

CPU_TYPES_PER_OS_OVERRIDES = {
    "linux": {
        "arm":        "armv5t",
        "armbe8":     "armv6t",
        "armhf":      "armv7a",

        "mips":       "mips1",
        "mipsel":     "mips1",

        "mips64":     "mips64r2",
        "mips64el":   "mips64r2",
    },
    "android": {
        "x86":        "i686",
    },
    "qnx": {
        "arm":        "armv6",
        "armeabi":    "armv7",
    },
}

BIG_ENDIAN_ARCHS = {
    "armbe8",
    "mips",
    "mips64",
    "s390x",
}

TARGET_TRIPLET_ARCH_PATTERN = re.compile(r"^(i.86|x86_64|arm(v\w+)?|aarch64|mips\w*|s390x)$")


@dataclass
class MachineSpec:
    os: str
    arch: str
    config: Optional[str] = None
    triplet: Optional[str] = None

    @property
    def identifier(self) -> str:
        parts = [self.os, self.arch]
        if self.config is not None:
            parts += [self.config]
        return "-".join(parts)

    @property
    def os_dash_arch(self) -> str:
        return f"{self.os}-{self.arch}"

    @property
    def os_dash_config(self) -> str:
        parts = [self.os]
        if self.config is not None:
            parts += [self.config]
        return "-".join(parts)

    @property
    def executable_suffix(self) -> str:
        return ".exe" if self.os == "windows" else ""

    @property
    def msvs_platform(self) -> str:
        return "x64" if self.arch == "x86_64" else "Win32"

    @property
    def msvc_platform(self) -> str:
        return "x64" if self.arch == "x86_64" else "x86"

    @property
    def is_apple(self) -> str:
        return self.os in {"macos", "ios", "watchos", "tvos"}

    @property
    def system(self) -> str:
        return "darwin" if self.is_apple else self.os

    @property
    def subsystem(self) -> str:
        return self.os_dash_config if self.is_apple else self.os

    @property
    def kernel(self) -> str:
        return KERNELS.get(self.os, self.os)

    @property
    def cpu_family(self) -> str:
        arch = self.arch
        return CPU_FAMILIES.get(arch, arch)

    @property
    def cpu(self) -> str:
        arch = self.arch

        mappings_to_search = [
            CPU_TYPES_PER_OS_OVERRIDES.get(self.os, {}),
            CPU_TYPES,
        ]
        for m in mappings_to_search:
            cpu = m.get(arch, None)
            if cpu is not None:
                return cpu

        return arch

    @property
    def endian(self) -> str:
        return "big" if self.arch in BIG_ENDIAN_ARCHS else "little"

    def __eq__(self, other):
        if isinstance(other, MachineSpec):
            return other.identifier == self.identifier
        return False


def parse(raw_spec):
    tokens = raw_spec.split("-")

    if len(tokens) in {3, 4}:
        arch = tokens[0]
        m = TARGET_TRIPLET_ARCH_PATTERN.match(arch)
        if m is not None:
            kernel = tokens[-2]
            system = tokens[-1]

            if kernel == "w64":
                os = "windows"
            elif kernel == "nto":
                os = "qnx"
            else:
                os = kernel

            if arch[0] == "i":
                arch = "x86"
            elif arch == "arm":
                if system == "gnueabihf":
                    arch = "armhf"
                elif os == "qnx" and system.endswith("eabi"):
                    arch = "armeabi"
            elif arch == "aarch64":
                arch = "arm64"

            config = None
            if system.startswith("musl"):
                config = "musl"
            elif kernel == "w64":
                config = "mingw64" if arch == "x86_64" else "mingw32"

            return MachineSpec(os, arch, config, raw_spec)

    os, arch, *rest = tokens
    if len(rest) != 0:
        config = rest[0].lower()
    else:
        if os == "windows":
            config = "release"
        else:
            config = None
    return MachineSpec(os, arch, config)
