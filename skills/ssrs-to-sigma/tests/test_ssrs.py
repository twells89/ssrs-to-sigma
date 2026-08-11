#!/usr/bin/env python3
"""Offline regression tests for the ssrs-to-sigma pipeline (stdlib only).

Run:  python3 -m unittest discover -s tests   (from skills/ssrs-to-sigma)
  or: python3 tests/test_ssrs.py

Covers the two failure modes that previously passed silently:
  * RDL 2016 <ReportSections> layouts losing every visual, and
  * an obsolete (pre-workbooks-as-code) workbook spec envelope.
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SCRIPTS = os.path.join(SKILL, "scripts")
FIXTURES = os.path.join(SKILL, "fixtures")
sys.path.insert(0, SCRIPTS)

import parse_rdl      # noqa: E402
import scan_gaps      # noqa: E402
import convert        # noqa: E402


def _fixture(name):
    return os.path.join(FIXTURES, name)


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def _write_rdl(xml):
    fd, path = tempfile.mkstemp(suffix=".rdl")
    with os.fdopen(fd, "w") as fh:
        fh.write(xml)
    return path


def _convert(bundle):
    flags = convert.Flags()
    _dm_spec, dm_index = convert.build_dm(bundle, "CONN", "DM", "FOLDER", True, flags)
    wb = convert.build_workbook(bundle, None, None, dm_index, "WB", "FOLDER", flags)
    return wb, flags


class LegacyGoldenTest(unittest.TestCase):
    def test_legacy_bundle_unchanged(self):
        """The 2008/2010-style flat <Body> golden must stay byte-for-byte."""
        rep = parse_rdl.parse_rdl(_fixture("SalesByRegion.rdl"))
        expected = _load(_fixture("expected_bundle.json"))["reports"][0]
        self.assertEqual(rep, expected)
        self.assertNotIn("warnings", rep)


class ReportSectionsTest(unittest.TestCase):
    def setUp(self):
        self.rep = parse_rdl.parse_rdl(_fixture("ReportSections2016.rdl"))

    def test_matches_golden(self):
        expected = _load(_fixture("expected_reportsections_bundle.json"))["reports"][0]
        self.assertEqual(self.rep, expected)

    def test_section_body_items_retained(self):
        kinds = [i["kind"] for i in self.rep["bodyItems"]]
        self.assertEqual(kinds, ["subreport", "tablix"])

    def test_section_header_and_footer_retained(self):
        self.assertEqual(
            [i["value"] for i in self.rep["pageHeaderItems"]],
            ["Sampling Report Pharmacy Audit"],
        )
        self.assertEqual(
            [i["value"] for i in self.rep["pageFooterItems"]],
            ["Confidential"],
        )

    def test_subreport_forces_unhandled(self):
        bucket, reasons = scan_gaps.classify_report(self.rep)
        self.assertEqual(bucket, "UNHANDLED")
        self.assertTrue(any("subreport" in r.lower() for r in reasons["unhandled"]))


class MultiSectionTest(unittest.TestCase):
    def test_items_concatenate_across_sections(self):
        rep = parse_rdl.parse_rdl(_fixture("MultiSection2016.rdl"))
        kinds = [i["kind"] for i in rep["bodyItems"]]
        self.assertEqual(kinds, ["chart", "tablix"])
        self.assertNotIn("warnings", rep)


_NO_LAYOUT_RDL = """<?xml version="1.0" encoding="utf-8"?>
<Report xmlns="http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition">
  <DataSets>
    <DataSet Name="D"><Query><CommandText>SELECT 1 AS X</CommandText></Query></DataSet>
  </DataSets>
</Report>
"""

_EMPTY_BODY_RDL = """<?xml version="1.0" encoding="utf-8"?>
<Report xmlns="http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition">
  <DataSets>
    <DataSet Name="D"><Query><CommandText>SELECT 1 AS X</CommandText></Query>
      <Fields><Field Name="X"><DataField>X</DataField></Field></Fields>
    </DataSet>
  </DataSets>
  <ReportSections><ReportSection><Body><Height>1in</Height></Body></ReportSection></ReportSections>
