#!/usr/bin/env python3
"""Offline regression tests for the ssrs-to-sigma pipeline (stdlib only).

Run:  python3 -m unittest discover -s tests   (from skills/ssrs-to-sigma)
  or: python3 tests/test_ssrs.py

Covers the two failure modes that previously passed silently:
  * RDL 2016 <ReportSections> layouts losing every visual, and
  * an obsolete (pre-workbooks-as-code) workbook spec envelope.
"""
import json
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SCRIPTS = os.path.join(SKILL, "scripts")
LIB = os.path.join(SCRIPTS, "lib")
FIXTURES = os.path.join(SKILL, "fixtures")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, LIB)

import parse_rdl      # noqa: E402
import scan_gaps      # noqa: E402
import convert        # noqa: E402
import code_rep       # noqa: E402
import publish        # noqa: E402


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


def _convert_report(bundle):
    flags = convert.Flags()
    _dm_spec, dm_index = convert.build_dm(
        bundle, "CONN", "DM", "FOLDER", True, flags
    )
    report = convert.build_report(
        bundle, None, None, dm_index, "Report", "FOLDER", flags
    )
    return report, flags


class LegacyGoldenTest(unittest.TestCase):
    def test_legacy_bundle_unchanged(self):
        """The 2008/2010-style flat <Body> parse must match its golden."""
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


class LayoutMetadataAndTargetTest(unittest.TestCase):
    def test_print_layout_metadata_is_normalized(self):
        rep = parse_rdl.parse_rdl(_fixture("PrintLayout2016.rdl"))
        self.assertEqual(
            rep,
            _load(_fixture("expected_printlayout_bundle.json"))["reports"][0],
        )
        layout = rep["layout"]
        self.assertEqual(layout["reportWidth"], "7.5in")
        self.assertEqual(layout["bodyHeight"], "9.5in")
        self.assertEqual(layout["pageWidth"], "8.5in")
        self.assertEqual(layout["pageHeight"], "11in")
        self.assertEqual(layout["sectionCount"], 1)
        section = layout["sections"][0]
        self.assertEqual(section["bodyHeight"], "9.5in")
        self.assertEqual(section["pageWidth"], "8.5in")
        self.assertEqual(section["pageHeight"], "11in")
        self.assertEqual(section["margins"], {
            "top": "0.5in", "right": "0.5in",
            "bottom": "0.5in", "left": "0.5in",
        })
        self.assertEqual(section["headerHeight"], "0.4in")
        self.assertEqual(section["footerHeight"], "0.3in")
        self.assertEqual(layout["signals"], {
            "pageBreakCount": 1, "listCount": 1, "subreportCount": 1,
        })
        invoice = next(
            item for item in section["itemPositions"]
            if item["name"] == "InvoiceLabel"
        )
        self.assertEqual(invoice["position"]["left"], "0.2in")
        self.assertEqual(invoice["ancestorPositions"][0]["left"], "0.5in")

    def test_auto_target_uses_print_and_dashboard_signals(self):
        dashboard = parse_rdl.parse_rdl(_fixture("SalesByRegion.rdl"))
        printable = parse_rdl.parse_rdl(_fixture("PrintLayout2016.rdl"))
        self.assertEqual(convert.resolve_target(dashboard)["target"], "workbook")
        resolution = convert.resolve_target(printable)
        self.assertEqual(resolution["target"], "report")
        self.assertGreater(
            resolution["printScore"], resolution["dashboardScore"]
        )


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

