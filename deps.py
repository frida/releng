#!/usr/bin/env python3
import argparse
import base64
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Optional
import urllib.request

RELENG_DIR = Path(__file__).parent.resolve()
ROOT_DIR = RELENG_DIR.parent

if __name__ == "__main__":
    # TODO: Refactor
    sys.path.insert(0, str(ROOT_DIR))

from releng import env, winenv
from releng.machine_spec import MachineSpec


BUNDLE_URL = "https://build.frida.re/deps/{version}/{filename}"

DEPS_MK_PATH = RELENG_DIR / "deps.mk"

CONFIG_KEY_VALUE_PATTERN = re.compile(r"^([a-z]\w+) = (.*?)(?<!\\)$", re.MULTILINE | re.DOTALL)
CONFIG_VARIABLE_REF_PATTERN = re.compile(r"\$\((\w+)\)")


class Bundle(Enum):
    TOOLCHAIN = 1,
    SDK = 2,


class PackageRole(Enum):
    TOOL = 1,
    LIBRARY = 2,


Package = tuple[str, PackageRole]


class SourceState(Enum):
    PRISTINE = 1,
    MODIFIED = 2,


class BundleNotFoundError(Exception):
    pass


@dataclass
class PackageSpec:
    name: str
    version: str
    url: str
    recipe: str
    patches: list[str]
    deps: list[str]
    deps_for_build: list[str]
    options: list[str]


@dataclass
class DependencyParameters:
    deps_version: str
    bootstrap_version: str
    packages: dict[str, PackageSpec]

    def get_package_spec(self, name: str) -> PackageSpec:
        return self.packages[name.replace("-", "_")]


