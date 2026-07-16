import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli_tools.cli import review_reports


class ReviewReportsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.design = self.root / "design.md"
        self.requirements = self.root / "requirements.md"
        self.reviews_dir = self.root / "reviews"
        self.runner = CliRunner()

        self.design.write_text(
            "---\n"
            'title: "Test design"\n'
            'requirements: "requirements.md"\n'
            "---\n"
            "# Test design\n",
            encoding="utf-8",
        )
        requirements_body = [
            "---\n",
            'title: "Test requirements"\n',
            'design: "design.md"\n',
            "---\n",
            "# Test requirements\n\n",
        ]
        for index in range(1, 26):
            requirements_body.extend(
                [
                    f"### R{index}. Requirement {index}\n\n",
                    f"The implementation must satisfy obligation {index}.\n\n",
                ]
            )
        self.requirements.write_text("".join(requirements_body), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def invoke(self, arguments):
        with patch.object(review_reports, "REVIEWS_DIR", self.reviews_dir):
            return self.runner.invoke(review_reports.review_report_group, arguments)

    def create_report(self, scope: str) -> Path:
        result = self.invoke(["create", str(self.design), "--scope", scope])
        self.assertEqual(result.exit_code, 0, result.output)
        return Path(result.output.strip())

    def complete_report(self, report: Path, verdict: str = "SATISFIED") -> None:
        content = report.read_text(encoding="utf-8")
        content = content.replace("Verdict: PENDING", f"Verdict: {verdict}")
        severity = "none" if verdict == "SATISFIED" else "high"
        if verdict in ("SPEC_DEFECT", "INSUFFICIENT_EVIDENCE"):
            severity = "blocking"
        content = content.replace("Severity: pending", f"Severity: {severity}")
        content = content.replace(
            "Finding summary: PENDING",
            "Finding summary: Concrete implementation evidence supports this verdict.",
        )
        content = re.sub(
            r"\[Replace this placeholder[^\]]*\]",
            "Inspected implementation and tests. Concrete evidence supports this conclusion.",
            content,
        )
        report.write_text(content, encoding="utf-8")

    def add_general_finding(
        self,
        report: Path,
        kind: str = "IMPLEMENTATION_DEFECT",
    ) -> None:
        block = f"""

<!-- REVIEW-GENERAL-FINDING G1 START -->
## G1. Shared implementation concern

Kind: {kind}
Summary: The implementation uses a fragile pattern that should be surfaced.

### Evidence

The relevant code contains concrete duplicated control flow.

### Impact

Future changes may diverge and introduce inconsistent behavior.

### Recommendation

Consolidate the shared behavior behind one maintained abstraction.
<!-- REVIEW-GENERAL-FINDING G1 END -->
"""
        with report.open("a", encoding="utf-8") as stream:
            stream.write(block)

    def test_group_exposes_only_create_verify_and_summary(self) -> None:
        result = self.invoke(["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("create", result.output)
        self.assertIn("verify", result.output)
        self.assertIn("summary", result.output)
        self.assertNotIn("plan", result.output)
        self.assertNotIn("extract", result.output)

    def test_create_scaffolds_targeted_scope_and_resets_stable_path(self) -> None:
        report = self.create_report("R1-R20")
        content = report.read_text(encoding="utf-8")

        self.assertEqual(report.name, "R1-R20.md")
        self.assertIn('schema: "review-report/v1"', content)
        self.assertIn('scope: "R1-R20"', content)
        self.assertIn("  - R1", content)
        self.assertIn("  - R20", content)
        self.assertNotIn("  - R21\n", content)
        self.assertEqual(content.count("<!-- REVIEW-REQUIREMENT R"), 40)

        report.write_text("existing report content", encoding="utf-8")
        recreated = self.create_report("R1-R20")
        self.assertEqual(recreated, report)
        self.assertIn("Verdict: PENDING", report.read_text(encoding="utf-8"))

    def test_create_rejects_more_than_twenty_requirements(self) -> None:
        result = self.invoke(
            ["create", str(self.design), "--scope", "R1-R21"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("more than 20", result.output)

    def test_verify_rejects_pending_and_short_reports(self) -> None:
        report = self.create_report("R21-R25")
        pending = self.invoke(
            ["verify", str(report), "--minimum-chars", "1"]
        )
        self.assertEqual(pending.exit_code, 1, pending.output)
        self.assertIn("still has verdict PENDING", pending.output)

        self.complete_report(report)
        short = self.invoke(["verify", str(report)])
        self.assertEqual(short.exit_code, 1, short.output)
        self.assertIn("minimum is 1500", short.output)

    def test_verify_accepts_structured_general_findings(self) -> None:
        report = self.create_report("R21-R25")
        self.complete_report(report)
        self.add_general_finding(report)

        result = self.invoke(
            ["verify", str(report), "--minimum-chars", "20"]
        )

        self.assertEqual(result.exit_code, 0, result.output)

    def test_verify_rejects_modified_requirement_title(self) -> None:
        report = self.create_report("R21-R25")
        self.complete_report(report)
        content = report.read_text(encoding="utf-8")
        content = content.replace(
            "## R21. Requirement 21",
            "## R21. Reviewer conclusion",
            1,
        )
        report.write_text(content, encoding="utf-8")

        result = self.invoke(
            ["verify", str(report), "--minimum-chars", "20"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("title", result.output)
        self.assertIn("canonical requirements document", result.output)

    def test_verify_rejects_modified_obligation(self) -> None:
        report = self.create_report("R21-R25")
        self.complete_report(report)
        content = report.read_text(encoding="utf-8")
        content = content.replace(
            "The implementation must satisfy obligation 21.",
            "The implementation does not satisfy obligation 21.",
            1,
        )
        report.write_text(content, encoding="utf-8")

        result = self.invoke(
            ["verify", str(report), "--minimum-chars", "20"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Obligation", result.output)
        self.assertIn("canonical requirements document", result.output)

    def test_summary_requires_complete_non_overlapping_coverage(self) -> None:
        report = self.create_report("R1-R20")
        self.complete_report(report)

        result = self.invoke(
            ["summary", str(self.design), "--minimum-chars", "20"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("missing: R21", result.output)

    def test_summary_aggregates_only_non_passing_and_general_findings(self) -> None:
        first = self.create_report("R1-R20")
        second = self.create_report("R21-R25")
        self.complete_report(first)
        self.complete_report(second)
        content = first.read_text(encoding="utf-8")
        content = content.replace("Verdict: SATISFIED", "Verdict: UNSATISFIED", 1)
        content = content.replace("Severity: none", "Severity: high", 1)
        content = content.replace(
            "Finding summary: Concrete implementation evidence supports this verdict.",
            "Finding summary: Required behavior is absent from the implementation.",
            1,
        )
        first.write_text(content, encoding="utf-8")
        self.add_general_finding(second)

        result = self.invoke(
            ["summary", str(self.design), "--minimum-chars", "20"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["verdict"], "CHANGES_REQUIRED")
        self.assertEqual(payload["counts"]["UNSATISFIED"], 1)
        self.assertEqual(payload["general_findings"], 1)

        summary = Path(payload["summary"]).read_text(encoding="utf-8")
        self.assertIn('schema: "review-summary/v1"', summary)
        self.assertIn("### R1. Requirement 1", summary)
        self.assertNotIn("### R2. Requirement 2", summary)
        self.assertIn("#### Investigation performed", summary)
        self.assertIn(
            "Inspected implementation and tests. Concrete evidence supports this conclusion.",
            summary,
        )
        self.assertIn("### R21-R25/G1. Shared implementation concern", summary)
        self.assertIn("#### Evidence", summary)
        self.assertIn("The relevant code contains concrete duplicated control flow.", summary)
        self.assertIn("#### Recommendation", summary)
        self.assertIn(
            "Consolidate the shared behavior behind one maintained abstraction.",
            summary,
        )

    def test_recommendation_does_not_block_aggregate_pass(self) -> None:
        first = self.create_report("R1-R20")
        second = self.create_report("R21-R25")
        self.complete_report(first)
        self.complete_report(second)
        self.add_general_finding(second, kind="RECOMMENDATION")

        result = self.invoke(
            ["summary", str(self.design), "--minimum-chars", "20"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["verdict"], "PASS")
        summary = Path(payload["summary"]).read_text(encoding="utf-8")
        self.assertIn("- Kind: RECOMMENDATION", summary)
        self.assertIn(
            "Consolidate the shared behavior behind one maintained abstraction.",
            summary,
        )

    def test_implementation_defect_requires_changes(self) -> None:
        first = self.create_report("R1-R20")
        second = self.create_report("R21-R25")
        self.complete_report(first)
        self.complete_report(second)
        self.add_general_finding(second)

        result = self.invoke(
            ["summary", str(self.design), "--minimum-chars", "20"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["verdict"], "CHANGES_REQUIRED")

    def test_verify_requires_a_supported_general_finding_kind(self) -> None:
        report = self.create_report("R21-R25")
        self.complete_report(report)
        self.add_general_finding(report, kind="UNKNOWN")

        result = self.invoke(
            ["verify", str(report), "--minimum-chars", "20"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("invalid kind: UNKNOWN", result.output)

    def test_verify_requires_exact_general_finding_fields(self) -> None:
        report = self.create_report("R21-R25")
        self.complete_report(report)
        self.add_general_finding(report, kind="RECOMMENDATION")
        content = report.read_text(encoding="utf-8")
        content = content.replace(
            "Kind: RECOMMENDATION\n",
            "Kind: RECOMMENDATION\nUnexpected: value\n",
        )
        report.write_text(content, encoding="utf-8")

        result = self.invoke(
            ["verify", str(report), "--minimum-chars", "20"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("exactly Kind and Summary fields", result.output)

    def test_spec_defect_takes_aggregate_precedence(self) -> None:
        first = self.create_report("R1-R20")
        second = self.create_report("R21-R25")
        self.complete_report(first, verdict="SPEC_DEFECT")
        self.complete_report(second)

        result = self.invoke(
            ["summary", str(self.design), "--minimum-chars", "20"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["verdict"], "SPEC_DEFECT")


if __name__ == "__main__":
    unittest.main()
