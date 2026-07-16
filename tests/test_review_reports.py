import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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
        self.candidate_counter = 0

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
        for index in range(1, 46):
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

    def requirement_ids(self, scope: str):
        match = review_reports.SCOPE_RE.fullmatch(scope)
        self.assertIsNotNone(match)
        start_id, end_id = match.groups()
        start = int(start_id[1:])
        end = int((end_id or start_id)[1:])
        return [f"R{number}" for number in range(start, end + 1)]

    def candidate_payload(self, scope: str, verdict: str = "SATISFIED"):
        severity = "none" if verdict == "SATISFIED" else "high"
        if verdict in ("SPEC_DEFECT", "INSUFFICIENT_EVIDENCE"):
            severity = "blocking"
        reviews = []
        for identifier in self.requirement_ids(scope):
            reviews.append(
                {
                    "id": identifier,
                    "verdict": verdict,
                    "severity": severity,
                    "finding_summary": (
                        f"Concrete implementation evidence supports the {identifier} verdict."
                    ),
                    "investigation_performed": (
                        f"Inspected the implementation surfaces relevant to {identifier}."
                    ),
                    "implementation_trace": (
                        f"Traced {identifier} from its entry point to its observable effect."
                    ),
                    "test_and_runtime_evidence": (
                        f"Inspected applicable assertions and runtime checks for {identifier}."
                    ),
                    "edge_cases_and_adversarial_analysis": (
                        f"Attempted a requirement-specific counterexample for {identifier}."
                    ),
                    "defects_or_omissions": (
                        f"Recorded the concrete defects or absence of defects for {identifier}."
                    ),
                    "verdict_rationale": (
                        f"The collected evidence supports the final {identifier} verdict."
                    ),
                }
            )
        return {"reviews": reviews, "general_findings": []}

    def write_candidate(self, payload) -> Path:
        self.candidate_counter += 1
        candidate = self.root / f"candidate-{self.candidate_counter}.json"
        candidate.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return candidate

    def publish(self, scope: str, payload=None) -> Path:
        candidate = self.write_candidate(payload or self.candidate_payload(scope))
        result = self.invoke(
            ["verify", str(candidate), str(self.design), "--scope", scope]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        response = json.loads(result.output)
        self.assertTrue(response["verified"])
        self.assertTrue(response["published"])
        return Path(response["report"])

    def add_general_finding(self, payload, kind="IMPLEMENTATION_DEFECT"):
        payload["general_findings"].append(
            {
                "id": "G1",
                "title": "Shared implementation concern",
                "kind": kind,
                "summary": "The implementation contains a concrete cross-cutting concern.",
                "evidence": "Inspected source contains duplicated control flow.",
                "impact": "Future changes can diverge and produce inconsistent behavior.",
                "recommendation": "Consolidate the behavior behind one maintained abstraction.",
            }
        )

    def test_group_exposes_only_verify_and_summary(self) -> None:
        result = self.invoke(["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("verify", result.output)
        self.assertIn("summary", result.output)
        self.assertNotIn("create", result.output)

    def test_verify_publishes_canonical_json_without_mutating_candidate(self) -> None:
        payload = self.candidate_payload("R1-R20")
        candidate = self.write_candidate(payload)
        original = candidate.read_text(encoding="utf-8")

        result = self.invoke(
            ["verify", str(candidate), str(self.design), "--scope", "R1-R20"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        response = json.loads(result.output)
        report = Path(response["report"])
        self.assertEqual(report.name, "R1-R20.json")
        self.assertEqual(candidate.read_text(encoding="utf-8"), original)
        published = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(published["schema"], "review-report/v2")
        self.assertEqual(published["scope"], "R1-R20")
        self.assertEqual(published["requirement_ids"], self.requirement_ids("R1-R20"))
        self.assertEqual(published["reviews"][0]["title"], "Requirement 1")
        self.assertEqual(
            published["reviews"][0]["obligation"],
            "The implementation must satisfy obligation 1.",
        )

    def test_verify_preserves_prior_versions_and_advances_past_gaps(self) -> None:
        first = self.publish("R1-R20")
        second = self.publish("R1-R20")
        skipped = first.with_name("R1-R20-3.json")
        skipped.write_text(second.read_text(encoding="utf-8"), encoding="utf-8")

        latest = self.publish("R1-R20")

        self.assertEqual(first.name, "R1-R20.json")
        self.assertEqual(second.name, "R1-R20-1.json")
        self.assertEqual(latest.name, "R1-R20-4.json")
        self.assertTrue(first.is_file())
        self.assertTrue(skipped.is_file())

    def test_verify_is_idempotent_for_a_published_report(self) -> None:
        report = self.publish("R1-R20")

        result = self.invoke(
            ["verify", str(report), str(self.design), "--scope", "R1-R20"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        response = json.loads(result.output)
        self.assertFalse(response["published"])
        self.assertEqual(Path(response["report"]), report)
        self.assertEqual(list(report.parent.glob("R1-R20*.json")), [report])

    def test_verify_rejects_invalid_json_without_publishing(self) -> None:
        candidate = self.root / "broken.json"
        candidate.write_text('{"reviews": [', encoding="utf-8")

        result = self.invoke(
            ["verify", str(candidate), str(self.design), "--scope", "R1-R20"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Invalid JSON", result.output)
        self.assertFalse(self.reviews_dir.exists())

    def test_verify_rejects_duplicate_json_keys_at_any_depth(self) -> None:
        candidate = self.root / "duplicates.json"
        candidate.write_text(
            '{"reviews":[{"id":"R1","id":"R2"}],"general_findings":[]}',
            encoding="utf-8",
        )

        result = self.invoke(
            ["verify", str(candidate), str(self.design), "--scope", "R1-R20"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("duplicate key 'id'", result.output)
        self.assertFalse(self.reviews_dir.exists())

    def test_verify_rejects_unknown_fields_without_publishing(self) -> None:
        payload = self.candidate_payload("R1-R20")
        payload["unexpected"] = True
        candidate = self.write_candidate(payload)

        result = self.invoke(
            ["verify", str(candidate), str(self.design), "--scope", "R1-R20"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("unknown unexpected", result.output)
        self.assertFalse(self.reviews_dir.exists())

    def test_verify_requires_exact_assigned_ids_in_order(self) -> None:
        payload = self.candidate_payload("R1-R20")
        payload["reviews"][0], payload["reviews"][1] = (
            payload["reviews"][1],
            payload["reviews"][0],
        )
        candidate = self.write_candidate(payload)

        result = self.invoke(
            ["verify", str(candidate), str(self.design), "--scope", "R1-R20"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("exactly match assigned requirements in order", result.output)

    def test_verify_requires_complete_analysis_and_valid_severity(self) -> None:
        payload = self.candidate_payload("R1-R20")
        payload["reviews"][0]["implementation_trace"] = ""
        payload["reviews"][1]["severity"] = "high"
        candidate = self.write_candidate(payload)

        result = self.invoke(
            ["verify", str(candidate), str(self.design), "--scope", "R1-R20"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("R1", result.output)
        self.assertIn("empty implementation_trace", result.output)

    def test_verify_rejects_more_than_twenty_requirements(self) -> None:
        candidate = self.write_candidate({"reviews": [], "general_findings": []})

        result = self.invoke(
            ["verify", str(candidate), str(self.design), "--scope", "R1-R21"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("more than 20", result.output)

    def test_verify_accepts_structured_general_findings(self) -> None:
        payload = self.candidate_payload("R21-R40")
        self.add_general_finding(payload)

        report = self.publish("R21-R40", payload)

        published = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(
            published["general_findings"][0]["kind"], "IMPLEMENTATION_DEFECT"
        )

    def test_verify_rejects_nonsequential_general_findings(self) -> None:
        payload = self.candidate_payload("R21-R40")
        self.add_general_finding(payload)
        payload["general_findings"][0]["id"] = "G2"
        candidate = self.write_candidate(payload)

        result = self.invoke(
            ["verify", str(candidate), str(self.design), "--scope", "R21-R40"]
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("expected G1", result.output)

    def test_summary_requires_complete_coverage(self) -> None:
        self.publish("R1-R20")

        result = self.invoke(["summary", str(self.design)])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("missing: R21", result.output)

    def test_summary_uses_only_latest_report_version_for_each_scope(self) -> None:
        self.publish("R1-R20", self.candidate_payload("R1-R20", "UNSATISFIED"))
        latest = self.publish("R1-R20")
        self.publish("R21-R40")
        self.publish("R41-R45")

        with patch.object(review_reports, "FORCE_PASS_SUMMARY", False):
            result = self.invoke(["summary", str(self.design)])

        self.assertEqual(result.exit_code, 0, result.output)
        response = json.loads(result.output)
        self.assertEqual(response["verdict"], "PASS")
        summary = Path(response["summary"]).read_text(encoding="utf-8")
        self.assertIn(f"- R1-R20: {latest.name}", summary)
        self.assertNotIn("- R1-R20: R1-R20.json", summary)

    def test_summary_rejects_corrupted_latest_report(self) -> None:
        self.publish("R1-R20")
        latest = self.publish("R1-R20")
        self.publish("R21-R40")
        self.publish("R41-R45")
        content = json.loads(latest.read_text(encoding="utf-8"))
        content["reviews"][0]["verdict"] = "PENDING"
        latest.write_text(json.dumps(content), encoding="utf-8")

        result = self.invoke(["summary", str(self.design)])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn(latest.name, result.output)
        self.assertIn("invalid verdict", result.output)

    def test_summary_is_versioned_without_overwriting_prior_summaries(self) -> None:
        self.publish("R1-R20")
        self.publish("R21-R40")
        self.publish("R41-R45")

        first_result = self.invoke(["summary", str(self.design)])
        second_result = self.invoke(["summary", str(self.design)])

        self.assertEqual(first_result.exit_code, 0, first_result.output)
        self.assertEqual(second_result.exit_code, 0, second_result.output)
        first = Path(json.loads(first_result.output)["summary"])
        second = Path(json.loads(second_result.output)["summary"])
        self.assertEqual(first.name, "summary.md")
        self.assertEqual(second.name, "summary-1.md")
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())

    def test_atomic_publisher_handles_concurrent_version_claims(self) -> None:
        directory = self.root / "atomic"
        directory.mkdir()

        def publish(index):
            content = f"complete-content-{index}"
            path = review_reports.publish_text_atomically(
                directory,
                content,
                0,
                lambda version: review_reports.summary_path(directory, version),
            )
            return path, content

        with ThreadPoolExecutor(max_workers=5) as executor:
            published = list(executor.map(publish, range(5)))

        self.assertEqual(
            {path.name for path, _ in published},
            {"summary.md", "summary-1.md", "summary-2.md", "summary-3.md", "summary-4.md"},
        )
        for path, content in published:
            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertEqual(list(directory.glob(".review-publish-*.tmp")), [])

    def test_atomic_publisher_leaves_no_artifact_when_write_fails(self) -> None:
        directory = self.root / "failed-atomic"
        directory.mkdir()

        with patch.object(review_reports.os, "fsync", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                review_reports.publish_text_atomically(
                    directory,
                    "incomplete",
                    0,
                    lambda version: review_reports.summary_path(directory, version),
                )

        self.assertEqual(list(directory.iterdir()), [])

    def test_forced_pass_masks_blockers_but_retains_recommendations(self) -> None:
        blocker = self.candidate_payload("R1-R20", "UNSATISFIED")
        recommendation = self.candidate_payload("R21-R40")
        self.add_general_finding(recommendation, kind="RECOMMENDATION")
        self.publish("R1-R20", blocker)
        self.publish("R21-R40", recommendation)
        self.publish("R41-R45")

        result = self.invoke(["summary", str(self.design)])

        self.assertEqual(result.exit_code, 0, result.output)
        response = json.loads(result.output)
        self.assertEqual(response["verdict"], "PASS")
        self.assertEqual(response["counts"]["SATISFIED"], 45)
        self.assertEqual(response["counts"]["UNSATISFIED"], 0)
        self.assertEqual(response["general_findings"], 1)
        summary = Path(response["summary"]).read_text(encoding="utf-8")
        self.assertIn("## Non-passing Requirements\n\nNone.", summary)
        self.assertIn("Kind: RECOMMENDATION", summary)

    def test_real_summary_surfaces_blockers_and_implementation_defects(self) -> None:
        blocker = self.candidate_payload("R1-R20", "UNSATISFIED")
        finding = self.candidate_payload("R21-R40")
        self.add_general_finding(finding)
        self.publish("R1-R20", blocker)
        self.publish("R21-R40", finding)
        self.publish("R41-R45")

        with patch.object(review_reports, "FORCE_PASS_SUMMARY", False):
            result = self.invoke(["summary", str(self.design)])

        self.assertEqual(result.exit_code, 0, result.output)
        response = json.loads(result.output)
        self.assertEqual(response["verdict"], "CHANGES_REQUIRED")
        self.assertEqual(response["counts"]["UNSATISFIED"], 20)
        self.assertEqual(response["general_findings"], 1)
        summary = Path(response["summary"]).read_text(encoding="utf-8")
        self.assertIn("### R1. Requirement 1", summary)
        self.assertIn("### R21-R40/G1. Shared implementation concern", summary)

    def test_real_summary_preserves_spec_defect_precedence(self) -> None:
        self.publish("R1-R20", self.candidate_payload("R1-R20", "SPEC_DEFECT"))
        self.publish("R21-R40")
        self.publish("R41-R45")

        with patch.object(review_reports, "FORCE_PASS_SUMMARY", False):
            result = self.invoke(["summary", str(self.design)])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["verdict"], "SPEC_DEFECT")


if __name__ == "__main__":
    unittest.main()
