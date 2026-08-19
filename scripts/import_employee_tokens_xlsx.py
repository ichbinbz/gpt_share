#!/usr/bin/env python3
"""Bulk-create hashed CWS device tokens from the employee label workbook."""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

if __package__:
    from .codex_device_tokens import atomic_write, create_token, load_registry
else:
    from codex_device_tokens import atomic_write, create_token, load_registry


NAMESPACE = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
LABEL_PATTERN = re.compile(r"^[A-Z0-9]+(?:\.[A-Z0-9]+)?-(\d{5})$")


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(node.itertext()) for node in root.findall("x:si", NAMESPACE)]


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    value = cell.find("x:v", NAMESPACE)
    if value is None or value.text is None:
        inline = cell.find("x:is", NAMESPACE)
        return "" if inline is None else "".join(inline.itertext())
    if cell.get("t") == "s":
        return shared[int(value.text)]
    return value.text


def read_employee_rows(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        root = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rows: list[dict[str, str]] = []
    for row in root.findall(".//x:sheetData/x:row", NAMESPACE):
        if int(row.get("r", "0")) < 6:
            continue
        values = {
            cell.get("r", "")[:1]: _cell_value(cell, shared).strip()
            for cell in row.findall("x:c", NAMESPACE)
        }
        name = values.get("B", "")
        label = values.get("D", "")
        if not name or not label:
            continue
        match = LABEL_PATTERN.fullmatch(label)
        if match is None:
            raise ValueError(f"invalid employee token label: {label}")
        employee_id = values.get("C", "").split(".", 1)[0].zfill(5)
        if employee_id != match.group(1):
            raise ValueError(f"employee id does not match token label: {name} / {label}")
        rows.append({"employee_name": name, "employee_id": employee_id, "label": label})
    labels = [row["label"] for row in rows]
    if not labels or len(labels) != len(set(labels)):
        raise ValueError("employee token labels are empty or duplicated")
    return rows


def write_distribution(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=("employee_name", "employee_id", "device_label", "device_token"),
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def enroll(
    workbook_path: Path,
    registry_path: Path,
    distribution_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    employees = read_employee_rows(workbook_path)
    lock_path = registry_path.with_suffix(registry_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = load_registry(registry_path)
        existing_labels = {
            str(device.get("label"))
            for device in registry["devices"]
            if isinstance(device, dict) and device.get("label")
        }
        pending = [employee for employee in employees if employee["label"] not in existing_labels]
        if dry_run:
            return {
                "validated": len(employees),
                "to_create": len(pending),
                "already_exists": len(employees) - len(pending),
            }
        if distribution_path.exists():
            raise FileExistsError(f"distribution file already exists: {distribution_path}")
        distribution: list[dict[str, str]] = []
        for employee in pending:
            token = create_token(registry, employee["label"])
            device = registry["devices"][-1]
            device["employee_name"] = employee["employee_name"]
            device["employee_id"] = employee["employee_id"]
            distribution.append(
                {
                    "employee_name": employee["employee_name"],
                    "employee_id": employee["employee_id"],
                    "device_label": employee["label"],
                    "device_token": token,
                }
            )
        write_distribution(distribution_path, distribution)
        try:
            atomic_write(registry_path, registry)
        except BaseException:
            distribution_path.unlink(missing_ok=True)
            raise
        return {
            "validated": len(employees),
            "created": len(distribution),
            "already_exists": len(employees) - len(distribution),
            "distribution_file": str(distribution_path),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("/var/lib/cws-codex/device-tokens.json"),
    )
    parser.add_argument(
        "--distribution",
        type=Path,
        default=Path("/root/cws-codex-employee-tokens.csv"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            enroll(args.workbook, args.registry, args.distribution, args.dry_run),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
