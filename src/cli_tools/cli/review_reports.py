"""Validate, publish, and aggregate scoped implementation review reports."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from itertools import count
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import click

from cli_tools.cli.document_utils import (
    PLAN_DIR,
    extract_frontmatter_scalar,
    parse_frontmatter,
    quote_yaml,
)


REPORT_SCHEMA = "review-report/v2"
SUMMARY_SCHEMA = "review-summary/v1"
REVIEWS_DIR = PLAN_DIR / "reviews"
MAX_REQUIREMENTS_PER_REPORT = 20
# Temporary experiment switch. Set to False to emit actual aggregate verdicts.
FORCE_PASS_SUMMARY = False

FINAL_VERDICTS = (
    "SATISFIED",
    "UNSATISFIED",
    "SPEC_DEFECT",
    "INSUFFICIENT_EVIDENCE",
)
REQUIREMENT_SEVERITIES = ("none", "low", "medium", "high", "blocking")
GENERAL_KINDS = ("IMPLEMENTATION_DEFECT", "RECOMMENDATION")
ANALYSIS_FIELDS = (
    "investigation_performed",
    "implementation_trace",
    "test_and_runtime_evidence",
    "edge_cases_and_adversarial_analysis",
    "defects_or_omissions",
    "verdict_rationale",
)
ANALYSIS_HEADINGS = {
    "investigation_performed": "Investigation performed",
    "implementation_trace": "Implementation trace",
    "test_and_runtime_evidence": "Test and runtime evidence",
    "edge_cases_and_adversarial_analysis": "Edge cases and adversarial analysis",
    "defects_or_omissions": "Defects or omissions",
    "verdict_rationale": "Verdict rationale",
}
GENERAL_FIELDS = ("evidence", "impact", "recommendation")
GENERAL_HEADINGS = {
    "evidence": "Evidence",
    "impact": "Impact",
    "recommendation": "Recommendation",
}

AUTHOR_REPORT_KEYS = {"reviews", "general_findings"}
PUBLISHED_REPORT_KEYS = {
    "schema",
    "design",
    "requirements",
    "scope",
    "requirement_ids",
    "reviews",
    "general_findings",
}
AUTHOR_REVIEW_KEYS = {
    "id",
    "verdict",
    "severity",
    "finding_summary",
    *ANALYSIS_FIELDS,
}
PUBLISHED_REVIEW_KEYS = {
    *AUTHOR_REVIEW_KEYS,
    "title",
    "obligation",
}
GENERAL_FINDING_KEYS = {
    "id",
    "title",
    "kind",
    "summary",
    *GENERAL_FIELDS,
}

REQUIREMENT_HEADING_RE = re.compile(r"^###\s+(R\d+)\.\s+(.+?)\s*$")
SCOPE_RE = re.compile(r"^(R\d+)(?:-(R\d+))?$")
REPORT_FILENAME_RE = re.compile(
    r"^(?P<scope>R\d+(?:-R\d+)?)(?:-(?P<version>[1-9]\d*))?\.json$"
)
SUMMARY_FILENAME_RE = re.compile(r"^summary(?:-(?P<version>[1-9]\d*))?\.md$")


def fail(message: str) -> None:
    click.echo(f"❌ {message}", err=True)
    raise click.exceptions.Exit(1)


@dataclass(frozen=True)
class Requirement:
    identifier: str
    title: str
    obligation: str


@dataclass(frozen=True)
class ReportEntry:
    identifier: str
    title: str
    obligation: str
    verdict: str
    severity: str
    finding_summary: str
    analysis: Dict[str, str]


@dataclass(frozen=True)
class GeneralFinding:
    identifier: str
    title: str
    kind: str
    summary: str
    sections: Dict[str, str]


@dataclass(frozen=True)
class ParsedReport:
    path: Path
    design: Path
    requirements: Path
    scope: str
    assigned_ids: List[str]
    entries: List[ReportEntry]
    general_findings: List[GeneralFinding]


def read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        fail(f"Failed to read {label} '{path}': {error}")
    raise AssertionError("unreachable")


def read_markdown_document(path: Path, label: str) -> Tuple[List[str], List[str]]:
    frontmatter, body = parse_frontmatter(read_text(path, label))
    if frontmatter is None or body is None:
        fail(f"Invalid {label} '{path}': YAML frontmatter not found")
    return frontmatter, body


def resolve_linked_requirements(design: Path) -> Path:
    frontmatter, _ = read_markdown_document(design, "design document")
    linked = extract_frontmatter_scalar(frontmatter, "requirements")
    if not linked:
        fail(f"Design document does not reference a requirements document: {design}")
    requirements = (design.parent / linked).resolve()
    if not requirements.is_file():
        fail(f"Linked requirements document not found: {requirements}")
    return requirements


def parse_requirements(path: Path) -> List[Requirement]:
    _, body = read_markdown_document(path, "requirements document")
    lines = "".join(body).splitlines()
    requirements: List[Requirement] = []
    seen = set()
    index = 0

    while index < len(lines):
        match = REQUIREMENT_HEADING_RE.match(lines[index])
        if not match:
            index += 1
            continue
        identifier, title = match.groups()
        if identifier in seen:
            fail(f"Duplicate requirement identifier in {path}: {identifier}")
        seen.add(identifier)
        obligation_lines: List[str] = []
        index += 1
        while index < len(lines) and not re.match(r"^#{1,3}\s+", lines[index]):
            obligation_lines.append(lines[index])
            index += 1
        obligation = "\n".join(obligation_lines).strip()
        if not obligation:
            fail(f"Requirement {identifier} has no obligation text in {path}")
        requirements.append(Requirement(identifier, title, obligation))

    if not requirements:
        fail(f"No requirements with headings like '### R1. Title' found in {path}")
    return requirements


def load_scope(design_path: str) -> Tuple[Path, Path, List[Requirement]]:
    design = Path(design_path).expanduser().resolve()
    if not design.is_file():
        fail(f"Design document not found: {design}")
    requirements = resolve_linked_requirements(design)
    return design, requirements, parse_requirements(requirements)


def requirement_number(identifier: str) -> int:
    return int(identifier[1:])


def select_scope(scope: str, requirements: Sequence[Requirement]) -> List[Requirement]:
    match = SCOPE_RE.fullmatch(scope.strip())
    if not match:
        fail("Scope must use 'R1-R20' or single-requirement 'R1' syntax")
    start_id, end_id = match.groups()
    end_id = end_id or start_id
    start = requirement_number(start_id)
    end = requirement_number(end_id)
    if end < start:
        fail(f"Scope end precedes its start: {scope}")
    if end - start + 1 > MAX_REQUIREMENTS_PER_REPORT:
        fail(
            f"Scope contains more than {MAX_REQUIREMENTS_PER_REPORT} requirements: {scope}"
        )

    by_id = {item.identifier: item for item in requirements}
    identifiers = [f"R{number}" for number in range(start, end + 1)]
    missing = [identifier for identifier in identifiers if identifier not in by_id]
    if missing:
        fail(f"Scope references unknown requirements: {', '.join(missing)}")
    return [by_id[identifier] for identifier in identifiers]


def scope_label(requirement_ids: Sequence[str]) -> str:
    if len(requirement_ids) == 1:
        return requirement_ids[0]
    return f"{requirement_ids[0]}-{requirement_ids[-1]}"


def review_directory(requirements: Path) -> Path:
    return REVIEWS_DIR / requirements.stem


def report_path(requirements: Path, assigned_ids: Sequence[str], version: int) -> Path:
    suffix = "" if version == 0 else f"-{version}"
    return review_directory(requirements) / f"{scope_label(assigned_ids)}{suffix}.json"


def report_filename_identity(path: Path) -> Optional[Tuple[str, int]]:
    match = REPORT_FILENAME_RE.fullmatch(path.name)
    if match is None:
        return None
    return match.group("scope"), int(match.group("version") or 0)


def summary_filename_version(path: Path) -> Optional[int]:
    match = SUMMARY_FILENAME_RE.fullmatch(path.name)
    if match is None:
        return None
    return int(match.group("version") or 0)


def latest_report_files(directory: Path) -> List[Path]:
    latest_by_scope: Dict[str, Tuple[int, Path]] = {}
    for path in directory.glob("*.json"):
        if not path.is_file():
            continue
        identity = report_filename_identity(path)
        if identity is None:
            continue
        scope, version = identity
        current = latest_by_scope.get(scope)
        if current is None or version > current[0]:
            latest_by_scope[scope] = (version, path)
    return [item[1] for item in latest_by_scope.values()]


def next_report_version(directory: Path, scope: str) -> int:
    versions = [
        identity[1]
        for path in directory.glob("*.json")
        if path.is_file()
        and (identity := report_filename_identity(path)) is not None
        and identity[0] == scope
    ]
    return max(versions, default=-1) + 1


def next_summary_version(directory: Path) -> int:
    versions = [
        version
        for path in directory.glob("summary*.md")
        if path.is_file() and (version := summary_filename_version(path)) is not None
    ]
    return max(versions, default=-1) + 1


def summary_path(directory: Path, version: int) -> Path:
    suffix = "" if version == 0 else f"-{version}"
    return directory / f"summary{suffix}.md"


class DuplicateJsonKeyError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def reject_duplicate_json_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonKeyError(key)
        value[key] = item
    return value


def read_json_object(path: Path, label: str) -> Dict[str, Any]:
    if path.suffix != ".json":
        fail(f"{label.capitalize()} must use a .json filename: {path}")
    try:
        value = json.loads(
            read_text(path, label),
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except DuplicateJsonKeyError as error:
        fail(f"Invalid JSON in {label} '{path}': duplicate key {error.key!r}")
    except json.JSONDecodeError as error:
        fail(f"Invalid JSON in {label} '{path}': {error.msg} at line {error.lineno}")
    if not isinstance(value, dict):
        fail(f"{label.capitalize()} must contain one JSON object: {path}")
    return value


def require_exact_keys(value: Dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    details = []
    if missing:
        details.append(f"missing {', '.join(missing)}")
    if unknown:
        details.append(f"unknown {', '.join(unknown)}")
    fail(f"{label} has invalid fields: {'; '.join(details)}")


def require_string(value: Dict[str, Any], key: str, label: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        fail(f"{label}.{key} must be a string")
    return result


def require_string_list(value: Any, label: str) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        fail(f"{label} must be an array of strings")
    return list(value)


def require_object_list(value: Any, label: str) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        fail(f"{label} must be an array of objects")
    return list(value)


def parse_general_findings(value: Any, path: Path) -> List[GeneralFinding]:
    raw_findings = require_object_list(value, "general_findings")
    findings: List[GeneralFinding] = []
    for index, raw in enumerate(raw_findings, start=1):
        label = f"general_findings[{index - 1}]"
        require_exact_keys(raw, GENERAL_FINDING_KEYS, label)
        identifier = require_string(raw, "id", label)
        if identifier != f"G{index}":
            fail(f"General findings in {path} must be sequential; expected G{index}")
        sections = {field: require_string(raw, field, label) for field in GENERAL_FIELDS}
        finding = GeneralFinding(
            identifier=identifier,
            title=require_string(raw, "title", label),
            kind=require_string(raw, "kind", label),
            summary=require_string(raw, "summary", label),
            sections=sections,
        )
        validate_general_finding(finding, path)
        findings.append(finding)
    return findings


def parse_review_entries(
    value: Any,
    path: Path,
    canonical: Sequence[Requirement],
    published: bool,
) -> List[ReportEntry]:
    raw_reviews = require_object_list(value, "reviews")
    expected_keys = PUBLISHED_REVIEW_KEYS if published else AUTHOR_REVIEW_KEYS
    canonical_by_id = {item.identifier: item for item in canonical}
    entries: List[ReportEntry] = []
    for index, raw in enumerate(raw_reviews):
        label = f"reviews[{index}]"
        require_exact_keys(raw, expected_keys, label)
        identifier = require_string(raw, "id", label)
        canonical_entry = canonical_by_id.get(identifier)
        if canonical_entry is None:
            fail(f"{label}.id references an unassigned requirement: {identifier}")
        if published:
            title = require_string(raw, "title", label)
            obligation = require_string(raw, "obligation", label)
        else:
            title = canonical_entry.title
            obligation = canonical_entry.obligation
        analysis = {field: require_string(raw, field, label) for field in ANALYSIS_FIELDS}
        entries.append(
            ReportEntry(
                identifier=identifier,
                title=title,
                obligation=obligation,
                verdict=require_string(raw, "verdict", label),
                severity=require_string(raw, "severity", label),
                finding_summary=require_string(raw, "finding_summary", label),
                analysis=analysis,
            )
        )
    expected_ids = [item.identifier for item in canonical]
    actual_ids = [entry.identifier for entry in entries]
    if actual_ids != expected_ids:
        fail(f"Reviews must exactly match assigned requirements in order: {expected_ids}")
    return entries


def parse_author_report(
    path: Path,
    payload: Dict[str, Any],
    design: Path,
    requirements: Path,
    assigned: Sequence[Requirement],
) -> ParsedReport:
    require_exact_keys(payload, AUTHOR_REPORT_KEYS, "review report")
    assigned_ids = [item.identifier for item in assigned]
    return ParsedReport(
        path=path,
        design=design,
        requirements=requirements,
        scope=scope_label(assigned_ids),
        assigned_ids=assigned_ids,
        entries=parse_review_entries(payload["reviews"], path, assigned, published=False),
        general_findings=parse_general_findings(payload["general_findings"], path),
    )


def parse_published_report(path_str: str) -> ParsedReport:
    path = Path(path_str).expanduser().resolve()
    payload = read_json_object(path, "published review report")
    require_exact_keys(payload, PUBLISHED_REPORT_KEYS, "published review report")
    schema = require_string(payload, "schema", "published review report")
    if schema != REPORT_SCHEMA:
        fail(f"Unsupported review report schema in {path}: {schema!r}")
    design_value = require_string(payload, "design", "published review report")
    requirements_value = require_string(
        payload, "requirements", "published review report"
    )
    scope = require_string(payload, "scope", "published review report")
    assigned_ids = require_string_list(payload["requirement_ids"], "requirement_ids")
    design = Path(design_value).expanduser().resolve()
    requirements = Path(requirements_value).expanduser().resolve()
    _, linked_requirements, all_requirements = load_scope(str(design))
    if linked_requirements != requirements:
        fail(f"Published report references the wrong requirements document: {path}")
    canonical = select_scope(scope, all_requirements)
    if assigned_ids != [item.identifier for item in canonical]:
        fail(f"Published report requirement_ids do not match scope in {path}")
    report = ParsedReport(
        path=path,
        design=design,
        requirements=requirements,
        scope=scope,
        assigned_ids=assigned_ids,
        entries=parse_review_entries(payload["reviews"], path, canonical, published=True),
        general_findings=parse_general_findings(payload["general_findings"], path),
    )
    validate_report(report, design, requirements, canonical)
    return report


def validate_general_finding(finding: GeneralFinding, path: Path) -> None:
    if not finding.title.strip():
        fail(f"General finding {finding.identifier} in {path} has an empty title")
    if finding.kind not in GENERAL_KINDS:
        fail(f"General finding {finding.identifier} in {path} has invalid kind: {finding.kind}")
    if not 20 <= len(finding.summary.strip()) <= 500:
        fail(f"General finding {finding.identifier} in {path} needs a 20-500 character summary")
    for field, content in finding.sections.items():
        if not content.strip():
            fail(f"General finding {finding.identifier} in {path} has empty {field}")


def validate_completed_entry(entry: ReportEntry, path: Path) -> None:
    if entry.verdict not in FINAL_VERDICTS:
        fail(f"Requirement {entry.identifier} in {path} has invalid verdict: {entry.verdict}")
    if entry.severity not in REQUIREMENT_SEVERITIES:
        fail(f"Requirement {entry.identifier} in {path} has invalid severity: {entry.severity}")
    if not 20 <= len(entry.finding_summary.strip()) <= 500:
        fail(f"Requirement {entry.identifier} in {path} needs a 20-500 character finding_summary")
    for field, content in entry.analysis.items():
        if not content.strip():
            fail(f"Requirement {entry.identifier} in {path} has empty {field}")
    if entry.verdict == "SATISFIED" and entry.severity != "none":
        fail(f"Satisfied requirement {entry.identifier} in {path} must use severity 'none'")
    if entry.verdict == "UNSATISFIED" and entry.severity == "none":
        fail(f"Unsatisfied requirement {entry.identifier} in {path} needs non-none severity")
    if entry.verdict in ("SPEC_DEFECT", "INSUFFICIENT_EVIDENCE") and entry.severity != "blocking":
        fail(f"{entry.verdict} requirement {entry.identifier} in {path} must be blocking")


def validate_report(
    report: ParsedReport,
    design: Path,
    requirements: Path,
    canonical: Sequence[Requirement],
) -> None:
    if report.design != design or report.requirements != requirements:
        fail(f"Report metadata does not match the assigned design pair: {report.path}")
    expected_ids = [item.identifier for item in canonical]
    if report.scope != scope_label(expected_ids) or report.assigned_ids != expected_ids:
        fail(f"Report metadata does not match the assigned scope: {report.path}")
    for entry, requirement in zip(report.entries, canonical):
        if entry.title != requirement.title:
            fail(f"Requirement {entry.identifier} title does not match the canonical document")
        if entry.obligation != requirement.obligation:
            fail(f"Requirement {entry.identifier} obligation does not match the canonical document")
        validate_completed_entry(entry, report.path)


def report_document(report: ParsedReport) -> Dict[str, Any]:
    reviews = []
    for entry in report.entries:
        review: Dict[str, Any] = {
            "id": entry.identifier,
            "title": entry.title,
            "obligation": entry.obligation,
            "verdict": entry.verdict,
            "severity": entry.severity,
            "finding_summary": entry.finding_summary,
        }
        review.update(entry.analysis)
        reviews.append(review)
    general_findings = []
    for finding in report.general_findings:
        value: Dict[str, Any] = {
            "id": finding.identifier,
            "title": finding.title,
            "kind": finding.kind,
            "summary": finding.summary,
        }
        value.update(finding.sections)
        general_findings.append(value)
    return {
        "schema": REPORT_SCHEMA,
        "design": str(report.design),
        "requirements": str(report.requirements),
        "scope": report.scope,
        "requirement_ids": report.assigned_ids,
        "reviews": reviews,
        "general_findings": general_findings,
    }


def json_text(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def publish_text_atomically(
    directory: Path,
    content: str,
    first_version: int,
    destination_for_version: Callable[[int], Path],
) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory,
        prefix=".review-publish-",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        for version in count(first_version):
            destination = destination_for_version(version)
            try:
                os.link(temporary, destination)
                return destination
            except FileExistsError:
                continue
    finally:
        temporary.unlink(missing_ok=True)
    raise AssertionError("unreachable")


def publish_report(report: ParsedReport) -> ParsedReport:
    directory = review_directory(report.requirements)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        first_version = next_report_version(directory, report.scope)
        content = json_text(report_document(report))
        destination = publish_text_atomically(
            directory,
            content,
            first_version,
            lambda version: report_path(
                report.requirements, report.assigned_ids, version
            ),
        )
        return replace(report, path=destination)
    except OSError as error:
        fail(f"Failed to publish review report in '{directory}': {error}")
    raise AssertionError("unreachable")


def validate_published_location(report: ParsedReport) -> None:
    identity = report_filename_identity(report.path)
    expected_directory = review_directory(report.requirements).resolve()
    if report.path.parent != expected_directory or identity is None or identity[0] != report.scope:
        fail(f"Published report path does not match its scope: {report.path}")


def aggregate_verdict(entries: Sequence[ReportEntry], findings: Sequence[GeneralFinding]) -> str:
    if FORCE_PASS_SUMMARY:
        return "PASS"
    if any(entry.verdict == "SPEC_DEFECT" for entry in entries):
        return "SPEC_DEFECT"
    if any(entry.verdict in ("UNSATISFIED", "INSUFFICIENT_EVIDENCE") for entry in entries):
        return "CHANGES_REQUIRED"
    if any(finding.kind == "IMPLEMENTATION_DEFECT" for finding in findings):
        return "CHANGES_REQUIRED"
    return "PASS"


def project_summary_results(
    entries: Sequence[ReportEntry],
    findings: Sequence[Tuple[ParsedReport, GeneralFinding]],
) -> Tuple[List[ReportEntry], List[Tuple[ParsedReport, GeneralFinding]]]:
    if not FORCE_PASS_SUMMARY:
        return list(entries), list(findings)
    passing_entries = [replace(entry, verdict="SATISFIED", severity="none") for entry in entries]
    recommendations = [
        (report, finding)
        for report, finding in findings
        if finding.kind == "RECOMMENDATION"
    ]
    return passing_entries, recommendations


def render_summary(
    design: Path,
    requirements: Path,
    reports: Sequence[ParsedReport],
    entries: Sequence[ReportEntry],
    findings: Sequence[Tuple[ParsedReport, GeneralFinding]],
    verdict: str,
) -> str:
    counts = {value: 0 for value in FINAL_VERDICTS}
    for entry in entries:
        counts[entry.verdict] += 1
    lines = [
        "---\n",
        f"schema: {quote_yaml(SUMMARY_SCHEMA)}\n",
        f"design: {quote_yaml(str(design))}\n",
        f"requirements: {quote_yaml(str(requirements))}\n",
        f"verdict: {verdict}\n",
        f"report_count: {len(reports)}\n",
        f"requirement_count: {len(entries)}\n",
        "---\n",
        "# Implementation Review Summary\n\n",
        f"## Verdict\n\n`{verdict}`\n\n",
        "## Requirement Counts\n\n",
    ]
    for value in FINAL_VERDICTS:
        lines.append(f"- {value}: {counts[value]}\n")
    lines.append("\n## Non-passing Requirements\n\n")
    non_passing = [entry for entry in entries if entry.verdict != "SATISFIED"]
    if not non_passing:
        lines.append("None.\n")
    else:
        source_by_id = {
            entry.identifier: report.path.name
            for report in reports
            for entry in report.entries
        }
        for entry in non_passing:
            lines.extend(
                [
                    f"### {entry.identifier}. {entry.title}\n\n",
                    f"- Verdict: {entry.verdict}\n",
                    f"- Severity: {entry.severity}\n",
                    f"- Finding: {entry.finding_summary}\n",
                    f"- Source: {source_by_id[entry.identifier]}\n\n",
                    "#### Obligation\n\n",
                    f"{entry.obligation}\n\n",
                ]
            )
            for field in ANALYSIS_FIELDS:
                lines.extend(
                    [
                        f"#### {ANALYSIS_HEADINGS[field]}\n\n",
                        f"{entry.analysis[field]}\n\n",
                    ]
                )
    lines.append("## General Findings\n\n")
    if not findings:
        lines.append("None.\n")
    else:
        for report, finding in findings:
            lines.extend(
                [
                    f"### {report.scope}/{finding.identifier}. {finding.title}\n\n",
                    f"- Kind: {finding.kind}\n",
                    f"- Summary: {finding.summary}\n",
                    f"- Source: {report.path.name}\n\n",
                ]
            )
            for field in GENERAL_FIELDS:
                lines.extend(
                    [
                        f"#### {GENERAL_HEADINGS[field]}\n\n",
                        f"{finding.sections[field]}\n\n",
                    ]
                )
    lines.append("## Coverage\n\n")
    for report in reports:
        lines.append(f"- {report.scope}: {report.path.name}\n")
    return "".join(lines)


def publish_summary(directory: Path, content: str) -> Path:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        first_version = next_summary_version(directory)
        return publish_text_atomically(
            directory,
            content,
            first_version,
            lambda version: summary_path(directory, version),
        )
    except OSError as error:
        fail(f"Failed to publish aggregate review summary in '{directory}': {error}")
    raise AssertionError("unreachable")


@click.group(name="review-report")
def review_report_group() -> None:
    """Commands for scoped implementation review reports."""
    pass


@review_report_group.command(name="verify")
@click.argument("report", required=True, type=click.Path())
@click.argument("design", required=True, type=click.Path())
@click.option("--scope", required=True, help="Contiguous requirement scope, e.g. R1-R20")
def review_report_verify_command(report: str, design: str, scope: str) -> None:
    """Validate a reviewer-authored JSON report and publish its canonical form."""
    design_path, requirements_path, all_requirements = load_scope(design)
    assigned = select_scope(scope, all_requirements)
    candidate = Path(report).expanduser().resolve()
    payload = read_json_object(candidate, "review report")

    if set(payload) == PUBLISHED_REPORT_KEYS:
        parsed = parse_published_report(str(candidate))
        validate_report(parsed, design_path, requirements_path, assigned)
        validate_published_location(parsed)
        published = parsed
        newly_published = False
    else:
        parsed = parse_author_report(
            candidate, payload, design_path, requirements_path, assigned
        )
        validate_report(parsed, design_path, requirements_path, assigned)
        published = publish_report(parsed)
        newly_published = True

    click.echo(
        json.dumps(
            {
                "scope": published.scope,
                "report": str(published.path),
                "requirements": len(published.entries),
                "published": newly_published,
                "verified": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


@review_report_group.command(name="summary")
@click.argument("design", required=True, type=click.Path())
def review_report_summary_command(design: str) -> None:
    """Validate latest report versions and publish a versioned aggregate summary."""
    design_path, requirements_path, requirements = load_scope(design)
    directory = review_directory(requirements_path)
    report_files = latest_report_files(directory)
    if not report_files:
        fail(f"No published JSON review reports found in {directory}")
    reports = [parse_published_report(str(path)) for path in report_files]
    for report in reports:
        validate_published_location(report)
        if report.design != design_path or report.requirements != requirements_path:
            fail(f"Report belongs to a different design pair: {report.path}")
    reports.sort(key=lambda report: requirement_number(report.assigned_ids[0]))

    all_entries: List[ReportEntry] = []
    all_findings: List[Tuple[ParsedReport, GeneralFinding]] = []
    owner_by_requirement: Dict[str, Path] = {}
    for report in reports:
        for entry in report.entries:
            previous = owner_by_requirement.get(entry.identifier)
            if previous:
                fail(
                    f"Requirement {entry.identifier} appears in both {previous} and {report.path}"
                )
            owner_by_requirement[entry.identifier] = report.path
            all_entries.append(entry)
        all_findings.extend((report, finding) for finding in report.general_findings)

    expected_ids = [item.identifier for item in requirements]
    expected_set = set(expected_ids)
    found_ids = [entry.identifier for entry in all_entries]
    missing = [identifier for identifier in expected_ids if identifier not in owner_by_requirement]
    unknown = [identifier for identifier in found_ids if identifier not in expected_set]
    if missing:
        fail(f"Review coverage is incomplete; missing: {', '.join(missing)}")
    if unknown:
        fail(f"Review reports contain unknown requirements: {', '.join(unknown)}")
    all_entries.sort(key=lambda entry: requirement_number(entry.identifier))

    findings = [finding for _, finding in all_findings]
    verdict = aggregate_verdict(all_entries, findings)
    summary_entries, summary_findings = project_summary_results(all_entries, all_findings)
    content = render_summary(
        design_path,
        requirements_path,
        reports,
        summary_entries,
        summary_findings,
        verdict,
    )
    published_summary = publish_summary(directory, content)
    counts = {value: 0 for value in FINAL_VERDICTS}
    for entry in summary_entries:
        counts[entry.verdict] += 1
    click.echo(
        json.dumps(
            {
                "verdict": verdict,
                "summary": str(published_summary),
                "counts": counts,
                "general_findings": len(summary_findings),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