class CommandError(Exception):
    pass


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()

    bundle_choices = [name.lower() for name in Bundle.__members__]

    command = subparsers.add_parser("sync", help="ensure prebuilt dependencies are up-to-date")
    command.add_argument("bundle", help="bundle to synchronize", choices=bundle_choices)
    command.add_argument("host", help="OS/arch")
    command.add_argument("location", help="filesystem location")
    command.set_defaults(func=lambda args: sync(Bundle[args.bundle.upper()], MachineSpec.parse(args.host), Path(args.location).resolve()))

    command = subparsers.add_parser("roll", help="build and upload prebuilt dependencies if needed")
    command.add_argument("bundle", help="bundle to roll", choices=bundle_choices)
    command.add_argument("host", help="OS/arch")
    command.add_argument("--activate", default=False, action='store_true')
    command.add_argument("--post", help="post-processing script")
    command.set_defaults(func=lambda args: roll(Bundle[args.bundle.upper()], MachineSpec.parse(args.host), args.activate,
                                                Path(args.post) if args.post is not None else None))

    command = subparsers.add_parser("build", help="build prebuilt dependencies")
    command.add_argument("bundle", help="bundle to roll", choices=bundle_choices)
    command.add_argument("--host", help="OS/arch", default=MachineSpec.make_from_local_system().identifier)
    command.set_defaults(func=lambda args: build(Bundle[args.bundle.upper()], MachineSpec.parse(args.host)))

    command = subparsers.add_parser("wait", help="wait for prebuilt dependencies if needed")
    command.add_argument("bundle", help="bundle to wait for", choices=bundle_choices)
    command.add_argument("host", help="OS/arch")
    command.set_defaults(func=lambda args: wait(Bundle[args.bundle.upper()], MachineSpec.parse(args.host)))

    command = subparsers.add_parser("bump", help="bump dependency versions")
    command.set_defaults(func=lambda args: bump())

    args = parser.parse_args()
    if 'func' in args:
        try:
            args.func(args)
        except CommandError as e:
            print(e, file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_usage(file=sys.stderr)
        sys.exit(1)


def query_toolchain_prefix(machine: MachineSpec,
                           cache_dir: Path) -> Path:
    identifier = "windows-x86" if machine.os == "windows" and machine.arch in {"x86", "x86_64"} \
            else machine.identifier
    return cache_dir / f"toolchain-{identifier}"


def ensure_toolchain(machine: MachineSpec,
                     cache_dir: Path,
                     version: Optional[str] = None) -> tuple[Path, SourceState]:
    toolchain_prefix = query_toolchain_prefix(machine, cache_dir)
    state = sync(Bundle.TOOLCHAIN, machine, toolchain_prefix, version)
    return (toolchain_prefix, state)


def query_sdk_prefix(machine: MachineSpec,
                     cache_dir: Path) -> Path:
    return cache_dir / f"sdk-{machine.identifier}"


def ensure_sdk(machine: MachineSpec,
               cache_dir: Path,
               version: Optional[str] = None) -> tuple[Path, SourceState]:
    sdk_prefix = query_sdk_prefix(machine, cache_dir)
    state = sync(Bundle.SDK, machine, sdk_prefix, version)
    return (sdk_prefix, state)


def detect_cache_dir(sourcedir: Path) -> Path:
    raw_location = os.environ.get("FRIDA_DEPS", None)
    if raw_location is not None:
        location = Path(raw_location)
    else:
        location = sourcedir / "deps"
    return location


def sync(bundle: Bundle,
         machine: MachineSpec,
         location: Path,
         version: Optional[str] = None) -> SourceState:
    state = SourceState.PRISTINE

    if version is None:
        version = read_dependency_parameters().deps_version

    bundle_nick = bundle.name.lower() if bundle != Bundle.SDK else bundle.name

    if bundle == Bundle.SDK:
        if machine.os == "windows":
            msvs_platform = winenv.msvs_platform_from_arch(machine.arch)
            subdir_name = f"{msvs_platform}-{machine.config.title()}"
            location = location / subdir_name
        else:
            subdir_name = machine.identifier

    if location.exists():
        try:
            cached_version = (location / "VERSION.txt").read_text(encoding='utf-8').strip()
            if cached_version == version:
                return state
        except:
            pass
        shutil.rmtree(location)
        state = SourceState.MODIFIED

    (url, filename, suffix) = compute_bundle_parameters(bundle, machine, version)

    local_bundle = location.parent / filename
    if local_bundle.exists():
        print("Deploying local {}...".format(bundle_nick), flush=True)
        archive_path = local_bundle
        archive_is_temporary = False
    else:
        if bundle == Bundle.SDK:
            print(f"Downloading SDK {version} for {subdir_name}...", flush=True)
        else:
            print(f"Downloading {bundle_nick} {version}...", flush=True)
        try:
            with urllib.request.urlopen(url) as response, \
                    tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as archive:
                shutil.copyfileobj(response, archive)
                archive_path = Path(archive.name)
                archive_is_temporary = True
            print(f"Extracting {bundle_nick}...", flush=True)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise BundleNotFoundError(f"missing bundle at {url}") from e
            raise e

    try:
        staging_dir = location.parent / f"_{location.name}"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)

        if machine.os == "windows":
            subprocess.run([
                archive_path,
                "-o" + str(staging_dir),
                "-y"
            ], capture_output=True, check=True)
        else:
            with tarfile.open(archive_path, "r:bz2") as tar:
                tar.extractall(staging_dir)

        root_items = list(staging_dir.iterdir())
        temp_item = None
        if len(root_items) == 1:
            item = root_items[0]
            root_items = list(item.iterdir())
            temp_item = item

        if machine.os == "windows" and bundle == Bundle.SDK:
            assert len(root_items) == 2
            version_txt = next(item for item in root_items if item.name == "VERSION.txt")
            content_dir = next(item for item in root_items if item != version_txt)

            shutil.move(version_txt, staging_dir / version_txt.name)
            for item in content_dir.iterdir():
                shutil.move(item, staging_dir / item.name)
        elif temp_item is not None:
            for item in root_items:
                item.rename(staging_dir / item.name)

        if temp_item is not None:
            shutil.rmtree(temp_item)

        if bundle == Bundle.TOOLCHAIN:
            suffix_len = len(".frida.in")
            raw_location = location.as_posix()
            for f in staging_dir.rglob("*.frida.in"):
                target = f.parent / f.name[:-suffix_len]
                f.write_text(f.read_text(encoding="utf-8").replace("@FRIDA_TOOLROOT@", raw_location),
                             encoding="utf-8")
                f.rename(target)

        staging_dir.rename(location)
    finally:
        if archive_is_temporary:
            archive_path.unlink()

    return state


