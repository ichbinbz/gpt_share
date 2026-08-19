import csv
import json
import zipfile
from pathlib import Path

from scripts.import_employee_tokens_xlsx import enroll, read_employee_rows


def employee_workbook(path: Path) -> None:
    xml = """<?xml version="1.0" encoding="utf-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="6">
      <c r="B6" t="str"><v>胡鹏</v></c>
      <c r="C6"><v>465</v></c>
      <c r="D6" t="str"><v>P.HU-00465</v></c>
    </row>
    <row r="7">
      <c r="B7" t="str"><v>研发公用</v></c>
      <c r="C7" />
      <c r="D7" />
    </row>
  </sheetData>
</worksheet>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", xml)


def test_reads_employee_label_rows_and_preserves_leading_zero(tmp_path: Path):
    workbook = tmp_path / "employees.xlsx"
    employee_workbook(workbook)
    assert read_employee_rows(workbook) == [
        {"employee_name": "胡鹏", "employee_id": "00465", "label": "P.HU-00465"}
    ]


def test_bulk_enrollment_stores_hash_and_root_distribution_secret(tmp_path: Path):
    workbook = tmp_path / "employees.xlsx"
    registry = tmp_path / "device-tokens.json"
    distribution = tmp_path / "employee-tokens.csv"
    employee_workbook(workbook)
    result = enroll(workbook, registry, distribution)
    assert result["created"] == 1
    stored = json.loads(registry.read_text(encoding="utf-8"))["devices"][0]
    with distribution.open(encoding="utf-8-sig", newline="") as handle:
        issued = list(csv.DictReader(handle))[0]
    assert stored["label"] == "P.HU-00465"
    assert stored["employee_name"] == "胡鹏"
    assert stored["employee_id"] == "00465"
    assert issued["device_token"].startswith("cwsdt_")
    assert issued["device_token"] not in registry.read_text(encoding="utf-8")
    assert distribution.stat().st_mode & 0o777 == 0o600
