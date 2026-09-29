"""Replay frozen raw reviews through old/new temporal policies without API calls.

Run from the repository root:
    uv run python eval/review_policy.py --split dev
    uv run python eval/review_policy.py --case D03
    uv run python eval/review_policy.py --output eval/review_policy_results.json

Labels are assistant-authored synthetic examples, not human-validated judgements.
This measures policy behaviour on these examples, not forecasting quality.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.review_policy import apply_review_policy
from app.schemas import QuestionSpec, Review
from review_policy_legacy import BASELINE_COMMIT, apply_legacy_policy


FROZEN_CASES_SHA256 = "b26846307a25dfefa765df4e07eae29b3d589ee80622a6a5b197312d9545304d"
FIELDS = ("issues", "missing_evidence", "unsupported_claims")


def stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def evaluate_one(case, policy):
    raw = Review.model_validate(case["raw_review"])
    before = raw.model_dump(mode="json")
    question = QuestionSpec.model_validate(case["question"])
    if policy == "legacy":
        result, trace = apply_legacy_policy(raw, question, case["world_summary"])
    else:
        result, trace = apply_review_policy(raw, question)
    if raw.model_dump(mode="json") != before:
        raise ValueError(f"{case['id']}: policy mutated its input")
    rows = trace["decisions"]
    actual = {(row["field"], row["index"]): row for row in rows}
    expected_keys = {(field, index) for field in FIELDS for index in range(len(before[field]))}
    if len(actual) != len(rows) or set(actual) != expected_keys:
        raise ValueError(f"{case['id']}: decision trace is incomplete or has duplicate/extra entries")
    # Cross-check the trace against the returned review, rather than trusting
    # self-reported keep/drop decisions alone. Synthetic labels have no duplicates.
    output = result.model_dump(mode="json")
    for field in FIELDS:
        surviving = Counter(stable(value) for value in output[field])
        for index, value in enumerate(before[field]):
            action = actual[(field, index)]["action"]
            if action not in {"keep", "drop"}:
                raise ValueError(f"{case['id']}: unknown action {action}")
            key = stable(value)
            if action == "keep":
                if not surviving[key]:
                    raise ValueError(f"{case['id']}: trace says keep but the output lost {field}[{index}]")
                surviving[key] -= 1
            elif surviving[key]:
                raise ValueError(f"{case['id']}: trace says drop but the output retained {field}[{index}]")
    decisions = []
    for field in FIELDS:
        for index, label in enumerate(case["expected"]["decisions"][field]):
            observed = actual[(field, index)]
            decisions.append({"field": field, "index": index,
                              "kind": label["kind"], "expected": label["action"],
                              "actual": observed["action"], "reason": observed["reason"],
                              "correct": label["action"] == observed["action"]})
    status_correct = result.status == case["expected"]["status"]
    return {"status": result.status, "expected_status": case["expected"]["status"],
            "status_correct": status_correct, "decisions": decisions,
            "case_correct": status_correct and all(row["correct"] for row in decisions)}


def ratio(numerator, denominator):
    return {"count": numerator, "total": denominator}


def aggregate(cases, policy):
    results = [case[policy] for case in cases]
    decisions = [row for result in results for row in result["decisions"]]
    keep = [row for row in decisions if row["expected"] == "keep"]
    pure = [row for row in decisions if row["kind"] == "pure_future_demand"]
    mixed = [row for row in decisions if row["kind"] == "mixed"]
    return {
        "keep_labelled_opinions_dropped": ratio(sum(row["actual"] == "drop" for row in keep), len(keep)),
        "pure_future_demands_retained": ratio(sum(row["actual"] == "keep" for row in pure), len(pure)),
        "mixed_opinions_dropped": ratio(sum(row["actual"] == "drop" for row in mixed), len(mixed)),
        "correct_status": ratio(sum(result["status_correct"] for result in results), len(results)),
        "fully_correct_cases": ratio(sum(result["case_correct"] for result in results), len(results)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="all")
    parser.add_argument("--case", action="append", dest="case_ids", help="Show a specific before/after example; repeat to select several IDs")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-label", default="replay", help="Descriptive provenance label; not proof of an independent run")
    args = parser.parse_args()
    fixture = ROOT / "eval" / "review_policy_cases.json"
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    if digest != FROZEN_CASES_SHA256:
        raise SystemExit("Frozen fixture changed. Review labels and update the declared evaluation protocol explicitly.")
    data = json.loads(fixture.read_text(encoding="utf-8"))
    requested = set(args.case_ids or [])
    unknown = requested - {case["id"] for case in data["cases"]}
    if unknown:
        parser.error("Unknown case IDs: " + ", ".join(sorted(unknown)))
    rows = []
    for case in data["cases"]:
        if args.split != "all" and case["split"] != args.split:
            continue
        if requested and case["id"] not in requested:
            continue
        rows.append({"id": case["id"], "split": case["split"], "category": case["category"],
                     "legacy": evaluate_one(case, "legacy"), "current": evaluate_one(case, "current")})
        if requested:
            print(f"\n{case['id']} | as_of={case['question']['as_of']}")
            print("Raw review: " + json.dumps(case["raw_review"], ensure_ascii=False))
            old, new = rows[-1]["legacy"], rows[-1]["current"]
            print(f"Status: raw={case['raw_review']['status']} | legacy={old['status']} | current={new['status']} | expected={case['expected']['status']}")
            for previous, current in zip(old["decisions"], new["decisions"], strict=True):
                print(f"  {current['field']}[{current['index']}]: {previous['actual']} -> {current['actual']} (expected={current['expected']}; {current['reason']})")
    if not rows:
        parser.error("No cases selected; check --case and --split together.")
    summaries = {}
    for split in ("dev", "heldout", "all"):
        selected = [row for row in rows if split == "all" or row["split"] == split]
        if selected:
            summaries[split] = {policy: aggregate(selected, policy) for policy in ("legacy", "current")}
    report = {
        "scope": data["scope"], "label_provenance": data["label_provenance"],
        "split_protocol": data["split_protocol"], "run_label": args.run_label,
        "holdout_interpretation": (
            "First requested evaluation after implementation was frozen; held-out texts had not been shared with the implementer. This label is a recorded workflow assertion, not independently verifiable by this script."
            if args.run_label == "first-heldout" else
            "Regression replay. The original held-out cases were exposed during the first evaluation and used to fix H01. The dev/heldout split names preserve original assignment; subsequent passing results are NOT an independent holdout success. See agent6-first-heldout.json for the preserved first result."
        ),
        "baseline_commit": BASELINE_COMMIT, "cases_sha256": digest,
        "policy_source_sha256": hashlib.sha256((ROOT / "backend/app/review_policy.py").read_bytes()).hexdigest(),
        "evaluation_conditions": "Identical frozen raw reviews and cutoff times; no model, retrieval, world simulation, or evidence_audit calls. Workflow integration is tested separately.",
        "metrics_note": "Counts use labelled opinions as denominators except status/case metrics. Keep-labelled includes legitimate, mixed and ambiguous opinions; lower is better for the first three metrics.",
        "summary": summaries, "cases": rows,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for split, results in summaries.items():
        print(f"[{split}] legacy -> current")
        for key in results["legacy"]:
            old, new = results["legacy"][key], results["current"][key]
            print(f"  {key}: {old['count']}/{old['total']} -> {new['count']}/{new['total']}")
    failed = [row["id"] for row in rows if not row["current"]["case_correct"]]
    print("Current policy failed cases: " + (", ".join(failed) if failed else "none"))
    if args.output:
        print(f"Report: {args.output}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
