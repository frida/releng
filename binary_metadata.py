#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
import re
from typing import Mapping, Tuple


ASSETS_DIR = Path(__file__).resolve().parent / "binary-metadata-assets"


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    source = subparsers.add_parser("source", help="generate the C source that embeds the metadata")
    source.add_argument("name")
    source.add_argument("version")
    source.add_argument("architecture")
    source.add_argument("output", type=Path)
    source.set_defaults(func=lambda args: generate_source(args.name, args.version, args.architecture, args.output))

    resource = subparsers.add_parser("resource", help="generate the Windows VERSIONINFO resource script")
    resource.add_argument("kind", choices=FILE_KINDS.keys())
    resource.add_argument("name")
    resource.add_argument("version")
    resource.add_argument("output", type=Path)
    resource.set_defaults(func=lambda args: generate_resource(args.kind, args.name, args.version, args.output))

    args = parser.parse_args()
    args.func(args)


def generate_source(name: str, version: str, architecture: str, output: Path):
    metadata = {
        "type": "frida",
        "name": name,
        "version": version,
        "architecture": architecture,
    }
    payload = json.dumps(metadata, separators=(",", ":"))
    payload_as_c_string_literal = json.dumps(payload)
    render("metadata.c.in", {"METADATA": payload_as_c_string_literal}, output)


def generate_resource(kind: str, name: str, version: str, output: Path):
    file_type, file_suffix = FILE_KINDS[kind]
    major, minor, micro, nano = parse_version_quad(version)
    render("versioninfo.rc.in", {
        "MAJOR": major,
        "MINOR": minor,
        "MICRO": micro,
        "NANO": nano,
        "FILETYPE": file_type,
        "DESCRIPTION": " ".join([word.capitalize() for word in name.split("-")]),
        "VERSION": version,
        "NAME": name,
        "FILENAME": name + file_suffix,
    }, output)


def render(template: str, variables: Mapping[str, object], output: Path):
    text = (ASSETS_DIR / template).read_text(encoding="utf-8")
    output.write_text(re.sub(r"@(\w+)@", lambda m: str(variables[m.group(1)]), text), encoding="utf-8")


def parse_version_quad(version: str) -> Tuple[int, int, int, int]:
    name, _, dev = version.partition("-dev.")
    core, _, suffix = name.partition("-")
    major, minor, micro = [int(token) for token in core.split(".")]
    if dev == "":
        return (major, minor, micro, 0)
    if suffix == "":
        micro -= 1
    return (major, minor, micro, int(dev) + 1)


FILE_KINDS = {
    "executable": ("VFT_APP", ".exe"),
    "shared-library": ("VFT_DLL", ".dll"),
}


if __name__ == "__main__":
    main()
