"""Create, verify, and aggregate scoped implementation review reports."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import click

from cli_tools.cli.document_utils import (
    PLAN_DIR,
    extract_frontmatter_scalar,
    extract_yaml_list,
    parse_frontmatter,
    quote_yaml,
)


REPORT_SCHEMA = "review-report/v1"
SUMMARY_SCHEMA = "review-summary/v1"
REVIEWS_DIR = PLAN_DIR / "reviews"
MAX_REQUIREMENTS_PER_REPORT = 20
MINIMUM_SUBSTANTIVE_CHARACTERS = 1500
FINAL_VERDICTS = (
    "SATISFIED",
    "UNSATISFIED",
    "SPEC_DEFECT",
    "INSUFFICIENT_EVIDENCE",
)
ALL_VERDICTS = ("PENDING", *FINAL_VERDICTS)
REQUIREMENT_SEVERITIES = ("pending", "none", "low", "medium", "high", "blocking")
GENERAL_KINDS = ("IMPLEMENTATION_DEFECT", "RECOMMENDATION")
REQUIRED_SECTIONS = (
    "Obligation",
    "Investigation performed",
    "Implementation trace",
    "Test and runtime evidence",
    "Edge cases and adversarial analysis",
    "Defects or omissions",
    "Verdict rationale",
)
ANALYSIS_SECTIONS = REQUIRED_SECTIONS[1:]
GENERAL_SECTIONS = ("Evidence", "Impact", "Recommendation")

REQUIREMENT_HEADING_RE = re.compile(r"^###\s+(R\d+)\.\s+(.+?)\s*$")
SCOPE_RE = re.compile(r"^(R\d+)(?:-(R\d+))?$")
REQUIREMENT_BLOCK_RE = re.compile(
    r"^<!-- REVIEW-REQUIREMENT (?P<id>R\d+) START -->[ \t]*\n"
    r"(?P<body>.*?)"
    r"^<!-- REVIEW-REQUIREMENT (?P=id) END -->[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
REQUIREMENT_START_RE = re.compile(
    r"^<!-- REVIEW-REQUIREMENT (R\d+) START -->[ \t]*$", re.MULTILINE
)
REQUIREMENT_END_RE = re.compile(
    r"^<!-- REVIEW-REQUIREMENT (R\d+) END -->[ \t]*$", re.MULTILINE
)
GENERAL_BLOCK_RE = re.compile(
    r"^<!-- REVIEW-GENERAL-FINDING (?P<id>G\d+) START -->[ \t]*\n"
    r"(?P<body>.*?)"
    r"^<!-- REVIEW-GENERAL-FINDING (?P=id) END -->[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
GENERAL_START_RE = re.compile(
    r"^<!-- REVIEW-GENERAL-FINDING (G\d+) START -->[ \t]*$", re.MULTILINE
)
GENERAL_END_RE = re.compile(
    r"^<!-- REVIEW-GENERAL-FINDING (G\d+) END -->[ \t]*$", re.MULTILINE
)
PLACEHOLDER_RE = re.compile(r"\[Replace this placeholder[^\]]*\]", re.IGNORECASE)


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
    verdict: str
    severity: str
    finding_summary: str
    substantive_characters: int
    sections: Dict[str, str]


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


def report_path(requirements: Path, requirement_ids: Sequence[str]) -> Path:
    return review_directory(requirements) / f"{scope_label(requirement_ids)}.md"


def report_frontmatter(
    design: Path, requirements: Path, assigned: Sequence[Requirement]
) -> str:
    identifiers = [item.identifier for item in assigned]
    lines = [
        "---\n",
        f"schema: {quote_yaml(REPORT_SCHEMA)}\n",
        f"design: {quote_yaml(str(design))}\n",
        f"requirements: {quote_yaml(str(requirements))}\n",
        f"scope: {quote_yaml(scope_label(identifiers))}\n",
        "requirement_ids:\n",
    ]
    lines.extend(f"  - {identifier}\n" for identifier in identifiers)
    lines.append("---\n")
    return "".join(lines)


def requirement_block(requirement: Requirement) -> str:
    return f"""
<!-- REVIEW-REQUIREMENT {requirement.identifier} START -->
## {requirement.identifier}. {requirement.title}

Verdict: PENDING
Severity: pending
Finding summary: PENDING

### Obligation

{requirement.obligation}

### Investigation performed

