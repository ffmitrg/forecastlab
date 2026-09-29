"""Lock the frozen labelled examples and the reproducible comparison command."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def run_evaluation(tmp_path, *extra):
    report = tmp_path / "policy-report.json"
    process = subprocess.run(
        [sys.executable, "eval/review_policy.py", "--output", str(report), *extra],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    return process.stdout, json.loads(report.read_text(encoding="utf-8"))


def test_frozen_review_cases_are_a_passing_regression_set(tmp_path):
    _, report = run_evaluation(tmp_path)
    fixture = ROOT / "eval/review_policy_cases.json"
    assert report["cases_sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
    assert len(report["cases"]) == 16
    current = report["summary"]["all"]["current"]
    assert current["fully_correct_cases"] == {"count": 16, "total": 16}
    assert current["keep_labelled_opinions_dropped"]["count"] == 0
    assert current["pure_future_demands_retained"]["count"] == 0
    assert "NOT an independent holdout success" in report["holdout_interpretation"]


def test_single_case_command_shows_the_plan_document_bug(tmp_path):
    stdout, report = run_evaluation(tmp_path, "--case", "D03")
    assert len(report["cases"]) == 1
    assert "缺少截至日前已公布的2026年10月1日发布计划文件" in stdout
    assert "drop -> keep" in stdout
    assert "legacy=qualified | current=blocked" in stdout