_PAGE_BREAK_RDL = """<?xml version="1.0" encoding="utf-8"?>
<Report xmlns="http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition">
  <Body>
    <ReportItems>
      <Rectangle Name="Band">
        <PageBreak><BreakLocation>End</BreakLocation><Disabled>{disabled}</Disabled></PageBreak>
        <ReportItems>
          <Textbox Name="Label"><Value>Detail</Value></Textbox>
        </ReportItems>
      </Rectangle>
    </ReportItems>
    <Height>1in</Height>
  </Body>
  <Width>4in</Width>
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


class PageBreakTargetTest(unittest.TestCase):
    def _parse(self, disabled):
        path = _write_rdl(_PAGE_BREAK_RDL.format(disabled=disabled))
        try:
            return parse_rdl.parse_rdl(path)
        finally:
            os.remove(path)

    def test_literal_disabled_page_break_does_not_score_as_print(self):
        rep = self._parse("true")
        self.assertEqual(rep["layout"]["signals"]["pageBreakCount"], 0)
        self.assertEqual(convert.resolve_target(rep)["target"], "workbook")

    def test_dynamic_disabled_page_break_is_manual_and_still_potential(self):
        rep = self._parse("=Parameters!Breaks.Value")
        self.assertEqual(rep["layout"]["signals"]["pageBreakCount"], 1)
        self.assertTrue(any("dynamic" in warning for warning in rep["warnings"]))
        resolution = convert.resolve_target(rep)
        self.assertEqual(resolution["target"], "report")
        self.assertTrue(any(
            "dynamic Disabled" in signal
            for signal in resolution["printSignals"]
        ))


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
        self.assertEqual(lists, [])
        self.assertTrue(any(
            where == "parameter Region"
            and "does not match" in message
            for _report, where, message in self.flags.items
        ))
        dates = [c for c in controls if c["controlType"] == "date-range"]
        self.assertTrue(dates, "DateTime params should map to date-range")

    def test_text_element_uses_body(self):
        texts = [e for e in self.wb["document"]["elements"] if e.get("kind") == "text"]
        self.assertTrue(texts)
        for t in texts:
            self.assertIn("body", t)
            self.assertNotIn("content", t)
            self.assertNotIn("name", t)

    def test_pivot_shelves_use_column_id(self):
        pivots = [
            element for element in self.wb["document"]["elements"]
            if element.get("kind") == "pivot-table"
        ]
        self.assertTrue(pivots)
        for pivot in pivots:
            for shelf in pivot["rowsBy"] + pivot["columnsBy"]:
                self.assertEqual(set(shelf), {"columnId"})

    def test_data_model_is_not_workbook_wrapped(self):
        flags = convert.Flags()
        dm, _ = convert.build_dm(
            self.bundle, "CONN", "DM", "FOLDER", True, flags
        )
        self.assertNotIn("document", dm)
        self.assertIn("pages", dm)

    def test_data_model_source_columns_are_qualified(self):
        base = next(
            element for element in self.wb["document"]["elements"]
            if element["id"].startswith("base-")
        )
        self.assertTrue(base["columns"])
        for column in base["columns"]:
            self.assertRegex(
                column["formula"],
                r"^\[SalesByRegion · OrderFacts/[^]]+\]$",
            )


class CanonicalCodeRepTest(unittest.TestCase):
    def test_theme_alignment_flattening_and_layout_aliases(self):
        wrapped = code_rep.wrap({
            "schemaVersion": 1,
            "kind": "workbook",
            "themeName": "Light",
            "themeOverrides": {"pageBackgroundColor": "#fff"},
            "pages": [{
                "id": "p", "name": "Page",
                "elements": [{
                    "id": "t", "kind": "text", "body": "Title",
                    "verticalAlign": "middle",
                }],
            }],
            "layout": (
                '<Page id="p"><LayoutElement elementId="t"/>'
                '<GridContainer elementId="g"/></Page>'
            ),
        }, {"name": "Workbook", "folderId": "folder"})
        doc = wrapped["document"]
        self.assertEqual(code_rep.theme(wrapped), {
            "name": "Light",
            "overrides": {"pageBackgroundColor": "#fff"},
        })
        self.assertNotIn("elements", doc["pages"][0])
        self.assertEqual(doc["elements"][0]["verticalAlign"], "center")
        self.assertIn("<Element ", doc["layout"])
        self.assertIn("<Container ", doc["layout"])
        self.assertNotIn("themeName", doc)


class ListControlSourceTest(unittest.TestCase):
    def setUp(self):
        self.source = {
            "id": "base-orders",
            "dataSetName": "Orders",
            "columns": [
                {"id": "col-region", "name": "Region"},
                {"id": "col-code", "name": "Code"},
            ],
        }
        self.base_param = {
            "name": "Region",
            "dataType": "String",
            "multiValue": True,
            "prompt": "Region",
            "defaultValues": [],
            "validValuesQuery": None,
            "validValuesStatic": None,
        }

    def test_same_dataset_query_with_exact_field_binds(self):
        param = {
            **self.base_param,
            "validValuesQuery": {
                "dataSet": "Orders",
                "valueField": "Code",
                "labelField": "Region",
            },
        }
        control = convert._control(
            param, convert.Flags(), "Report", self.source
        )
        self.assertEqual(control["source"]["columnId"], "col-code")

    def test_unrelated_valid_values_dataset_is_omitted(self):
        param = {
            **self.base_param,
            "validValuesQuery": {
                "dataSet": "RegionLookup",
                "valueField": "Region",
                "labelField": "Region",
            },
        }
        flags = convert.Flags()
        self.assertIsNone(convert._control(param, flags, "Report", self.source))
        self.assertTrue(any(
            "unrelated table" in message
            for _report, _where, message in flags.items
        ))

    def test_static_values_require_exact_parameter_column(self):
        param = {
            **self.base_param,
            "validValuesStatic": ["East", "West"],
        }
        control = convert._control(
            param, convert.Flags(), "Report", self.source
        )
        self.assertEqual(control["source"]["columnId"], "col-region")
        missing = {**param, "name": "Territory"}
        self.assertIsNone(convert._control(
            missing, convert.Flags(), "Report", self.source
        ))


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


class ReportSpecTest(unittest.TestCase):
    def setUp(self):
        rep = parse_rdl.parse_rdl(_fixture("PrintLayout2016.rdl"))
        self.spec, self.flags = _convert_report(
            {"version": 1, "reports": [rep]}
        )

    def test_wrapped_flat_report_contract(self):
        self.assertEqual(set(self.spec), {"name", "folderId", "document"})
        doc = self.spec["document"]
        self.assertEqual(doc["kind"], "report")
        self.assertEqual(doc["config"], {
            "pageWidth": 816, "pageHeight": 1056, "margin": 48,
        })
        self.assertTrue(doc["panels"])
        for page in doc["pages"]:
            self.assertNotIn("elements", page)
        for panel in doc["panels"]:
            self.assertNotIn("elements", panel)
        self.assertNotRegex(
            doc["layout"],
            r"gridColumn|gridRow|gridTemplate|<Container",
        )

    def test_every_element_has_one_in_bounds_pixel_placement(self):
        doc = self.spec["document"]
        fragment = re.sub(r"^\s*<\?xml[^>]*\?>", "", doc["layout"], count=1)
        root = ET.fromstring(f"<Layout>{fragment}</Layout>")
        placements = [
            node for node in root.iter() if node.get("elementId")
        ]
        ids = [element["id"] for element in doc["elements"]]
        self.assertEqual(
            sorted(node.get("elementId") for node in placements),
            sorted(ids),
        )
        self.assertEqual(len(placements), len(ids))
        for node in placements:
            self.assertGreater(float(node.get("width")), 0)
            self.assertGreater(float(node.get("height")), 0)
            self.assertGreaterEqual(float(node.get("x")), 0)
            self.assertGreaterEqual(float(node.get("y")), 0)

    def test_nested_list_offset_is_preserved(self):
        match = re.search(
            r'<Element elementId="txt-printlayout2016-body-0-invoicelabel" '
            r'x="([^"]+)" y="([^"]+)"',
            self.spec["document"]["layout"],
        )
        self.assertIsNotNone(match)
        # 0.5in page margin + 0.5in List + 0.2in child.
        self.assertAlmostEqual(float(match.group(1)), 115.2)
        # 0.5in margin + 0.4in header + 0.25in List + 0.1in child.
        self.assertAlmostEqual(float(match.group(2)), 120.0)

    def test_unsupported_subreport_is_flagged_not_faked(self):
        bodies = [
            element.get("body", "") for element in self.spec["document"]["elements"]
            if element.get("kind") == "text"
        ]
        self.assertFalse(any("LineItems" in body for body in bodies))
        self.assertTrue(any(
            where == "subreport LineItems"
            for _report, where, _message in self.flags.items
        ))

    def test_report_validator_rejects_grid_syntax(self):
        bad = json.loads(json.dumps(self.spec))
        bad["document"]["layout"] = bad["document"]["layout"].replace(
            'x="', 'gridColumn="1 / 2" x="', 1
        )
        with self.assertRaises(ValueError):
            convert._validate_report(bad)

    def test_body_content_stays_above_footer_and_bottom_margin(self):
        doc = self.spec["document"]
        fragment = re.sub(r"^\s*<\?xml[^>]*\?>", "", doc["layout"], count=1)
        root = ET.fromstring(f"<Layout>{fragment}</Layout>")
        footer_by_page = {}
        for panel in doc["panels"]:
            if panel["type"] == "footer":
                for page_id in panel["pages"]:
                    footer_by_page[page_id] = panel["config"]["height"]
        for page in [node for node in root if node.tag == "Page"]:
            bottom = (
                doc["config"]["pageHeight"]
                - doc["config"]["margin"]
                - footer_by_page.get(page.get("id"), 0)
            )
            for element in page:
                self.assertLessEqual(
                    float(element.get("y")) + float(element.get("height")),
                    bottom + 0.001,
                )

    def test_validator_rejects_body_overlap_with_footer(self):
        bad = json.loads(json.dumps(self.spec))
        element_id = "txt-printlayout2016-body-0-invoicelabel"
        bad["document"]["layout"] = re.sub(
            rf'(<Element elementId="{element_id}"[^>]*\by=")[^"]+(")',
            rf"\g<1>1000\g<2>",
            bad["document"]["layout"],
        )
        with self.assertRaisesRegex(ValueError, "margin|footer"):
            convert._validate_report(bad)


class ConvertCliTargetTest(unittest.TestCase):
    def _run(self, target=None, extra=None):
        dashboard = parse_rdl.parse_rdl(_fixture("SalesByRegion.rdl"))
        printable = parse_rdl.parse_rdl(_fixture("PrintLayout2016.rdl"))
        temp = tempfile.TemporaryDirectory()
        workdir = Path(temp.name)
        bundle = workdir / "bundle.json"
        bundle.write_text(json.dumps({
            "version": 1, "reports": [dashboard, printable],
        }), encoding="utf-8")
        command = [
            sys.executable, os.path.join(SCRIPTS, "convert.py"),
            "--bundle", str(bundle),
            "--connection-id", "CONN",
            "--folder-id", "FOLDER",
            "--out-prefix", str(workdir / "sigma"),
        ]
        if target:
            command.extend(["--target", target])
        command.extend(extra or [])
        subprocess.run(
            command, cwd=workdir, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return temp, workdir

    def test_omitted_target_remains_workbook(self):
        temp, workdir = self._run()
        try:
            self.assertTrue((workdir / "sigma_workbook_spec.json").is_file())
            self.assertFalse((workdir / "sigma_report_spec.json").exists())
            workbook = _load(workdir / "sigma_workbook_spec.json")
            self.assertEqual(len(workbook["document"]["pages"]), 2)
        finally:
            temp.cleanup()

    def test_auto_partitions_a_mixed_bundle(self):
        temp, workdir = self._run("auto")
        try:
            workbook = _load(workdir / "sigma_workbook_spec.json")
            report = _load(workdir / "sigma_report_spec.json")
            resolution = _load(workdir / "sigma_target_resolution.json")
            self.assertEqual(
                [page["name"] for page in workbook["document"]["pages"]],
                ["SalesByRegion"],
            )
            report_page_names = [
                page["name"] for page in report["document"]["pages"]
                if page.get("visibility") != "hidden"
            ]
            self.assertEqual(report_page_names, ["PrintLayout2016"])
            self.assertEqual({
                row["report"]: row["resolvedTarget"]
                for row in resolution["reports"]
            }, {
                "SalesByRegion": "workbook",
                "PrintLayout2016": "report",
            })
        finally:
            temp.cleanup()

    def test_explicit_report_routes_every_source_report(self):
        temp, workdir = self._run("report")
        try:
            self.assertFalse((workdir / "sigma_workbook_spec.json").exists())
            report = _load(workdir / "sigma_report_spec.json")
            visible = [
                page["name"] for page in report["document"]["pages"]
                if page.get("visibility") != "hidden"
            ]
            self.assertEqual(visible, ["SalesByRegion", "PrintLayout2016"])
        finally:
            temp.cleanup()

    def test_report_schema_version_cli_override(self):
        temp, workdir = self._run(
            "report", ["--report-schema-version", "7"]
        )
        try:
            report = _load(workdir / "sigma_report_spec.json")
            self.assertEqual(report["document"]["schemaVersion"], 7)
        finally:
            temp.cleanup()

    def test_rerun_removes_outputs_from_previous_target_mode(self):
        temp, workdir = self._run("auto")
        try:
            self.assertTrue((workdir / "sigma_report_spec.json").is_file())
            self.assertTrue((workdir / "sigma_target_resolution.json").is_file())
            subprocess.run([
                sys.executable, os.path.join(SCRIPTS, "convert.py"),
                "--bundle", str(workdir / "bundle.json"),
                "--connection-id", "CONN",
                "--folder-id", "FOLDER",
                "--target", "workbook",
                "--out-prefix", str(workdir / "sigma"),
            ], cwd=workdir, check=True, stdout=subprocess.PIPE,
               stderr=subprocess.PIPE, text=True)
            self.assertTrue((workdir / "sigma_workbook_spec.json").is_file())
            self.assertFalse((workdir / "sigma_report_spec.json").exists())
            self.assertFalse((workdir / "sigma_target_resolution.json").exists())
        finally:
            temp.cleanup()


class FakePublishClient:
    def __init__(self, spec, verify=None, byte_responses=None):
        self.spec = spec
        self.calls = []
        self.verify = {"valid": True} if verify is None else verify
        self.byte_responses = list(
            byte_responses if byte_responses is not None
            else [b"%PDF-1.7\nfixture"]
        )

    def request_json(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/spec/verify"):
            return self.verify
        if method == "POST" and path.endswith("/spec"):
            kind = "report" if "/reports/" in path else "workbook"
            return {f"{kind}Id": f"{kind}-1", "success": True}
        if method == "GET" and "/spec?format=json" in path:
            return json.loads(json.dumps(self.spec))
        if path.endswith("/export"):
            return {"jobComplete": False, "queryId": "query-1"}
        raise AssertionError((method, path))

    def request_bytes(self, method, path, body=None, accept="*/*"):
        self.calls.append((method, path, body))
        return self.byte_responses.pop(0)


class PublishHelperTest(unittest.TestCase):
    def _workbook(self):
        return {
            "name": "Workbook", "folderId": "folder",
            "document": {
                "schemaVersion": 1, "kind": "workbook",
                "elements": [{"id": "title", "kind": "text", "body": "Title"}],
                "pages": [{"id": "page", "name": "Page"}],
                "layout": (
                    '<Page id="page"><Element elementId="title" '
                    'gridColumn="1 / 25" gridRow="1 / 3"/></Page>'
                ),
            },
        }

    def _rich_workbook(self):
        spec = self._workbook()
        spec["document"]["elements"] = [{
            "id": "title",
            "kind": "table",
            "name": "Orders",
            "source": {
                "kind": "data-model",
                "dataModelId": "dm-1",
                "elementId": "orders",
            },
            "columns": [{
                "id": "amount",
                "name": "Amount",
                "formula": "[Orders/Amount]",
            }],
            "filters": [{"columnId": "amount", "formula": "[Amount] > 0"}],
        }]
        spec["document"]["panels"] = [{
            "id": "sidebar",
            "type": "sidebar",
            "pages": ["page"],
            "config": {"width": 240},
        }]
        return spec

    def test_default_is_verify_only_and_writes_response(self):
        spec = self._workbook()
        fake = FakePublishClient(spec)
        with tempfile.TemporaryDirectory() as directory:
            result = publish.publish_spec(
                "workbook", spec, fake, directory
            )
            self.assertFalse(result["created"])
            self.assertEqual(
                [(call[0], call[1]) for call in fake.calls],
                [("POST", "/v2/workbooks/spec/verify")],
            )
            self.assertTrue(
                (Path(directory) / "workbook-verify-response.json").is_file()
            )

    def test_create_saves_readback_and_checks_workbook_layout(self):
        spec = self._workbook()
        fake = FakePublishClient(spec)
        with tempfile.TemporaryDirectory() as directory:
            result = publish.publish_spec(
                "workbook", spec, fake, directory, create=True
            )
            self.assertTrue(result["verdict"]["pass"])
            self.assertTrue(
                (Path(directory) / "workbook-workbook-1-readback.json").is_file()
            )
            self.assertIn(
                ("GET", "/v2/workbooks/workbook-1/spec?format=json"),
                [(call[0], call[1]) for call in fake.calls],
            )

    def test_workbook_readback_missing_layout_element_fails(self):
        spec = self._workbook()
        readback = json.loads(json.dumps(spec))
        readback["document"]["layout"] = '<Page id="page"></Page>'
        verdict = publish.readback_verdict(spec, readback, "workbook")
        self.assertFalse(verdict["pass"])
        self.assertEqual(verdict["layout"]["missingPlacements"], ["title"])

    def test_complete_document_readback_rejects_material_changes(self):
        spec = self._rich_workbook()
        mutations = {
            "source": lambda doc: doc["elements"][0]["source"].update(
                {"dataModelId": "dm-other"}
            ),
            "column": lambda doc: doc["elements"][0]["columns"].append({
                "id": "tax", "name": "Tax", "formula": "[Orders/Tax]",
            }),
            "formula": lambda doc: doc["elements"][0]["columns"][0].update(
                {"formula": "Sum([Orders/Amount])"}
            ),
            "filter": lambda doc: doc["elements"][0]["filters"][0].update(
                {"formula": "[Amount] >= 0"}
            ),
            "panel": lambda doc: doc["panels"][0]["config"].update(
                {"width": 320}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                readback = json.loads(json.dumps(spec))
                mutate(readback["document"])
                verdict = publish.readback_verdict(
                    spec, readback, "workbook"
                )
                self.assertFalse(verdict["pass"])
                self.assertTrue(verdict["documentDifferences"])

    def test_readback_allows_outer_metadata_and_layout_whitespace(self):
        spec = self._rich_workbook()
        readback = json.loads(json.dumps(spec))
        readback.update({
            "workbookId": "workbook-1",
            "url": "https://app.sigmacomputing.com/workbook-1",
            "documentVersion": 2,
        })
        readback["document"]["layout"] = (
            "\n<Page id=\"page\">\n  "
            "<Element elementId=\"title\" gridColumn=\"1 / 25\" "
            "gridRow=\"1 / 3\"/>\n</Page>\n"
        )
        verdict = publish.readback_verdict(spec, readback, "workbook")
        self.assertTrue(verdict["pass"], verdict)
        self.assertEqual(verdict["documentDifferences"], [])

    def test_verify_requires_literal_true(self):
        spec = self._workbook()
        for response in ({}, {"valid": False}, {"valid": 1}, {"success": True}):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(publish.PublishError):
                    publish.publish_spec(
                        "workbook",
                        spec,
                        FakePublishClient(spec, verify=response),
                        directory,
                    )

    def test_report_create_can_export_pdf(self):
        rep = parse_rdl.parse_rdl(_fixture("PrintLayout2016.rdl"))
        spec, _ = _convert_report({"version": 1, "reports": [rep]})
        fake = FakePublishClient(spec)
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "report.pdf"
            result = publish.publish_spec(
                "report", spec, fake, directory, create=True,
                pdf_out=pdf, sleep=lambda _seconds: None,
            )
            self.assertEqual(pdf.read_bytes(), b"%PDF-1.7\nfixture")
            self.assertEqual(result["pdf"], str(pdf))
            self.assertIn(
                ("GET", "/v2/reports/report-1/spec?format=json"),
                [(call[0], call[1]) for call in fake.calls],
            )

    def test_report_pdf_polling_retries_delayed_readiness(self):
        rep = parse_rdl.parse_rdl(_fixture("PrintLayout2016.rdl"))
        spec, _ = _convert_report({"version": 1, "reports": [rep]})
        fake = FakePublishClient(spec, byte_responses=[
            None,
            b'{"jobComplete": false, "message": "processing"}',
            b"%PDF-1.7\ndelayed",
        ])
        sleeps = []
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "report.pdf"
            publish.publish_spec(
                "report", spec, fake, directory, create=True,
                pdf_out=pdf, sleep=sleeps.append, poll_interval=0.25,
                poll_attempts=3,
            )
            self.assertEqual(pdf.read_bytes(), b"%PDF-1.7\ndelayed")
            self.assertEqual(sleeps, [0.25, 0.25])

    def test_report_pdf_polling_stops_at_attempt_bound(self):
        rep = parse_rdl.parse_rdl(_fixture("PrintLayout2016.rdl"))
        spec, _ = _convert_report({"version": 1, "reports": [rep]})
        fake = FakePublishClient(spec, byte_responses=[None, None])
        sleeps = []
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(publish.PublishError, "timeout"):
                publish.publish_spec(
                    "report", spec, fake, directory, create=True,
                    pdf_out=Path(directory) / "report.pdf",
                    sleep=sleeps.append, poll_interval=0.25,
                    poll_attempts=2,
                )
        self.assertEqual(sleeps, [0.25])

    def test_download_404_is_treated_as_bounded_not_ready(self):
        def not_ready(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 404, "not ready", {}, io.BytesIO(b"")
            )

        client = publish.SigmaClient(env={
            "SIGMA_BASE_URL": "https://api.sigmacomputing.com",
            "SIGMA_API_TOKEN": "token",
        }, opener=not_ready)
        self.assertIsNone(client.request_bytes(
            "GET", "/v2/query/query-1/download"
        ))

    def test_redirects_never_forward_basic_or_bearer_authorization(self):
        handler = publish.RejectRedirectHandler()
        for authorization in ("Basic abc", "Bearer token"):
            with self.subTest(authorization=authorization):
                request = urllib.request.Request(
                    "https://api.sigmacomputing.com/v2/auth/token",
                    headers={"Authorization": authorization},
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    handler.redirect_request(
                        request, io.BytesIO(), 302, "Found", {},
                        "https://attacker.example/steal",
                    )
                self.assertIn("redirect refused", str(raised.exception))

    def test_base_url_rejects_credential_exfiltration_hosts(self):
        with self.assertRaises(publish.PublishError):
            publish.validate_base_url("https://sigma.example.com")
        self.assertEqual(
            publish.validate_base_url("https://aws-api.sigmacomputing.com/"),
            "https://aws-api.sigmacomputing.com",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
