from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from fastapi.testclient import TestClient
from app import config
from app.api import create_app, report_html
from app.demo import DEMO_QUESTION, demo_evidence, demo_output
from app.graph import build_graph, canonicalize_forecast_ids, evidence_for_model, execute, validate_forecast
from app.schemas import Forecast, ImportedEvidence, Review, RunRecord, WorldState
from app.sources import normalize_import, public_url
from app.storage import RunStore


def test_demo_reaches_report_and_preserves_provenance():
    with TemporaryDirectory() as directory, TestClient(create_app(Path(directory))) as client:
        created = client.post("/api/runs", json={"question": DEMO_QUESTION.model_dump(mode="json"), "evidence_mode": "demo"})
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        run = client.get(f"/api/runs/{run_id}").json()
        assert run["status"] == "completed", run["errors"]
        assert len(run["actions"]) == 6
        assert [(s["parent_state"], s["next_state"]) for s in run["simulation"]] == [(0, 1), (1, 2)]
        assert all(e["source_type"] == "exercise" for e in run["evidence"])
        assert run["forecast"]["calibrated"] is False
        assert sum(run["forecast"]["probabilities"].values()) == 1
        assert set(run["stage_outputs"]) == {"question", "evidence", "world", "simulation", "review", "forecast"}
        assert run["evidence_assessment"]["gaps"]
        assert client.get(f"/api/runs/{run_id}/export?format=json").status_code == 200
        assert "教学演示 / 虚构材料" in client.get(f"/api/runs/{run_id}/export?format=html").text
        assert len(list((Path(directory) / "runs" / run_id).glob("*.json"))) >= 6


def test_parse_requires_resolvable_target():
    with TemporaryDirectory() as directory, TestClient(create_app(Path(directory))) as client:
        response = client.post("/api/questions/parse", json={"question": "这件事在未来会怎样发展？", "mode": "binary"})
        assert response.json()["clarification_fields"] == ["resolve_by", "resolution_rule"]
        scenario = client.post("/api/questions/parse", json={"question": "这件事在未来会怎样发展？", "mode": "scenario"})
        assert scenario.json()["spec"]["mode"] == "scenario"


def test_references_and_probabilities_are_checked():
    evidence = demo_evidence()
    world = WorldState.model_validate(demo_output("world"))
    review = Review.model_validate(demo_output("review"))
    forecast = Forecast.model_validate(demo_output("forecast"))
    forecast.supporting[0].evidence_ids = ["E999"]
    with pytest.raises(ValueError, match="不存在"):
        validate_forecast(forecast, DEMO_QUESTION, evidence, world, [], review)
    forecast = Forecast.model_validate(demo_output("forecast"))
    forecast.probabilities = {"是": .8, "否": .8}
    with pytest.raises(ValueError, match="合计"):
        validate_forecast(forecast, DEMO_QUESTION, evidence, world, [], review)


def test_explanatory_text_around_known_references_is_canonicalized():
    evidence = demo_evidence()
    world = WorldState.model_validate(demo_output("world"))
    forecast = Forecast.model_validate(demo_output("forecast"))
    forecast.key_assumptions = ["兼容问题能按时修复（H001）"]
    forecast.supporting[0].evidence_ids = ["路线图证据 E001"]
    canonicalize_forecast_ids(forecast, evidence, world, [])
    assert forecast.key_assumptions == ["H001"]
    assert forecast.supporting[0].evidence_ids == ["E001"]
    validate_forecast(forecast, DEMO_QUESTION, evidence, world, [], Review.model_validate(demo_output("review")))


def test_import_rejects_future_or_private_source(monkeypatch):
    # This normalization test is about the demo cutoff, not the wall clock.
    monkeypatch.setattr("app.sources.utcnow", lambda: DEMO_QUESTION.as_of)
    assert not public_url("http://127.0.0.1/private")
    assert not public_url("http://localhost/private")
    item = demo_evidence()[0].model_copy(update={"published_at": DEMO_QUESTION.as_of + timedelta(days=1)})
    with pytest.raises(ValueError, match="晚于"):
        normalize_import([item], DEMO_QUESTION)
    minimal = ImportedEvidence(title="Test source", source_url="https://example.org/source", excerpt="A quoted sentence.")
    normalized = normalize_import([minimal], DEMO_QUESTION)[0]
    assert normalized.id == "E001"
    assert len(normalized.content_hash) == 64
    assert normalized.source_type == "imported"