[Replace this placeholder with exact searches, inspections, checks, and their scope.]

### Implementation trace

[Replace this placeholder with concrete paths, symbols, control flow, and integration evidence.]

### Test and runtime evidence

[Replace this placeholder with inspected assertions, exact executed commands and outcomes, checks not performed, and limitations.]

### Edge cases and adversarial analysis

[Replace this placeholder with the strongest attempted disproof plus relevant failure modes, boundaries, regressions, and security analysis.]

### Defects or omissions

[Replace this placeholder with concrete defects or an evidence-based explanation that none were found.]

### Verdict rationale

[Replace this placeholder with clause-by-clause reasoning that supports the final verdict.]
<!-- REVIEW-REQUIREMENT {requirement.identifier} END -->
"""


def render_report(
    design: Path, requirements: Path, assigned: Sequence[Requirement]
) -> str:
    intro = """
# Implementation Review Report

Complete every assigned requirement section. Preserve all generated metadata, markers, identifiers, titles, headings, and Obligation text exactly. Replace every `PENDING` value before verification.

# Requirement Reviews
"""
    blocks = "".join(requirement_block(item) for item in assigned)
    general = """

# General Findings

Record implementation defects or recommendations discovered while investigating the assigned scope that fall outside an individual requirement's acceptance boundary. Use zero or more blocks in this exact form, numbered `G1`, `G2`, and so on:

```text
<!-- REVIEW-GENERAL-FINDING G1 START -->
## G1. Concise title

Kind: IMPLEMENTATION_DEFECT
Summary: Concise summary of the concrete implementation defect

### Evidence

Concrete evidence.

### Impact

Concrete impact.

### Recommendation

Recommended action.
<!-- REVIEW-GENERAL-FINDING G1 END -->
```
"""
    return report_frontmatter(design, requirements, assigned) + intro + blocks + general


def section_contents(
    body: str, expected: Sequence[str], identifier: str, path: Path
) -> Dict[str, str]:
    matches = list(re.finditer(r"^### (.+?)[ \t]*$", body, re.MULTILINE))
    headings = [match.group(1) for match in matches]
    if headings != list(expected):
        fail(
            f"Entry {identifier} in {path} must contain exactly these sections "
            f"in order: {', '.join(expected)}"
        )

    sections: Dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group(1)] = body[start:end].strip()
    return sections


def substantive_character_count(sections: Dict[str, str]) -> int:
    analysis = "\n".join(sections[name] for name in ANALYSIS_SECTIONS)
    analysis = re.sub(r"```.*?```", "", analysis, flags=re.DOTALL)
    analysis = re.sub(r"<!--.*?-->", "", analysis, flags=re.DOTALL)
    analysis = PLACEHOLDER_RE.sub("", analysis)
    return len(re.sub(r"\s+", " ", analysis).strip())


def unique_line_field(body: str, label: str, identifier: str, path: Path) -> str:
    matches = re.findall(
        rf"^{re.escape(label)}:[ \t]*(.*?)[ \t]*$", body, re.MULTILINE
    )
    if len(matches) != 1 or not matches[0].strip():
        fail(
            f"Entry {identifier} in {path} must contain exactly one "
            f"'{label}: <value>' line"
        )
    return matches[0].strip()


def parse_requirement_entries(body_text: str, path: Path) -> List[ReportEntry]:
    blocks = list(REQUIREMENT_BLOCK_RE.finditer(body_text))
    if len(blocks) != len(REQUIREMENT_START_RE.findall(body_text)) or len(blocks) != len(
        REQUIREMENT_END_RE.findall(body_text)
    ):
        fail(f"Review report contains malformed requirement markers: {path}")

    entries: List[ReportEntry] = []
    for block in blocks:
        identifier = block.group("id")
        block_body = block.group("body")
        title_match = re.search(
            rf"^## {re.escape(identifier)}\.\s+(.+?)$", block_body, re.MULTILINE
        )
        if not title_match:
            fail(f"Requirement {identifier} in {path} has an invalid or missing H2 heading")

        verdict = unique_line_field(block_body, "Verdict", identifier, path)
        severity = unique_line_field(block_body, "Severity", identifier, path)
        finding_summary = unique_line_field(
            block_body, "Finding summary", identifier, path
        )
        if verdict not in ALL_VERDICTS:
            fail(f"Requirement {identifier} in {path} has invalid verdict: {verdict}")
        if severity not in REQUIREMENT_SEVERITIES:
            fail(f"Requirement {identifier} in {path} has invalid severity: {severity}")

        sections = section_contents(
            block_body, REQUIRED_SECTIONS, identifier, path
        )
        entries.append(
            ReportEntry(
                identifier=identifier,
                title=title_match.group(1).strip(),
                verdict=verdict,
                severity=severity,
                finding_summary=finding_summary,
                substantive_characters=substantive_character_count(sections),
                sections=sections,
            )
        )
    return entries


def parse_general_findings(body_text: str, path: Path) -> List[GeneralFinding]:
    scan_text = re.sub(r"```.*?```", "", body_text, flags=re.DOTALL)
    blocks = list(GENERAL_BLOCK_RE.finditer(scan_text))
    if len(blocks) != len(GENERAL_START_RE.findall(scan_text)) or len(blocks) != len(
        GENERAL_END_RE.findall(scan_text)
    ):
        fail(f"Review report contains malformed general-finding markers: {path}")

    findings: List[GeneralFinding] = []
    for index, block in enumerate(blocks, start=1):
        identifier = block.group("id")
        if identifier != f"G{index}":
            fail(
                f"General findings in {path} must be sequential; expected G{index}, "
                f"found {identifier}"
            )
        block_body = block.group("body")
        title_match = re.search(
            rf"^## {re.escape(identifier)}\.\s+(.+?)$", block_body, re.MULTILINE
        )
        if not title_match:
            fail(f"General finding {identifier} in {path} has an invalid H2 heading")

        kind = unique_line_field(block_body, "Kind", identifier, path)
        summary = unique_line_field(block_body, "Summary", identifier, path)
        if kind not in GENERAL_KINDS:
            fail(f"General finding {identifier} in {path} has invalid kind: {kind}")
        if len(summary) > 500:
            fail(f"General finding {identifier} summary exceeds 500 characters in {path}")

        first_section = re.search(r"^### ", block_body, re.MULTILINE)
        if first_section is None:
            fail(f"General finding {identifier} in {path} has no structured sections")
        preamble_lines = [
            line.strip()
            for line in block_body[: first_section.start()].splitlines()
            if line.strip() and not line.startswith("## ")
        ]
        if preamble_lines != [f"Kind: {kind}", f"Summary: {summary}"]:
            fail(
                f"General finding {identifier} in {path} must contain exactly Kind and "
                "Summary fields before its sections"
            )

        sections = section_contents(block_body, GENERAL_SECTIONS, identifier, path)
        for section, content in sections.items():
            if not content or PLACEHOLDER_RE.search(content):
                fail(
                    f"General finding {identifier} in {path} has incomplete {section}"
                )
        findings.append(
            GeneralFinding(
                identifier=identifier,
                title=title_match.group(1).strip(),
                kind=kind,
                summary=summary,
                sections=sections,
            )
        )
    return findings


def parse_report(path_str: str) -> ParsedReport:
    path = Path(path_str).expanduser().resolve()
    frontmatter, body_lines = read_markdown_document(path, "review report")
    schema = extract_frontmatter_scalar(frontmatter, "schema")
    if schema != REPORT_SCHEMA:
        fail(f"Unsupported or missing review report schema in {path}: {schema!r}")

    design_value = extract_frontmatter_scalar(frontmatter, "design")
    requirements_value = extract_frontmatter_scalar(frontmatter, "requirements")
    scope = extract_frontmatter_scalar(frontmatter, "scope")
    if not design_value or not requirements_value or not scope:
        fail(f"Review report metadata is incomplete: {path}")
    assigned_ids = extract_yaml_list(frontmatter, "requirement_ids")
    if not assigned_ids or len(assigned_ids) > MAX_REQUIREMENTS_PER_REPORT:
        fail(
            f"Review report must assign 1-{MAX_REQUIREMENTS_PER_REPORT} requirements: {path}"
        )
    if len(set(assigned_ids)) != len(assigned_ids):
        fail(f"Review report contains duplicate requirement_ids: {path}")
    if scope != scope_label(assigned_ids):
        fail(
            f"Review report scope metadata does not match requirement_ids in {path}: "
            f"{scope} != {scope_label(assigned_ids)}"
        )

    body_text = "".join(body_lines)
    entries = parse_requirement_entries(body_text, path)
    entry_ids = [entry.identifier for entry in entries]
    if entry_ids != assigned_ids:
        fail(
            f"Requirement blocks in {path} must exactly match requirement_ids in order; "
            f"expected {assigned_ids}, found {entry_ids}"
        )

    return ParsedReport(
        path=path,
        design=Path(design_value).expanduser().resolve(),
        requirements=Path(requirements_value).expanduser().resolve(),
        scope=scope,
        assigned_ids=assigned_ids,
        entries=entries,
        general_findings=parse_general_findings(body_text, path),
    )


def validate_report_scope(report: ParsedReport) -> None:
    design, requirements, all_requirements = load_scope(str(report.design))
    if design != report.design or requirements != report.requirements:
        fail(f"Report document metadata does not match the linked design pair: {report.path}")
    by_id = {item.identifier: item for item in all_requirements}
    unknown = [identifier for identifier in report.assigned_ids if identifier not in by_id]
    if unknown:
        fail(f"Report contains unknown requirements: {', '.join(unknown)}")
    expected = [
        f"R{number}"
        for number in range(
            requirement_number(report.assigned_ids[0]),
            requirement_number(report.assigned_ids[-1]) + 1,
        )
    ]
    if report.assigned_ids != expected:
        fail(f"Report scope is not contiguous in {report.path}: {report.assigned_ids}")
    for entry in report.entries:
        canonical = by_id[entry.identifier]
        if entry.title != canonical.title:
            fail(
                f"Requirement {entry.identifier} title in {report.path} does not match "
                "the canonical requirements document"
            )
        if entry.sections["Obligation"] != canonical.obligation:
            fail(
                f"Requirement {entry.identifier} Obligation in {report.path} does not "
                "match the canonical requirements document"
            )


def validate_completed_entry(entry: ReportEntry, path: Path, minimum_chars: int) -> None:
    if entry.verdict not in FINAL_VERDICTS:
        fail(f"Requirement {entry.identifier} in {path} still has verdict {entry.verdict}")
    if entry.finding_summary == "PENDING" or not 20 <= len(entry.finding_summary) <= 500:
        fail(
            f"Requirement {entry.identifier} in {path} must have a 20-500 character "
            "Finding summary"
        )
    if entry.substantive_characters < minimum_chars:
        fail(
            f"Requirement {entry.identifier} in {path} has "
            f"{entry.substantive_characters} substantive characters; minimum is {minimum_chars}"
        )
    if entry.verdict == "SATISFIED" and entry.severity != "none":
        fail(f"Satisfied requirement {entry.identifier} in {path} must use severity 'none'")
    if entry.verdict == "UNSATISFIED" and entry.severity in ("none", "pending"):
        fail(
            f"Unsatisfied requirement {entry.identifier} in {path} must use a non-none severity"
        )
    if entry.verdict in ("SPEC_DEFECT", "INSUFFICIENT_EVIDENCE") and entry.severity != "blocking":
        fail(
            f"{entry.verdict} requirement {entry.identifier} in {path} must use "
            "severity 'blocking'"
        )


def verify_report(report: ParsedReport, minimum_chars: int) -> None:
    validate_report_scope(report)
    for entry in report.entries:
        validate_completed_entry(entry, report.path, minimum_chars)


def aggregate_verdict(
    entries: Sequence[ReportEntry], findings: Sequence[GeneralFinding]
) -> str:
    if any(entry.verdict == "SPEC_DEFECT" for entry in entries):
        return "SPEC_DEFECT"
    if any(
        entry.verdict in ("UNSATISFIED", "INSUFFICIENT_EVIDENCE")
        for entry in entries
    ) or any(finding.kind == "IMPLEMENTATION_DEFECT" for finding in findings):
        return "CHANGES_REQUIRED"
    return "PASS"


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

    lines.extend(["\n## Non-passing Requirements\n\n"])
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
                ]
            )
            for section in REQUIRED_SECTIONS:
                lines.extend(
                    [
                        f"#### {section}\n\n",
                        f"{entry.sections[section]}\n\n",
                    ]
                )

    lines.extend(["## General Findings\n\n"])
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
            for section in GENERAL_SECTIONS:
                lines.extend(
                    [
                        f"#### {section}\n\n",
                        f"{finding.sections[section]}\n\n",
                    ]
                )

    lines.extend(["## Coverage\n\n"])
    for report in reports:
        lines.append(f"- {report.scope}: {report.path.name}\n")
    return "".join(lines)


@click.group(name="review-report")
def review_report_group() -> None:
    """Commands for scoped implementation review reports."""
    pass


@review_report_group.command(name="create")
@click.argument("design", required=True, type=click.Path())
@click.option("--scope", required=True, help="Contiguous requirement scope, e.g. R1-R20")
def review_report_create_command(design: str, scope: str) -> None:
    """Create or reset one structured report for a requirement scope."""
    design_path, requirements_path, requirements = load_scope(design)
    assigned = select_scope(scope, requirements)
    identifiers = [item.identifier for item in assigned]
    destination = report_path(requirements_path, identifiers)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            render_report(design_path, requirements_path, assigned), encoding="utf-8"
        )
    except OSError as error:
        fail(f"Failed to create review report '{destination}': {error}")
    click.echo(str(destination))


@review_report_group.command(name="verify")
@click.argument("report", required=True, type=click.Path())
@click.option(
    "--minimum-chars",
    default=MINIMUM_SUBSTANTIVE_CHARACTERS,
    show_default=True,
    type=click.IntRange(min=1),
    help="Minimum substantive characters per requirement",
)
def review_report_verify_command(report: str, minimum_chars: int) -> None:
    """Verify one report's scope, formatting, findings, and analysis size."""
    parsed = parse_report(report)
    verify_report(parsed, minimum_chars)
    click.echo(
        f"✅ Review report verified: {parsed.path} "
        f"({len(parsed.entries)} requirements, minimum {minimum_chars} characters each)"
    )


