#!/usr/bin/env python3
"""Verify, or explicitly create and read back, a Sigma workbook/report spec.

The default operation is the non-persistent ``/spec/verify`` call. Pass
``--create`` to opt into a persistent POST. Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import code_rep  # noqa: E402


class PublishError(RuntimeError):
    pass


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward Basic or bearer credentials through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"redirect refused ({req.full_url} -> {newurl})",
            headers,
            fp,
        )


def load_neutral_env(env=None, path=None):
    """Fill missing Sigma variables from the agent-neutral env file."""
    result = dict(os.environ if env is None else env)
    env_path = Path(path or Path.home() / ".sigma-migration" / "env")
    if not env_path.is_file():
        return result
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"SIGMA_[A-Z0-9_]+", key) or key in result:
            continue
        try:
            values = shlex.split(raw_value, posix=True)
        except ValueError as exc:
            raise PublishError(f"{env_path}: invalid shell quoting for {key}") from exc
        if len(values) != 1:
            raise PublishError(f"{env_path}: {key} must contain one literal value")
        result[key] = values[0]
    return result


def validate_base_url(value, allow_insecure=False):
    """Validate and normalize a Sigma API origin before sending credentials."""
    if not value:
        raise PublishError("Set SIGMA_BASE_URL")
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PublishError("SIGMA_BASE_URL must be an API origin without credentials/query")
    if parsed.path not in ("", "/"):
        raise PublishError("SIGMA_BASE_URL must not include an API path")
    trusted = host == "sigmacomputing.com" or host.endswith(".sigmacomputing.com")
    if not allow_insecure and (parsed.scheme != "https" or not trusted):
        raise PublishError(
            "SIGMA_BASE_URL must use https:// on a sigmacomputing.com host; "
            "set SIGMA_ALLOW_INSECURE_BASE_URL=1 only for controlled dev/test"
        )
    if not host or not parsed.scheme:
        raise PublishError("SIGMA_BASE_URL must be an absolute URL")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "", "", "")
    ).rstrip("/")


class SigmaClient:
    def __init__(self, env=None, opener=None):
        self.env = load_neutral_env(env)
        self.base_url = validate_base_url(
            self.env.get("SIGMA_BASE_URL"),
            self.env.get("SIGMA_ALLOW_INSECURE_BASE_URL") == "1",
        )
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            RejectRedirectHandler(),
        )
        self.token = self.env.get("SIGMA_API_TOKEN")
        if not self.token:
            self.token = self._mint_token()

    def _open(self, request, retry_statuses=()):
        open_request = (
            self.opener.open
            if hasattr(self.opener, "open")
            else self.opener
        )
        try:
            return open_request(request, timeout=60)
        except TypeError:
            # Small fake openers used by tests need not mirror urlopen's kwargs.
            return open_request(request)
        except urllib.error.HTTPError as exc:
            if exc.code in retry_statuses:
                exc.close()
                return None
            detail = exc.read().decode("utf-8", "replace")
            raise PublishError(
                f"{request.method} {request.full_url} returned HTTP {exc.code}: "
                f"{detail[:1000]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise PublishError(
                f"{request.method} {request.full_url} failed: {exc.reason}"
            ) from exc

    def _mint_token(self):
        client_id = self.env.get("SIGMA_CLIENT_ID")
        secret = self.env.get("SIGMA_CLIENT_SECRET")
        if not client_id or not secret:
            raise PublishError(
                "Set SIGMA_API_TOKEN or SIGMA_CLIENT_ID and SIGMA_CLIENT_SECRET"
            )
        if client_id == secret:
            raise PublishError(
                "SIGMA_CLIENT_SECRET is identical to SIGMA_CLIENT_ID"
            )
        basic = base64.b64encode(
            f"{client_id}:{secret}".encode("utf-8")
        ).decode("ascii")
        request = urllib.request.Request(
            self.base_url + "/v2/auth/token",
            data=urllib.parse.urlencode(
                {"grant_type": "client_credentials"}
            ).encode("ascii"),
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        with self._open(request) as response:
            try:
                payload = json.loads(response.read())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise PublishError("token response was not valid JSON") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise PublishError("token response did not contain access_token")
        return token

    def _request(self, method, path, body=None, accept="application/json",
                 retry_statuses=()):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method.upper(), headers=headers
        )
        return self._open(request, retry_statuses=retry_statuses)

    def request_json(self, method, path, body=None):
        with self._request(method, path, body) as response:
            raw = response.read()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PublishError(f"{method} {path} returned non-JSON content") from exc
        if not isinstance(value, dict):
            raise PublishError(f"{method} {path} returned a non-object JSON value")
        return value

    def request_bytes(self, method, path, body=None, accept="*/*"):
        response = self._request(
            method, path, body, accept, retry_statuses=(404,)
        )
        if response is None:
            return None
        with response:
            if getattr(response, "status", 200) == 204:
                return None
            return response.read()


def load_spec(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"{path}: cannot load JSON spec: {exc}") from exc
    if not isinstance(value, dict):
        raise PublishError(f"{path}: spec must be a JSON object")
    return value


def detect_kind(spec, requested="auto"):
    document = spec.get("document")
    kind = document.get("kind") if isinstance(document, dict) else None
    if requested != "auto":
        if kind and kind != requested:
            raise PublishError(
                f"--type {requested} conflicts with document.kind {kind!r}"
            )
        return requested
    if kind not in ("workbook", "report"):
        raise PublishError("document.kind must be workbook or report")
    return kind


def _document(spec, kind):
    return code_rep.document(spec) if kind == "workbook" else (
        spec.get("document") or {}
    )


def _id_set(items):
    return {
        item.get("id") for item in (items or [])
        if isinstance(item, dict) and item.get("id")
    }


def _layout_placements(layout):
    fragment = re.sub(r"^\s*<\?xml[^>]*\?>", "", str(layout or ""), count=1)
    if not fragment.strip():
        return None, "layout is empty", {}
    try:
        root = ET.fromstring(f"<Layout>{fragment}</Layout>")
    except ET.ParseError as exc:
        return None, f"layout XML is not parseable: {exc}", {}
    placed = []
    signatures = {}
    for layout_root in root:
        root_key = (layout_root.tag, layout_root.get("id"))
        for node in layout_root.iter():
            element_id = node.get("elementId")
            if not element_id:
                continue
            placed.append(element_id)
            attrs = []
            for key, value in node.attrib.items():
                if key == "elementId":
                    continue
                if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value):
                    value = float(value)
                attrs.append((key, value))
            signatures.setdefault(element_id, []).append(
                (root_key, node.tag, tuple(sorted(attrs)))
            )
    return placed, None, signatures


def _canonical_layout(layout):
    """Normalize XML syntax/whitespace while preserving layout semantics."""
    fragment = re.sub(r"^\s*<\?xml[^>]*\?>", "", str(layout or ""), count=1)
    try:
        root = ET.fromstring(f"<Layout>{fragment}</Layout>")
    except ET.ParseError:
        return str(layout or "")

    def node_value(node):
        text = (node.text or "").strip()
        return {
            "tag": node.tag,
            "attributes": sorted(node.attrib.items()),
            "text": text or None,
            "children": [node_value(child) for child in node],
        }

    return [node_value(child) for child in root]


def _normalize_document_value(value, key=None):
    if key == "layout":
        return _canonical_layout(value)
    if isinstance(value, dict):
        return {
            item_key: _normalize_document_value(item, item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_document_value(item) for item in value]
    return value


def _document_differences(expected, actual, path="$document", limit=100):
    """Return material deep differences without dumping sensitive values."""
    differences = []

    def visit(left, right, current):
        if len(differences) >= limit:
            return
        if type(left) is not type(right):
            differences.append({"path": current, "kind": "type_changed"})
            return
        if isinstance(left, dict):
            for key in sorted(set(left) - set(right)):
                differences.append({
                    "path": f"{current}.{key}", "kind": "missing_in_readback",
                })
            for key in sorted(set(right) - set(left)):
                differences.append({
                    "path": f"{current}.{key}", "kind": "added_in_readback",
                })
            for key in sorted(set(left) & set(right)):
                visit(left[key], right[key], f"{current}.{key}")
            return
        if isinstance(left, list):
            if len(left) != len(right):
                differences.append({
                    "path": current, "kind": "list_length_changed",
                })
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                visit(left_item, right_item, f"{current}[{index}]")
            return
        if left != right:
            differences.append({"path": current, "kind": "value_changed"})

    visit(expected, actual, path)
    if len(differences) >= limit:
        differences.append({
            "path": path,
            "kind": f"truncated_after_{limit}_differences",
        })
    return differences


def readback_verdict(submitted, readback, kind):
    expected = _document(submitted, kind)
    actual = _document(readback, kind)
    normalized_expected = _normalize_document_value(expected)
    normalized_actual = _normalize_document_value(actual)
    document_differences = _document_differences(
        normalized_expected, normalized_actual
    )
    missing = {}
    for field in ("elements", "pages", "panels"):
        expected_ids = _id_set(expected.get(field))
        actual_ids = _id_set(actual.get(field))
        values = sorted(expected_ids - actual_ids)
        if values:
            missing[field] = values

    expected_placements, expected_error, expected_signatures = _layout_placements(
        expected.get("layout")
    )
    actual_placements, actual_error, actual_signatures = _layout_placements(
        actual.get("layout")
    )
    layout = {
        "parseError": actual_error,
        "missingPlacements": [],
        "duplicatePlacements": [],
        "changedPlacements": [],
    }
    if expected_error:
        layout["submittedParseError"] = expected_error
    if expected_placements is not None and actual_placements is not None:
        layout["missingPlacements"] = sorted(
            set(expected_placements) - set(actual_placements)
        )
        layout["duplicatePlacements"] = sorted({
            item for item in actual_placements
            if actual_placements.count(item) > 1
        })
        layout["changedPlacements"] = sorted(
            element_id
            for element_id in set(expected_signatures) & set(actual_signatures)
            if expected_signatures[element_id] != actual_signatures[element_id]
        )

    error_columns = []
    for element in actual.get("elements") or []:
        if not isinstance(element, dict):
            continue
        for column in element.get("columns") or []:
            if column.get("type") == "error" or column.get("error"):
                error_columns.append({
                    "element": element.get("id") or element.get("name"),
                    "column": column.get("id") or column.get("name"),
                    "error": column.get("error") or column.get("message"),
                })
    passed = not (
        missing
        or layout["parseError"]
        or layout["missingPlacements"]
        or layout["duplicatePlacements"]
        or layout["changedPlacements"]
        or document_differences
        or error_columns
    )
    result = {
        "pass": passed,
        "missingIds": missing,
        "layout": layout,
        "documentDifferences": document_differences,
        "errorColumns": error_columns,
    }
    return result


def _write_json(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _pdf_response_not_ready(payload):
    if payload is None or not payload:
        return True
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    if value.get("jobComplete") is False:
        return True
    status = str(value.get("status") or "").lower()
    if status in {"pending", "queued", "running", "processing", "not_ready"}:
        return True
    message = str(value.get("message") or "").lower()
    return any(word in message for word in ("not ready", "processing", "pending"))


def publish_spec(kind, spec, client, out_dir, create=False, pdf_out=None,
                 pdf_layout=None, poll_interval=2.0, poll_attempts=30,
                 sleep=time.sleep):
    """Run verify and optionally create/read back/export one representation."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if pdf_out is not None and kind != "report":
        raise PublishError("--pdf-out is supported only for reports")
    if pdf_out is not None and (poll_attempts < 1 or poll_interval < 0):
        raise PublishError(
            "PDF polling requires --poll-attempts >= 1 and "
            "--poll-interval >= 0"
        )
    base = f"/v2/{kind}s"
    verify = client.request_json("POST", f"{base}/spec/verify", spec)
    _write_json(out_dir / f"{kind}-verify-response.json", verify)
    result = {"kind": kind, "verify": verify, "created": False}
    if verify.get("valid") is not True:
        raise PublishError(
            f"{kind} server verification did not return valid=true"
        )
    if not create:
        return result

    response = client.request_json("POST", f"{base}/spec", spec)
    _write_json(out_dir / f"{kind}-create-response.json", response)
    id_field = f"{kind}Id"
    object_id = response.get(id_field) or response.get("id")
    if not object_id:
        raise PublishError(f"{kind} create response contained no {id_field}")
    _write_json(out_dir / f"{kind}-{object_id}-submitted.json", spec)
    readback = client.request_json(
        "GET", f"{base}/{object_id}/spec?format=json"
    )
    _write_json(out_dir / f"{kind}-{object_id}-readback.json", readback)
    verdict = readback_verdict(spec, readback, kind)
    _write_json(out_dir / f"{kind}-{object_id}-readback-verdict.json", verdict)
    result.update({
        "created": True,
        "id": object_id,
        "response": response,
        "readback": readback,
        "verdict": verdict,
    })
    if not verdict["pass"]:
        return result

    if pdf_out is not None:
        document = _document(spec, kind)
        config = document.get("config") or {}
        orientation = pdf_layout or (
            "landscape"
            if config.get("pageWidth", 0) > config.get("pageHeight", 0)
            else "portrait"
        )
        export_response = client.request_json(
            "POST",
            f"/v2/reports/{object_id}/export",
            {"format": {"type": "pdf", "layout": orientation}},
        )
        _write_json(
            out_dir / f"report-{object_id}-pdf-export-response.json",
            export_response,
        )
        query_id = export_response.get("queryId")
        if not query_id:
            raise PublishError("report PDF export response contained no queryId")
        pdf = None
        for attempt in range(poll_attempts):
            candidate = client.request_bytes(
                "GET", f"/v2/query/{query_id}/download",
                accept="application/pdf",
            )
            if candidate and candidate.startswith(b"%PDF-"):
                pdf = candidate
                break
            if not _pdf_response_not_ready(candidate):
                raise PublishError("report export download was not a PDF")
            if attempt + 1 < poll_attempts:
                sleep(poll_interval)
        if pdf is None:
            raise PublishError("report PDF export did not complete before timeout")
        Path(pdf_out).write_bytes(pdf)
        result["pdf"] = str(pdf_out)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--type", choices=("auto", "workbook", "report"),
                        default="auto")
    parser.add_argument(
        "--create", action="store_true",
        help="perform the persistent create POST after successful verify",
    )
    parser.add_argument("--out-dir", default="publish-output")
    parser.add_argument(
        "--pdf-out",
        help="after --create of a report, export and save its PDF",
    )
    parser.add_argument("--pdf-layout", choices=("portrait", "landscape"))
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--poll-attempts", type=int, default=30)
    args = parser.parse_args()
    try:
        spec = load_spec(args.spec)
        kind = detect_kind(spec, args.type)
        if args.pdf_out and not args.create:
            raise PublishError("--pdf-out requires --create")
        result = publish_spec(
            kind, spec, SigmaClient(), args.out_dir,
            create=args.create,
            pdf_out=args.pdf_out,
            pdf_layout=args.pdf_layout,
            poll_interval=args.poll_interval,
            poll_attempts=args.poll_attempts,
        )
    except (OSError, PublishError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    if result["created"]:
        verdict = "PASS" if result["verdict"]["pass"] else "FAIL"
        print(f"{verdict}: created {kind} {result['id']} and saved readback")
        return 0 if result["verdict"]["pass"] else 2
    print(f"PASS: {kind} verified without creating a persistent resource")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