def test_real_graph_contract_with_fake_model():
    class FakeModel:
        def complete(self, role, payload, schema, instructions):
            actor = payload.get("actor", {})
            return schema.model_validate(demo_output(role, actor.get("id"), payload.get("round", 1)))
    record = RunRecord(run_id="run_fake", question=DEMO_QUESTION, evidence_mode="import", model="fake")
    graph = build_graph(record, demo_evidence(), FakeModel(), Path("/tmp"))
    state = graph.invoke({"question": DEMO_QUESTION.model_dump(mode="json")})
    assert state["forecast"]["probabilities"] == {"是": .62, "否": .38}
    assert len(state["actions"]) == 6


def test_imported_evidence_skips_unused_question_model_call():
    class FakeModel:
        def complete(self, role, payload, schema, instructions):
            assert role != "question"
            actor = payload.get("actor", {})
            return schema.model_validate(demo_output(role, actor.get("id"), payload.get("round", 1)))

    record = RunRecord(run_id="run_import_fast", question=DEMO_QUESTION, evidence_mode="import", model="fake")
    state = build_graph(record, demo_evidence(), FakeModel(), Path("/tmp")).invoke(
        {"question": DEMO_QUESTION.model_dump(mode="json")})
    assert state["question_analysis"]["normalized_question"] == DEMO_QUESTION.question
    assert state["forecast"]["status"] == "completed"


def test_environment_drops_unknown_state_variable_with_audit_note():
    class FakeModel:
        environment_calls = 0

        def complete(self, role, payload, schema, instructions):
            output = demo_output(role, payload.get("actor", {}).get("id"), payload.get("round", 1))
            if role == "environment":
                self.environment_calls += 1
                if self.environment_calls == 1:
                    output["state_changes"]["invented_variable"] = "invalid"
                else:
                    assert "allowed_variable_keys" in payload
            return schema.model_validate(output)

    model = FakeModel()
    record = RunRecord(run_id="run_retry", question=DEMO_QUESTION, evidence_mode="import", model="fake")
    state = build_graph(record, demo_evidence(), model, Path("/tmp")).invoke({"question": DEMO_QUESTION.model_dump(mode="json")})
    assert model.environment_calls == 2
    assert "invented_variable" not in state["simulation"][0]["state_changes"]
    assert "invented_variable" in state["simulation"][0]["unresolved"][-1]
    assert state["forecast"]["status"] == "completed"


def test_forecast_retries_claim_without_reference():
    class FakeModel:
        forecast_calls = 0

        def complete(self, role, payload, schema, instructions):
            output = demo_output(role, payload.get("actor", {}).get("id"), payload.get("round", 1))
            if role == "forecast":
                self.forecast_calls += 1
                if self.forecast_calls == 1:
                    output["supporting"][0]["evidence_ids"] = []
                else:
                    assert "validation_feedback" in payload
            return schema.model_validate(output)

    model = FakeModel()
    record = RunRecord(run_id="run_forecast_retry", question=DEMO_QUESTION, evidence_mode="import", model="fake")
    state = build_graph(record, demo_evidence(), model, Path("/tmp")).invoke({"question": DEMO_QUESTION.model_dump(mode="json")})
    assert model.forecast_calls == 2
    assert state["forecast"]["supporting"][0]["evidence_ids"] == ["E001"]


def test_review_accepts_actor_action_reference_and_receives_compact_context():
    class FakeModel:
        def complete(self, role, payload, schema, instructions):
            output = demo_output(role, payload.get("actor", {}).get("id"), payload.get("round", 1))
            if role == "review":
                output["issues"][0]["affected_ids"] = ["M1-A1"]
                assert all(len(item["excerpt"]) <= 1200 for item in payload["evidence"])
                assert all("rationale_summary" not in action for action in payload["actions"])
            if role == "forecast":
                assert all(len(item["excerpt"]) <= 900 for item in payload["evidence"])
            return schema.model_validate(output)

    record = RunRecord(run_id="run_review_action", question=DEMO_QUESTION, evidence_mode="import", model="fake")
    state = build_graph(record, demo_evidence(), FakeModel(), Path("/tmp")).invoke({"question": DEMO_QUESTION.model_dump(mode="json")})
    assert state["review"]["issues"][0]["affected_ids"] == ["M1-A1"]


def test_forecast_falls_back_without_probability_when_citations_never_validate():
    class FakeModel:
        calls = 0

        def complete(self, role, payload, schema, instructions):
            output = demo_output(role, payload.get("actor", {}).get("id"), payload.get("round", 1))
            if role == "forecast":
                self.calls += 1
                output["supporting"][0]["evidence_ids"] = []
            return schema.model_validate(output)

    model = FakeModel()
    record = RunRecord(run_id="run_forecast_fallback", question=DEMO_QUESTION, evidence_mode="import", model="fake")
    state = build_graph(record, demo_evidence(), model, Path("/tmp")).invoke({"question": DEMO_QUESTION.model_dump(mode="json")})
    assert model.calls == 2
    assert state["forecast"]["status"] == "partial"
    assert state["forecast"]["probabilities"] is None
    assert len(state["forecast"]["supporting"]) == 1
    assert any("无法追溯" in item for item in state["forecast"]["limitations"])