</Report>
"""


class EmptyLayoutDiagnosticsTest(unittest.TestCase):
    def test_missing_layout_raises(self):
        path = _write_rdl(_NO_LAYOUT_RDL)
        try:
            with self.assertRaises(ValueError):
                parse_rdl.parse_rdl(path)
        finally:
            os.remove(path)

    def test_empty_body_with_datasets_warns_and_is_manual(self):
        path = _write_rdl(_EMPTY_BODY_RDL)
        try:
            rep = parse_rdl.parse_rdl(path)
        finally:
            os.remove(path)
        self.assertEqual(rep["bodyItems"], [])
        self.assertIn("warnings", rep)
        self.assertTrue(rep["warnings"])
        bucket, reasons = scan_gaps.classify_report(rep)
        self.assertEqual(bucket, "MANUAL")
        self.assertTrue(any("no report visuals" in r.lower() for r in reasons["manual"]))


class WorkbookSpecShapeTest(unittest.TestCase):
    def setUp(self):
        rep = parse_rdl.parse_rdl(_fixture("SalesByRegion.rdl"))
        self.bundle = {"version": 1, "reports": [rep]}
        self.wb, self.flags = _convert(self.bundle)

    def test_wrapped_document_envelope(self):
        self.assertEqual(set(self.wb), {"name", "folderId", "document"})
        doc = self.wb["document"]
        self.assertEqual(doc["kind"], "workbook")
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertIsInstance(doc["elements"], list)
        self.assertTrue(doc["elements"])

    def test_pages_are_metadata_only(self):
        for page in self.wb["document"]["pages"]:
            self.assertNotIn("elements", page)

    def test_every_element_placed_exactly_once(self):
        doc = self.wb["document"]
        ids = [e["id"] for e in doc["elements"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate element ids")
        import re
        placed = re.findall(r'elementId="([^"]+)"', doc["layout"])
        self.assertEqual(sorted(placed), sorted(ids))

    def test_controls_use_current_field_names(self):
        controls = [e for e in self.wb["document"]["elements"] if e.get("kind") == "control"]
        self.assertTrue(controls)
        for c in controls:
            self.assertNotIn("multiSelect", c)
            self.assertNotIn("defaultValue", c)
        lists = [c for c in controls if c["controlType"] == "list"]
        self.assertTrue(lists)
        for c in lists:
            self.assertIn("selectionMode", c)
            self.assertIn("values", c)
        dates = [c for c in controls if c["controlType"] == "date-range"]
        self.assertTrue(dates, "DateTime params should map to date-range")

    def test_text_element_uses_body(self):
        texts = [e for e in self.wb["document"]["elements"] if e.get("kind") == "text"]
        self.assertTrue(texts)
        for t in texts:
            self.assertIn("body", t)
            self.assertNotIn("content", t)
            self.assertNotIn("name", t)


class GroupedTableTest(unittest.TestCase):
    def test_grouped_table_gets_groupings(self):
        rep = parse_rdl.parse_rdl(_fixture("ReportSections2016.rdl"))
        wb, _flags = _convert({"version": 1, "reports": [rep]})
        tables = [e for e in wb["document"]["elements"]
                  if e.get("kind") == "table" and e["id"].startswith("tbx-")]
        self.assertTrue(tables, "expected a grouped Tablix table element")
        for t in tables:
            self.assertIn("groupings", t)
            grp = t["groupings"][0]
            self.assertTrue(grp["groupBy"])
            self.assertTrue(grp["calculations"])


class WorkbookValidatorTest(unittest.TestCase):
    def test_dangling_layout_reference_rejected(self):
        spec = {
            "name": "x", "folderId": "f",
            "document": {
                "schemaVersion": 1, "kind": "workbook",
                "elements": [{"id": "a", "kind": "text", "body": "hi"}],
                "pages": [{"id": "p", "name": "P"}],
                "layout": '<Page id="p"><Element elementId="ghost"/></Page>',
            },
        }
        with self.assertRaises(ValueError):
            convert._validate_workbook(spec)

    def test_unplaced_element_rejected(self):
        spec = {
            "name": "x", "folderId": "f",
            "document": {
                "schemaVersion": 1, "kind": "workbook",
                "elements": [{"id": "a", "kind": "text", "body": "hi"}],
                "pages": [{"id": "p", "name": "P"}],
                "layout": '<Page id="p"></Page>',
            },
        }
        with self.assertRaises(ValueError):
            convert._validate_workbook(spec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
