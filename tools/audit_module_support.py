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

"""Audit app-side module/tristate readiness.

This helper scans the apps tree for directories that contain a Makefile with
MAINSRC and classifies each candidate into one of the following groups:

  READY
    The directory already uses MODULE = $(CONFIG_...) and the backing Kconfig
    symbol is tristate.

  BOOL_NEEDS_TRISTATE
    The directory already uses MODULE = $(CONFIG_...), but the backing Kconfig
    symbol is still bool.

  MAKEFILE_NEEDS_MODULE
    The directory looks like an executable application, but the Makefile does
    not expose MODULE = $(CONFIG_...).

  MANUAL_REVIEW
    The directory has MAINSRC, but should not be converted mechanically because
    its executable path is bootloader-specific or tied to a non-Dynamic-ELF
    runtime mechanism.

  NO_CONFIG_SYMBOL
    A config symbol could not be resolved for the directory, so it needs a
    manual review.

The script is intended to support the Dynamic ELF packaging work where menu
configurable applications should be loadable as modules when that path is
supported.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional


CONFIG_RE = re.compile(r"^config\s+([A-Z0-9_]+)$")
TYPE_RE = re.compile(r"^\s*(bool|tristate)\b")
MODULE_RE = re.compile(r"^\s*MODULE\s*(?::=|\+=|=)\s*\$\((CONFIG_[A-Z0-9_]+)\)",
                       re.M)
EXCLUDED_BOOL_MODULE_SYMBOLS = {
    "SYSTEM_NXPKG",
}

MANUAL_REVIEW_MAKEFILES = {
    "boot/mcuboot/Makefile":
        "bootloader application; module conversion needs boot-flow review",
    "boot/miniboot/Makefile":
        "bootloader application; module conversion needs boot-flow review",
    "boot/nxboot/Makefile":
        "bootloader application; module conversion needs boot-flow review",
    "netutils/thttpd/Makefile":
        "CGI binfs/NXFLAT path; not a normal Dynamic ELF app conversion",
}


@dataclass
class ConfigDef:
    symbol: str
    cfg_type: str
    path: Path
    line: int


@dataclass
class AuditRow:
    status: str
    makefile: Path
    symbol: str
    cfg_type: str
    module_symbol: str
    kconfig: str
    line: str
    note: str = ""


def build_config_index(apps_dir: Path) -> Dict[str, ConfigDef]:
    index: Dict[str, ConfigDef] = {}

    for path in apps_dir.rglob("Kconfig*"):
        if not path.is_file():
            continue

        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue

        for i, line in enumerate(lines):
            match = CONFIG_RE.match(line)
            if not match:
                continue

            symbol = match.group(1)
            cfg_type = ""

            for next_line in lines[i + 1 : i + 8]:
                type_match = TYPE_RE.match(next_line)
                if type_match:
                    cfg_type = type_match.group(1)
                    break

            index[symbol] = ConfigDef(symbol, cfg_type, path, i + 1)

    return index


def first_symbol_from_local_kconfig(makefile: Path) -> str:
    for name in ("Kconfig", "Kconfig.debug", "Kconfig.tests"):
        path = makefile.parent / name
        if not path.exists():
            continue

        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                match = CONFIG_RE.match(line)
                if match:
                    return match.group(1)
        except OSError:
            continue

    return ""


def classify_makefile(apps_dir: Path, makefile: Path,
                      config_index: Dict[str, ConfigDef]) -> Optional[AuditRow]:
    try:
        text = makefile.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    if "MAINSRC" not in text:
        return None

    module_match = MODULE_RE.search(text)
    module_symbol = module_match.group(1) if module_match else ""
    config_symbol = module_symbol[7:] if module_symbol.startswith("CONFIG_") else ""

    if not config_symbol:
        config_symbol = first_symbol_from_local_kconfig(makefile)

    if not config_symbol:
        return AuditRow(
            "NO_CONFIG_SYMBOL",
            makefile,
            "",
            "",
            module_symbol,
            "",
            "",
        )

    config_def = config_index.get(config_symbol)
    if config_def is None:
        return AuditRow(
            "NO_CONFIG_SYMBOL",
            makefile,
            config_symbol,
            "",
            module_symbol,
            "",
            "",
        )

    note = ""
    review_key = makefile.relative_to(apps_dir).as_posix()

    if module_symbol:
        if config_def.cfg_type == "tristate":
            status = "READY"
        elif config_def.cfg_type == "bool":
            if config_def.symbol in EXCLUDED_BOOL_MODULE_SYMBOLS:
                status = "READY"
            else:
                status = "BOOL_NEEDS_TRISTATE"
        else:
            status = "NO_CONFIG_SYMBOL"
    elif review_key in MANUAL_REVIEW_MAKEFILES:
        status = "MANUAL_REVIEW"
        note = MANUAL_REVIEW_MAKEFILES[review_key]
    else:
        status = "MAKEFILE_NEEDS_MODULE"

    return AuditRow(
        status,
        makefile,
        config_def.symbol,
        config_def.cfg_type,
        module_symbol,
        str(config_def.path),
        str(config_def.line),
        note,
    )


def iter_rows(apps_dir: Path) -> Iterable[AuditRow]:
    config_index = build_config_index(apps_dir)

    for makefile in sorted(apps_dir.rglob("Makefile")):
        row = classify_makefile(apps_dir, makefile, config_index)
        if row is not None:
            yield row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "apps_dir",
        nargs="?",
        default=Path(__file__).resolve().parents[1],
        type=Path,
        help="Path to the apps tree",
    )
    parser.add_argument(
        "--status",
        action="append",
        default=[],
        help="Only print rows matching the given status",
    )
    args = parser.parse_args()

    apps_dir = args.apps_dir.resolve()
    rows = list(iter_rows(apps_dir))

    if args.status:
        allow = set(args.status)
        rows = [row for row in rows if row.status in allow]

    for row in rows:
        print(
            f"{row.status}|{row.makefile}|{row.symbol}|{row.cfg_type}|"
            f"{row.module_symbol}|{row.kconfig}|{row.line}|{row.note}"
        )

    summary: Dict[str, int] = {}
    for row in rows:
        summary[row.status] = summary.get(row.status, 0) + 1

    print("-- summary --", file=sys.stderr)
    for key in sorted(summary):
        print(f"{key}: {summary[key]}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