def test_model_receives_bounded_excerpt_without_changing_snapshot():
    evidence = demo_evidence()[0].model_copy(update={"excerpt": "x" * 4000})
    compact = evidence_for_model([evidence], limit=2400)[0]
    assert len(compact["excerpt"]) == 2400
    assert len(evidence.excerpt) == 4000
    assert compact["id"] == evidence.id
    assert "snapshot_path" not in compact


def test_resume_runs_only_the_failed_stage(monkeypatch):
    should_fail = {"forecast": True}

    class FakeModel:
        def __init__(self):
            self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

        def complete(self, role, payload, schema, instructions):
            self.usage["calls"] += 1
            if role == "forecast" and should_fail["forecast"]:
                raise RuntimeError("temporary model outage")
            output = demo_output(role, payload.get("actor", {}).get("id"), payload.get("round", 1))
            return schema.model_validate(output)

    monkeypatch.setattr(config, "MODEL_API_KEY", "test-only")
    monkeypatch.setattr("app.graph.ModelClient", FakeModel)
    with TemporaryDirectory() as directory, TestClient(create_app(Path(directory))) as client:
        store = RunStore(Path(directory))
        record = RunRecord(run_id="run_resume", question=DEMO_QUESTION, evidence_mode="import", model="fake")
        execute(record, demo_evidence(), store)
        failed = store.get(record.run_id)
        assert failed.status == "failed"
        assert failed.failed_stage == "forecast"
        assert "forecast" in failed.stage_durations
        assert "review" in failed.stage_outputs and "forecast" not in failed.stage_outputs
        previous_calls = failed.usage["calls"]
        should_fail["forecast"] = False
        response = client.post(f"/api/runs/{record.run_id}/resume")
        assert response.status_code == 202
        finished = client.get(f"/api/runs/{record.run_id}").json()
        assert finished["status"] == "completed", finished["errors"]
        assert finished["usage"]["calls"] == previous_calls + 1
        assert finished["resume_count"] == 1
        assert finished["retry_history"]


def test_reuse_keeps_frozen_evidence_and_history(monkeypatch):
    class FakeModel:
        def __init__(self):
            self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        def complete(self, role, payload, schema, instructions):
            self.usage["calls"] += 1
            return schema.model_validate(demo_output(role, payload.get("actor", {}).get("id"), payload.get("round", 1)))
    monkeypatch.setattr(config, "MODEL_API_KEY", "test-only")
    monkeypatch.setattr("app.graph.ModelClient", FakeModel)
    with TemporaryDirectory() as directory, TestClient(create_app(Path(directory))) as client:
        # Seed a non-demo run with the fake model. Reusing a demo as real evidence is now rejected.
        first_id = client.post("/api/runs", json={"question": DEMO_QUESTION.model_dump(mode="json"), "evidence_mode": "import",
            "evidence": [e.model_dump(mode="json") for e in demo_evidence()]}).json()["run_id"]
        second_id = client.post("/api/runs", json={"question": DEMO_QUESTION.model_dump(mode="json"), "evidence_mode": "reuse", "parent_run_id": first_id}).json()["run_id"]
        first = client.get(f"/api/runs/{first_id}").json()
        second = client.get(f"/api/runs/{second_id}").json()
        assert second["status"] == "completed", second["errors"]
        assert second["parent_run_id"] == first_id
        assert second["question_version"] == 2
        assert second["evidence"] == first["evidence"]


def test_restart_marks_inflight_record_interrupted():
    with TemporaryDirectory() as directory:
        store = RunStore(Path(directory))
        run = RunRecord(run_id="run_restart", question=DEMO_QUESTION, evidence_mode="demo", demo=True, model="fixture", status="running")
        store.save(run)
        store.mark_interrupted()
        assert store.get(run.run_id).status == "interrupted"
        assert "重启" in store.get(run.run_id).errors[0]


def test_export_escapes_untrusted_evidence():
    record = RunRecord(run_id="run_escape", question=DEMO_QUESTION, evidence_mode="demo", demo=True, model="fixture")
    record.evidence = [demo_evidence()[0].model_copy(update={"title": "<script>alert(1)</script>", "excerpt": "<img src=x onerror=alert(1)>"})]
    html = report_html(record)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html