def roll(bundle: Bundle, machine: MachineSpec, activate: bool, post: Optional[Path]):
    params = read_dependency_parameters()
    version = params.deps_version

    if activate and bundle == Bundle.SDK:
        configure_bootstrap_version(version)

    (public_url, filename, suffix) = compute_bundle_parameters(bundle, machine, version)

    # First do a quick check to avoid hitting S3 in most cases.
    request = urllib.request.Request(public_url)
    request.get_method = lambda: "HEAD"
    try:
        with urllib.request.urlopen(request) as r:
            return
    except urllib.request.HTTPError as e:
        if e.code != 404:
            raise CommandError("network error") from e

    s3_url = "s3://build.frida.re/deps/{version}/{filename}".format(version=version, filename=filename)

    # We will most likely need to build, but let's check S3 to be certain.
    r = subprocess.run(["aws", "s3", "ls", s3_url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8')
    if r.returncode == 0:
        return
    if r.returncode != 1:
        raise CommandError(f"unable to access S3: {r.stdout.strip()}")

    artifact = ROOT_DIR / "build" / filename
    if artifact.exists():
        artifact.unlink()

    if machine.os == "windows":
        subprocess.run([
                           sys.executable, RELENG_DIR / "build-deps-windows.py",
                           "--bundle=" + bundle.name.lower(),
                           "--host=" + machine.identifier,
                       ],
                       check=True)
    else:
        if platform.system().endswith("BSD"):
            gnu_make = "gmake"
        else:
            gnu_make = "make"
        subprocess.run([
                           gnu_make,
                           "-C", ROOT_DIR,
                           "-f", "Makefile.{}.mk".format(bundle.name.lower()),
                           "FRIDA_HOST=" + machine.identifier,
                       ],
                       check=True)

    if post is not None:
        post_script = RELENG_DIR / post
        if not post_script.exists():
            raise CommandError("post-processing script not found")

        subprocess.run([
                           sys.executable, post_script,
                           "--bundle=" + bundle.name.lower(),
                           "--host=" + machine.identifier,
                           "--artifact=" + str(artifact),
                           "--version=" + version,
                       ],
                       check=True)

    subprocess.run(["aws", "s3", "cp", artifact, s3_url], check=True)

    # Use the shell for Windows compatibility, where npm generates a .bat script.
    subprocess.run("cfcli purge " + public_url, shell=True, check=True)

    if activate and bundle == Bundle.TOOLCHAIN:
        configure_bootstrap_version(version)


def build(bundle: Bundle, machine: MachineSpec):
    packages: list[Package] = [
        ("zlib", PackageRole.LIBRARY),
    ]

    builder = DependencyBuilder(bundle, machine)
    try:
        builder.build(packages)
    except subprocess.CalledProcessError as e:
        print(e, file=sys.stderr)
        if e.stdout is not None:
            print("\n=== stdout ===\n" + e.stdout, file=sys.stderr)
        if e.stderr is not None:
            print("\n=== stderr ===\n" + e.stderr, file=sys.stderr)
        sys.exit(1)


class DependencyBuilder:
    def __init__(self, bundle: Bundle, host_machine: MachineSpec):
        self._bundle = bundle
        self._build_machine = MachineSpec.make_from_local_system()
        self._host_machine = host_machine
        self._default_library = "static"

        self._params = read_dependency_parameters({})
        self._cachedir = detect_cache_dir(ROOT_DIR)
        self._workdir = self._cachedir / "src"

        self._toolchain_prefix: Optional[Path] = None
        self._native_file: Optional[Path] = None
        self._cross_file: Optional[Path] = None
        self._machine_env: dict[str, str] = {}

    def build(self, packages: list[Package]):
        started_at = time.time()
        prepare_ended_at = None
        build_ended_at = None
        packaging_ended_at = None
        try:
            self._prepare(packages)
            prepare_ended_at = time.time()

            for name, role in packages:
                self._build_package(name, role, self._params.get_package_spec(name))
            build_ended_at = time.time()

            self._package()
            packaging_ended_at = time.time()
        finally:
            ended_at = time.time()

            if prepare_ended_at is not None:
                print("")
                print("*** TIME SPENT")
                print("")
                print("      Total: {}".format(format_duration(ended_at - started_at)))

            if prepare_ended_at is not None:
                print("    Prepare: {}".format(format_duration(prepare_ended_at - started_at)))

            if build_ended_at is not None:
                print("      Build: {}".format(format_duration(build_ended_at - prepare_ended_at)))

            if packaging_ended_at is not None:
                print("  Packaging: {}".format(format_duration(packaging_ended_at - build_ended_at)))

    def _prepare(self, packages: list[Package]):
        self._toolchain_prefix, toolchain_state = ensure_toolchain(self._build_machine, self._cachedir)
        if toolchain_state == SourceState.MODIFIED:
            self._wipe_build_state()

        (self._native_file, self._cross_file, machine_paths, machine_env) = \
                env.generate_machine_files(build_machine=self._build_machine,
                                           build_sdk_prefix=None,
                                           host_machine=self._host_machine,
                                           host_sdk_prefix=None,
                                           toolchain_prefix=self._toolchain_prefix,
                                           default_library=self._default_library,
                                           call_selected_meson=self._call_meson,
                                           outdir=self._get_outdir())
        menv = {**os.environ, **machine_env}
        menv["PATH"] = os.pathsep.join(machine_paths) + os.pathsep + menv["PATH"]
        self._machine_env = menv

        #self._check_build_environment()

        for name, _ in packages:
            pkg_state = self._grab_and_prepare(name, self._params.get_package_spec(name))
            if pkg_state == SourceState.MODIFIED:
                self._wipe_build_state()

    def _grab_and_prepare(self, name: str, spec: PackageSpec) -> SourceState:
        assert spec.recipe == "meson" or name == "ninja"

        sourcedir = self._get_sourcedir(name)
        if sourcedir.exists():
            if query_git_head(sourcedir) == spec.version:
                source_state = SourceState.PRISTINE
            else:
                print()
                print("{name}: synchronizing".format(name=name), flush=True)
                perform("git", "fetch", "-q",
                        cwd=sourcedir)
                perform("git", "checkout", "-q", spec.version,
                        cwd=sourcedir)
                source_state = SourceState.MODIFIED
        else:
            print()
            print(f"{name}: cloning", flush=True)
            sourcedir.parent.mkdir(parents=True, exist_ok=True)
            perform("git", "clone", "-q", "--recurse-submodules", spec.url, sourcedir.name,
                    cwd=sourcedir.parent)
            perform("git", "checkout", "-q", spec.version,
                    cwd=sourcedir)
            for name in spec.patches:
                # FIXME: If this fails and the build is restarted, we will end up skipping this part.
                perform("git", "apply", RELENG_DIR / "patches" / name,
                        cwd=sourcedir)
            source_state = SourceState.PRISTINE

        return source_state

    def _wipe_build_state(self):
        print("TODO: Wipe build state")
        #print("*** Wiping build state", flush=True)
        #locations = [
        #    ("existing packages", get_prefix_root()),
        #    ("build directories", get_tmp_root()),
        #]
        #for description, path in locations:
        #    if path.exists():
        #        print("Wiping", description, flush=True)
        #        shutil.rmtree(path)

    def _build_package(self, name: str, role: PackageRole, spec: PackageSpec):
        runtimes = ["static"]
        if self._host_machine.os == "windows" and role is PackageRole.LIBRARY:
            runtimes += ["dynamic"]

        for runtime in runtimes:
            manifest_path = self._get_manifest_path(name, runtime)
            if manifest_path.exists():
                continue

            print()
            print(f"*** Building {spec.name} for runtime={runtime} spec={spec}", flush=True)

            if name == "ninja":
                self._build_ninja(name, runtime)
            else:
                assert spec.recipe == "meson"
                self._build_using_meson(name, runtime, spec)

            assert manifest_path.exists()

    def _build_ninja(self, name: str, runtime: str):
        env_dir, shell_env = get_meson_params(arch, config, runtime)

        shell_env = shell_env.copy()
        del shell_env["CL"] # Remove unicode defines

        source_dir = DEPS_DIR / name
        build_dir = env_dir / name
        prefix = get_prefix_path(arch, config, runtime)
        bin_dir = prefix / "bin"

        if build_dir.exists():
            perform("git", "worktree", "remove", "-f", build_dir,
                    cwd=source_dir)

        perform("git", "worktree", "add", "-f", build_dir,
                cwd=source_dir)

        configure_file = build_dir / "configure.py"
        configure_code = configure_file.read_text(encoding="utf-8")
        configure_code = configure_code.replace("-O2", "/O1")
        configure_file.write_text(configure_code, encoding="utf-8")

        perform(sys.executable, configure_file, "--bootstrap",
                cwd=build_dir,
                env=shell_env)

        bin_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(build_dir / "ninja.exe", bin_dir)

        manifest_path = self._get_manifest_path(name, runtime)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("bin/ninja.exe\n", encoding="utf-8")

    def _build_using_meson(self, name: str, runtime: str, spec: PackageSpec):
        sourcedir = self._get_sourcedir(name)
        builddir = self._get_builddir(name, runtime)

        prefix = self._get_prefix(runtime)
        if self._host_machine.config != "debug":
            optimization = "s"
            ndebug = "true"
        else:
            optimization = "0"
            ndebug = "false"

        if builddir.exists():
            shutil.rmtree(builddir)

        self._call_meson([
                             "setup",
                             builddir,
                             f"--prefix={prefix}",
                             f"--default-library={self._default_library}",
                             f"--backend=ninja",
                             f"-Doptimization={optimization}",
                             f"-Db_ndebug={ndebug}",
                             f"-Db_vscrt={vscrt_from_configuration_and_runtime(self._host_machine.config, runtime)}",
                             *spec.options,
                         ],
                         cwd=sourcedir,
                         env=self._machine_env)

        self._call_meson(["install"],
                         cwd=builddir,
                         env=self._machine_env)

        manifest_lines = []
        install_locations = json.loads(self._call_meson(["introspect", "--installed"],
                                                        cwd=builddir,
                                                        capture_output=True,
                                                        encoding="utf-8",
                                                        env=self._machine_env).stdout)
        for installed_path in install_locations.values():
            manifest_lines.append(Path(installed_path).relative_to(prefix).as_posix())
        manifest_lines.sort()
        manifest_path = self._get_manifest_path(name, runtime)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    def _call_meson(self, argv, *args, **kwargs):
        return env.call_meson(argv, use_submodule=True, *args, **kwargs)

    def _package(self):
        with tempfile.TemporaryDirectory(prefix="frida-deps") as raw_tempdir:
            tempdir = Path(raw_tempdir)

            outfile = self._cachedir / f"{self._bundle.name.lower()}-{self._host_machine.identifier}.tar.bz2"
            print("outfile:", str(outfile))

            if self._bundle is Bundle.TOOLCHAIN:
                self._stage_toolchain_files(tempdir)
            else:
                self._stage_sdk_files(tempdir)


            toolchain_path = ROOT_DIR / "build" / "toolchain-windows-x86.exe"

            sdk_paths = {}
            for arch in host_selector.architectures:
                for config in host_selector.configurations:
                    sdk_paths[(arch, config)] = ROOT_DIR / "build" / f"sdk-windows-{arch}-{config}.exe"

            print("About to assemble:")
            if Bundle.TOOLCHAIN in bundle_ids:
                print("\t* " + toolchain_path.name)
            if Bundle.SDK in bundle_ids:
                for sdk_path in sorted(sdk_paths.values()):
                    print("\t* " + sdk_path.name)

            print()
            print("Determining what to include...", flush=True)

            prefixes_dir = get_prefix_root()

            toolchain_files = []
            toolchain_mixin_files = []
            if Bundle.TOOLCHAIN in bundle_ids:
                for root, dirs, files in os.walk(get_prefix_path("x86", "release", "static")):
                    relpath = PurePath(root).relative_to(prefixes_dir)
                    all_files = [relpath / f for f in files]
                    toolchain_files += [f for f in all_files if file_is_vala_toolchain_related(f) or \
                            f.name in {"ninja.exe", "pkg-config.exe", "glib-genmarshal", "glib-mkenums"} or \
                            f.parent.name == "manifest"]
                toolchain_files.sort()

                for root, dirs, files in os.walk(BOOTSTRAP_TOOLCHAIN_DIR):
                    relpath = PurePath(root).relative_to(BOOTSTRAP_TOOLCHAIN_DIR)
                    all_files = [relpath / f for f in files]
                    toolchain_mixin_files += [f for f in all_files if not (file_is_vala_toolchain_related(f) or \
                            f.parent.name == "manifest")]
                toolchain_mixin_files.sort()

            sdk_files = {}
            if Bundle.SDK in bundle_ids:
                for arch in host_selector.architectures:
                    for config in host_selector.configurations:
                        cur_files = []
                        sdk_files[(arch, config)] = cur_files
                        prefix_pattern = "-".join([arch, config, "static"])
                        for prefix in prefixes_dir.glob(prefix_pattern):
                            for root, dirs, files in os.walk(prefix):
                                relpath = PurePath(root).relative_to(prefixes_dir)
                                all_files = [relpath / f for f in files]
                                cur_files += [f for f in all_files if file_is_sdk_related(f)]
                            cur_files += [f.relative_to(prefixes_dir) for f in \
                                    (prefix.parent / (prefix.name[:-7] + "-dynamic") / "lib").glob("**/*.a")]
                        cur_files.sort()

            print("Copying files...", flush=True)
            if Bundle.TOOLCHAIN in bundle_ids:
                toolchain_tempdir = tempdir / toolchain_path.stem
                copy_files(BOOTSTRAP_TOOLCHAIN_DIR, toolchain_mixin_files, toolchain_tempdir)
                copy_files(prefixes_dir, toolchain_files, toolchain_tempdir, transform_toolchain_dest)
                fix_manifests(toolchain_tempdir)
                (toolchain_tempdir / "VERSION.txt").write_text(params.deps_version + "\n", encoding="utf-8")

            if Bundle.SDK in bundle_ids:
                for (arch, config), sdk_path in sdk_paths.items():
                    sdk_tempdir = tempdir / sdk_path.stem
                    copy_files(prefixes_dir, sdk_files[(arch, config)], sdk_tempdir, transform_sdk_dest)
                    fix_manifests(sdk_tempdir)
                    (sdk_tempdir / "VERSION.txt").write_text(params.deps_version + "\n", encoding="utf-8")

            print("Compressing...", flush=True)
            compression_switches = ["a", "-mx{}".format(COMPRESSION_LEVEL), "-sfx7zCon.sfx"]

            if Bundle.TOOLCHAIN in bundle_ids:
                toolchain_path.unlink(missing_ok=True)
                perform("7z", *compression_switches, "-r", toolchain_path, ".", cwd=toolchain_tempdir)

            if Bundle.SDK in bundle_ids:
                for (arch, config), sdk_path in sdk_paths.items():
                    sdk_path.unlink(missing_ok=True)
                    perform("7z", *compression_switches, "-r", sdk_path, ".", cwd=tempdir / sdk_path.stem)

            print("All done.", flush=True)

    def _get_outdir(self) -> Path:
        return self._workdir / "_out"

    def _get_sourcedir(self, name: str) -> Path:
        return self._workdir / name

    def _get_builddir(self, name: str, runtime: str) -> Path:
        return self._workdir / "_build" / self._compute_output_id(runtime) / name

    def _get_prefix(self, runtime: str) -> Path:
        return self._get_outdir() / self._compute_output_id(runtime)

    def _compute_output_id(self, runtime: str) -> str:
        parts = [self._host_machine.identifier]
        if self._host_machine.os == "windows":
            parts += [runtime]
        return "-".join(parts)

    def _get_manifest_path(self, name: str, runtime: str) -> Path:
        return self._get_prefix(runtime) / "manifest" / f"{name}.pkg"


def vscrt_from_configuration_and_runtime(config: str, runtime: str) -> str:
    result = "md" if runtime == "dynamic" else "mt"
    if config == "debug":
        result += "d"
    return result


def wait(bundle: Bundle, machine: MachineSpec):
    params = read_dependency_parameters()
    (url, filename, suffix) = compute_bundle_parameters(bundle, machine, params.deps_version)

    request = urllib.request.Request(url)
    request.get_method = lambda: "HEAD"
    started_at = time.time()
    while True:
        try:
            with urllib.request.urlopen(request) as r:
                return
        except urllib.request.HTTPError as e:
            if e.code != 404:
                return
        print("Waiting for: {}  Elapsed: {}  Retrying in 5 minutes...".format(url, int(time.time() - started_at)), flush=True)
        time.sleep(5 * 60)


def bump():
    params = read_dependency_parameters()

    auth_blob = base64.b64encode(":".join([
                                              os.environ["GH_USERNAME"],
                                              os.environ["GH_TOKEN"]
                                          ]).encode('utf-8')).decode('utf-8')
    auth_header = "Basic " + auth_blob

    for identifier, pkg in params.packages.items():
        url = pkg.url
        if not url.startswith("https://github.com/frida/"):
            continue

        print(f"*** Checking {pkg.name}")

        repo_name = url.split("/")[-1][:-4]
        branch_name = "next" if repo_name == "capstone" else "main"

        url = f"https://api.github.com/repos/frida/{repo_name}/commits/main"
        request = urllib.request.Request(url)
        request.add_header("Authorization", auth_header)
        with urllib.request.urlopen(request) as r:
            response = json.load(r)

        latest = response['sha']
        if pkg.version == latest:
            print(f"\tup-to-date")
        else:
            print(f"\toutdated")
            print(f"\t\tcurrent: {pkg.version}")
            print(f"\t\t latest: {latest}")

            deps_content = DEPS_MK_PATH.read_text(encoding='utf-8')
            deps_content = re.sub(f"^{identifier}_version = (.+)$", f"{identifier}_version = {latest}",
                                  deps_content, flags=re.MULTILINE)
            DEPS_MK_PATH.write_bytes(deps_content.encode('utf-8'))

            subprocess.run(["git", "add", "deps.mk"], cwd=RELENG_DIR, check=True)
            subprocess.run(["git", "commit", "-m" f"deps: Bump {pkg.name} to {latest[:7]}"], cwd=RELENG_DIR, check=True)

        print("")


def compute_bundle_parameters(bundle: Bundle, machine: MachineSpec, version: str) -> tuple[str, str, str]:
    if bundle == Bundle.TOOLCHAIN and machine.os == "windows":
        os_arch_config = "windows-x86"
    else:
        os_arch_config = machine.identifier
    suffix = ".exe" if machine.os == "windows" else ".tar.bz2"
    filename = "{}-{}{}".format(bundle.name.lower(), os_arch_config, suffix)
    url = BUNDLE_URL.format(version=version, filename=filename)
    return (url, filename, suffix)


def read_dependency_parameters(host_defines: dict[str, str] = {}) -> DependencyParameters:
    raw_params = host_defines.copy()
    for match in CONFIG_KEY_VALUE_PATTERN.finditer(DEPS_MK_PATH.read_text(encoding='utf-8')):
        key, value = match.group(1, 2)
        value = value \
                .replace("\\\n", " ") \
                .replace("\t", " ") \
                .replace("$(NULL)", "") \
                .strip()
        while "  " in value:
            value = value.replace("  ", " ")
        raw_params[key] = value

    packages = {}
    for key in [k for k in raw_params.keys() if k.endswith("_recipe")]:
        name = key[:-7]
        packages[name] = PackageSpec(
                parse_string_value(raw_params[name + "_name"], raw_params),
                parse_string_value(raw_params[name + "_version"], raw_params),
                parse_string_value(raw_params[name + "_url"], raw_params),
                parse_string_value(raw_params[name + "_recipe"], raw_params),
                parse_array_value(raw_params[name + "_patches"], raw_params),
                parse_array_value(raw_params[name + "_deps"], raw_params),
                parse_array_value(raw_params[name + "_deps_for_build"], raw_params),
                parse_array_value(raw_params[name + "_options"], raw_params))

    return DependencyParameters(
            raw_params["frida_deps_version"],
            raw_params["frida_bootstrap_version"],
            packages)


def configure_bootstrap_version(version):
    deps_content = DEPS_MK_PATH.read_text(encoding='utf-8')
    deps_content = re.sub("^frida_bootstrap_version = (.+)$", "frida_bootstrap_version = {}".format(version),
                          deps_content, flags=re.MULTILINE)
    DEPS_MK_PATH.write_bytes(deps_content.encode('utf-8'))


def parse_string_value(v: str, raw_params: dict[str, str]) -> str:
    return CONFIG_VARIABLE_REF_PATTERN.sub(lambda match: raw_params.get(match.group(1), ""), v)


def parse_array_value(v: str, raw_params: dict[str, str]) -> list[str]:
    v = parse_string_value(v, raw_params)
    if v == "":
        return []
    return v.split(" ")


def perform(*args, **kwargs):
    print(">", " ".join([str(arg) for arg in args]), flush=True)
    return subprocess.run(args, check=True, **kwargs)


def query_git_head(repo_path: str) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_path, encoding="utf-8").strip()


def format_duration(duration_in_seconds: float) -> str:
    hours, remainder = divmod(duration_in_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "{:02d}:{:02d}:{:02d}".format(int(hours), int(minutes), int(seconds))


if __name__ == "__main__":
    main()