@review_report_group.command(name="summary")
@click.argument("design", required=True, type=click.Path())
@click.option(
    "--minimum-chars",
    default=MINIMUM_SUBSTANTIVE_CHARACTERS,
    show_default=True,
    type=click.IntRange(min=1),
    help="Minimum substantive characters per requirement",
)
def review_report_summary_command(design: str, minimum_chars: int) -> None:
    """Verify complete coverage and write one aggregate findings summary."""
    design_path, requirements_path, requirements = load_scope(design)
    directory = review_directory(requirements_path)
    report_files = sorted(
        path for path in directory.glob("*.md") if path.name != "summary.md"
    )
    if not report_files:
        fail(f"No review reports found in {directory}")

    reports = [parse_report(str(path)) for path in report_files]
    reports.sort(key=lambda report: requirement_number(report.assigned_ids[0]))
    all_entries: List[ReportEntry] = []
    all_findings: List[Tuple[ParsedReport, GeneralFinding]] = []
    owner_by_requirement: Dict[str, Path] = {}

    for report in reports:
        verify_report(report, minimum_chars)
        if report.design != design_path or report.requirements != requirements_path:
            fail(f"Report belongs to a different design pair: {report.path}")
        for entry in report.entries:
            previous = owner_by_requirement.get(entry.identifier)
            if previous:
                fail(
                    f"Requirement {entry.identifier} appears in both {previous} and "
                    f"{report.path}"
                )
            owner_by_requirement[entry.identifier] = report.path
            all_entries.append(entry)
        all_findings.extend((report, finding) for finding in report.general_findings)

    expected_ids = [item.identifier for item in requirements]
    found_ids = [entry.identifier for entry in all_entries]
    missing = [identifier for identifier in expected_ids if identifier not in owner_by_requirement]
    unknown = [identifier for identifier in found_ids if identifier not in set(expected_ids)]
    if missing:
        fail(f"Review coverage is incomplete; missing: {', '.join(missing)}")
    if unknown:
        fail(f"Review reports contain unknown requirements: {', '.join(unknown)}")
    all_entries.sort(key=lambda entry: requirement_number(entry.identifier))

    findings = [finding for _, finding in all_findings]
    verdict = aggregate_verdict(all_entries, findings)
    summary_path = directory / "summary.md"
    try:
        summary_path.write_text(
            render_summary(
                design_path,
                requirements_path,
                reports,
                all_entries,
                all_findings,
                verdict,
            ),
            encoding="utf-8",
        )
    except OSError as error:
        fail(f"Failed to write aggregate review summary '{summary_path}': {error}")

    counts = {value: 0 for value in FINAL_VERDICTS}
    for entry in all_entries:
        counts[entry.verdict] += 1
    click.echo(
        json.dumps(
            {
                "verdict": verdict,
                "summary": str(summary_path),
                "counts": counts,
                "general_findings": len(all_findings),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
