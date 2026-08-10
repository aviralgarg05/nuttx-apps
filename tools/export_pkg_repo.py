#!/usr/bin/env python3
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.  The
# ASF licenses this file to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the
# License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations
# under the License.

"""Build an nxpkg repository from artifacts and Package.mk files."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VALID_TYPES = {"elf", "shared-lib"}
PACKAGE_SPEC_HELP = (
    "package must look like " "<name>:<version>:<elf|shared-lib>:<source>"
)

# Keep icons small for target downloads.
ICON_SIZE = 48

# LVGL v9 lv_image_header_t fields, followed by raw RGB565 pixels.
LV_IMAGE_HEADER_MAGIC = 0x19
LV_COLOR_FORMAT_RGB565 = 0x12

DECLARATION_NAME = "Package.mk"

MODULE_TYPES = {
    "executable": "elf",
    "shared_library": "shared-lib",
}


@dataclass
class PackageSpec:
    name: str
    version: str
    payload_type: str
    source: Path
    requires: List[str] = field(default_factory=list)
    description: Optional[str] = None
    category: Optional[str] = None
    icon: Optional[Path] = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as infile:
        while True:
            chunk = infile.read(65536)
            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def validate_component(value: str, label: str = "value") -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{label} must be one path component")

    return value


def parse_component(value: str) -> str:
    try:
        return validate_component(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def parse_package_spec(value: str) -> PackageSpec:
    parts = value.split(":", 3)
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(PACKAGE_SPEC_HELP)

    name, version, payload_type, source = parts
    name = parse_component(name)
    version = parse_component(version)
    if payload_type not in VALID_TYPES:
        raise argparse.ArgumentTypeError(
            f"unsupported package type '{payload_type}', expected one of "
            f"{', '.join(sorted(VALID_TYPES))}"
        )

    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        msg = f"source does not exist: {source_path}"
        raise argparse.ArgumentTypeError(msg)

    return PackageSpec(
        name=name,
        version=version,
        payload_type=payload_type,
        source=source_path,
    )


def parse_name_value(value: str) -> tuple:
    parts = value.split("=", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise argparse.ArgumentTypeError("value must look like <name>=<text>")

    return parse_component(parts[0]), parts[1]


def parse_icon_spec(value: str) -> tuple:
    name, source = parse_name_value(value)
    name = parse_component(name)
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        msg = f"icon source does not exist: {source_path}"
        raise argparse.ArgumentTypeError(msg)

    return name, source_path


def parse_assignments(path: Path) -> Dict[str, str]:
    """Read the `NAME := value` assignments out of a declaration file."""

    values: Dict[str, str] = {}

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        for separator in (":=", "="):
            key, found, value = line.partition(separator)
            if found:
                values[key.strip()] = value.strip()
                break

    return values


def declaration_icon(path: Path, value: str) -> Path:
    root = path.parent.resolve()
    icon = (root / value).resolve()
    try:
        icon.relative_to(root)
    except ValueError as error:
        message = f"{path}: LOCAL_ICON must stay below {root}"
        raise ValueError(message) from error

    if not icon.is_file():
        raise ValueError(f"{path}: icon does not exist: {icon}")

    return icon


def declaration_to_spec(path: Path, bindir: Path) -> Optional[PackageSpec]:
    """Read one built package declaration."""

    values = parse_assignments(path)

    name = values.get("LOCAL_MODULE")
    if not name:
        raise ValueError(f"{path}: LOCAL_MODULE is required")
    validate_component(name, f"{path}: LOCAL_MODULE")

    version = values.get("LOCAL_VERSION")
    if not version:
        raise ValueError(f"{path}: LOCAL_VERSION is required")
    validate_component(version, f"{path}: LOCAL_VERSION")

    module_type = values.get("LOCAL_MODULE_TYPE", "executable")
    payload_type = MODULE_TYPES.get(module_type)
    if payload_type is None:
        expected = ", ".join(sorted(MODULE_TYPES))
        raise ValueError(
            f"{path}: unsupported LOCAL_MODULE_TYPE '{module_type}', "
            f"expected one of {expected}"
        )

    filename = values.get("LOCAL_MODULE_FILENAME", name)
    validate_component(filename, f"{path}: LOCAL_MODULE_FILENAME")
    source = bindir / filename
    if not source.is_file():
        return None

    requires = values.get("LOCAL_SHARED_LIBS", "").split()
    for dependency in requires:
        validate_component(dependency, f"{path}: LOCAL_SHARED_LIBS")

    icon = None
    if values.get("LOCAL_ICON"):
        icon = declaration_icon(path, values["LOCAL_ICON"])

    return PackageSpec(
        name=name,
        version=version,
        payload_type=payload_type,
        source=source.resolve(),
        requires=requires,
        description=values.get("LOCAL_DESCRIPTION") or None,
        category=values.get("LOCAL_CATEGORY") or None,
        icon=icon,
    )


def discover_declarations(
    roots: List[Path], bindir: Path
) -> List[PackageSpec]:  # fmt: skip
    """Collect a specification for every declaration file below the roots."""

    specs: List[PackageSpec] = []

    for root in roots:
        if not root.is_dir():
            raise ValueError(f"scan path is not a directory: {root}")

        for path in sorted(root.rglob(DECLARATION_NAME)):
            spec = declaration_to_spec(path, bindir)
            if spec is not None:
                specs.append(spec)

    return specs


def read_config(path: Path) -> Dict[str, str]:
    """Read the CONFIG_* settings out of a generated NuttX .config."""

    config: Dict[str, str] = {}

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("CONFIG_") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        config[key.strip()] = value.strip().strip('"')

    return config


def derive_target(config: Dict[str, str]) -> Tuple[str, str, str]:
    """Work out the target identity a package must match at install time.

    These have to agree with pkg_runtime_arch() and pkg_runtime_compat() in
    system/nxpkg, which the target checks a manifest against before it will
    activate a package.
    """

    arch = config.get("CONFIG_ARCH", "")
    chip = config.get("CONFIG_ARCH_CHIP", "")
    compat = config.get("CONFIG_ARCH_BOARD", "")
    if not compat:
        compat = config.get("CONFIG_ARCH_BOARD_CUSTOM_NAME", "")

    try:
        validate_component(arch, "CONFIG_ARCH")
        validate_component(chip, "CONFIG_ARCH_CHIP")
        validate_component(compat, "CONFIG_ARCH_BOARD")
    except ValueError as error:
        raise ValueError(
            "could not determine the target from the configuration; "
            "pass --arch/--chip/--compat explicitly"
        ) from error

    return arch, chip, compat


def artifact_relpath(arch: str, chip: str, compat: str,
                     spec: PackageSpec) -> Path:  # fmt: skip
    filename = spec.source.name
    path = Path("artifacts") / arch / chip / compat
    path = path / spec.name / spec.version / filename
    return path


def icon_relpath(arch: str, chip: str, compat: str, name: str) -> Path:
    # The client refreshes this object when the catalog version changes.
    return Path("icons") / arch / chip / compat / f"{name}.bin"


def encode_icon_rgb565(source: Path, size: int = ICON_SIZE) -> bytes:
    """Encode an image as an LVGL header and RGB565 pixels."""

    from PIL import Image

    with Image.open(source) as img:
        img = img.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)
        pixels = list(img.getdata())

    stride = size * 2
    header = struct.pack(
        "<BBHHHHH",
        LV_IMAGE_HEADER_MAGIC,
        LV_COLOR_FORMAT_RGB565,
        0,  # flags
        size,  # w
        size,  # h
        stride,
        0,  # reserved_2
    )

    body = bytearray(stride * size)
    offset = 0
    for r, g, b in pixels:
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        struct.pack_into("<H", body, offset, rgb565)
        offset += 2

    return header + bytes(body)


def package_identity(package: Dict[str, str]) -> tuple:
    return (
        package["name"],
        package["version"],
        package["arch"],
        package["compat"],
        package["type"],
    )


def load_existing_packages(repo_dir: Path) -> List[dict]:
    index_path = repo_dir / "index.json"

    if not index_path.exists():
        return []

    root = json.loads(index_path.read_text(encoding="utf-8"))
    if isinstance(root, list):
        packages = root
    else:
        packages = root.get("packages")

    if not isinstance(packages, list):
        raise ValueError(f"invalid repository index format in {index_path}")

    for item in packages:
        if not isinstance(item, dict):
            raise ValueError(f"invalid repository index entry in {index_path}")

    return packages


def emit_index(repo_dir: Path, packages: List[dict]) -> None:
    index_path = repo_dir / "index.json"
    payload = {"packages": packages}
    index_path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "repo_dir",
        type=Path,
        help="Destination repository directory",
    )
    parser.add_argument(
        "--arch",
        type=parse_component,
        help="Target architecture, for example xtensa. Read from the "
        "configuration when not given",
    )
    parser.add_argument(
        "--chip",
        type=parse_component,
        help="Target chip/family, for example esp32s3. Read from the "
        "configuration when not given",
    )
    parser.add_argument(
        "--compat",
        type=parse_component,
        help="Target board identity, for example esp32s3-xiao. Read from "
        "the configuration when not given",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("../nuttx/.config"),
        help="Generated NuttX .config the target is read from",
    )
    parser.add_argument(
        "--scan",
        action="append",
        type=Path,
        default=[],
        help=f"Directory to search for {DECLARATION_NAME} files",
    )
    parser.add_argument(
        "--bindir",
        type=Path,
        default=Path("bin"),
        help="Directory holding the built artifacts named by LOCAL_MODULE",
    )
    parser.add_argument(
        "--artifact-prefix",
        default="",
        help="Optional prefix prepended to artifact paths in index.json",
    )
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        type=parse_package_spec,
        help=PACKAGE_SPEC_HELP,
    )
    parser.add_argument(
        "--package-description",
        action="append",
        default=[],
        type=parse_name_value,
        metavar="NAME=TEXT",
        help="Optional package description, keyed by package name",
    )
    parser.add_argument(
        "--package-category",
        action="append",
        default=[],
        type=parse_name_value,
        metavar="NAME=TEXT",
        help="Optional package category, keyed by package name",
    )
    parser.add_argument(
        "--package-icon",
        action="append",
        default=[],
        type=parse_icon_spec,
        metavar="NAME=PATH",
        help="Optional source image for a package icon",
    )
    args = parser.parse_args()

    arch, chip, compat = args.arch, args.chip, args.compat
    if not (arch and chip and compat):
        config_path = args.config.expanduser()
        if not config_path.is_file():
            parser.error(
                f"no configuration at {config_path}; pass --config, or give "
                "--arch/--chip/--compat explicitly"
            )

        try:
            derived = derive_target(read_config(config_path))
        except ValueError as error:
            parser.error(str(error))

        arch = arch or derived[0]
        chip = chip or derived[1]
        compat = compat or derived[2]

    specs = list(args.package)
    try:
        bindir = args.bindir.expanduser().resolve()
        specs.extend(discover_declarations(args.scan, bindir))
    except ValueError as error:
        parser.error(str(error))

    if not specs:
        parser.error("no built packages found; pass --scan or --package")

    repo_dir = args.repo_dir.expanduser().resolve()
    repo_dir.mkdir(parents=True, exist_ok=True)

    packages = load_existing_packages(repo_dir)
    packages_by_id = {}
    for package in packages:
        packages_by_id[package_identity(package)] = package
    prefix = args.artifact_prefix.rstrip("/")
    descriptions = dict(args.package_description)
    categories = dict(args.package_category)
    icons = dict(args.package_icon)

    for spec in specs:
        relpath = artifact_relpath(arch, chip, compat, spec)
        destination = repo_dir / relpath
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(spec.source, destination)

        artifact = relpath.as_posix()
        if prefix:
            artifact = f"{prefix}/{artifact}"

        package = {
            "name": spec.name,
            "version": spec.version,
            "arch": arch,
            "compat": compat,
            "artifact": artifact,
            "sha256": sha256_file(destination),
            "type": spec.payload_type,
        }

        description = descriptions.get(spec.name, spec.description)
        if description:
            package["description"] = description

        category = categories.get(spec.name, spec.category)
        if category:
            package["category"] = category

        icon = icons.get(spec.name, spec.icon)
        if icon:
            icon_bytes = encode_icon_rgb565(icon)
            icon_dest = repo_dir / icon_relpath(arch, chip, compat, spec.name)
            icon_dest.parent.mkdir(parents=True, exist_ok=True)
            icon_dest.write_bytes(icon_bytes)

            icon_rel = icon_relpath(arch, chip, compat, spec.name).as_posix()
            package["icon"] = f"{prefix}/{icon_rel}" if prefix else icon_rel

        if spec.requires:
            package["requires"] = spec.requires

        packages_by_id[package_identity(package)] = package

    packages = sorted(
        packages_by_id.values(),
        key=lambda item: (
            item["name"],
            item["version"],
            item["arch"],
            item["compat"],
            item["type"],
        ),
    )
    emit_index(repo_dir, packages)

    print(f"exported {len(packages)} package(s) to {repo_dir}")
    for package in packages:
        print(
            f"- {package['name']} {package['version']} "
            f"[{package['type']}] -> {package['artifact']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
