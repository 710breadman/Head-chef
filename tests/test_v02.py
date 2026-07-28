import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib import error as url_error

from head_chef.context_packager import package_context, render_package, split_context
from head_chef.config import Settings, resolve_state_dir, write_default_config
from head_chef.contracts import WORKER_OUTPUT_SCHEMA
from head_chef.executors import execute_job
from head_chef.jobs import JobCard, load_job_card, save_job_card
from head_chef.models import ModelProfile
from head_chef.models import infer_capabilities
from head_chef.ollama import OllamaClient, OllamaResponse
from head_chef.registry import apply_overrides, reconcile_registry
from head_chef.cli import _append_outcome, _captured_command, _cook_split_jobs, _evidence_path, _json, _public_job, _public_run, _safe_project_text, build_parser, cmd_cook
from head_chef.kitchen import build_kitchen
from head_chef.benchmark import _chat_score, benchmark_model, run_benchmarks
from contextlib import redirect_stdout
import io
from unittest.mock import patch
from head_chef.router import RouteRequest, route
from head_chef.runs import RunRecord, next_attempt, save_run
from head_chef.storage import ensure_state
from head_chef.verification import parse_worker_output, verify_output


VALID_RESULT = {
    "result": "done",
    "changed_files": [],
    "assumptions": [],
    "risks": [],
    "tests_recommended": [],
    "acceptance_check": [{"criterion": "works", "met": True, "evidence": "inspection"}],
    "confidence": 0.8,
    "blockers": [],
}


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat(self, model, messages, **kwargs):
        self.calls.append(("chat", model, messages, kwargs))
        return OllamaResponse(json.dumps(VALID_RESULT), {"eval_count": 2})

    def embed(self, model, inputs, **kwargs):
        self.calls.append(("embed", model, inputs, kwargs))
        return OllamaResponse("", {"embeddings": [[0.1, 0.2]]})


class ExecutorTests(unittest.TestCase):
    def test_chat_executor_uses_schema(self):
        client = FakeClient()
        result = execute_job(client, JobCard("j", ".", "analyze", selected_model="m"), timeout_seconds=2)
        self.assertEqual(result.executor, "analysis")
        self.assertEqual(client.calls[0][0], "chat")
        self.assertEqual(client.calls[0][3]["format_schema"], WORKER_OUTPUT_SCHEMA)
        self.assertIs(client.calls[0][3]["think"], False)

    def test_chat_executor_applies_per_task_limits(self):
        client = FakeClient()
        job = JobCard(
            "j", ".", "analyze", selected_model="m",
            context_limit_tokens=16384, output_limit_tokens=4096,
        )
        execute_job(client, job, timeout_seconds=2)
        self.assertEqual(client.calls[0][3]["options"]["num_ctx"], 16384)
        self.assertEqual(client.calls[0][3]["options"]["num_predict"], 4096)

    def test_embedding_executor_does_not_chat(self):
        client = FakeClient()
        job = JobCard("j", ".", "embed", selected_model="e", category="embedding", context_text="hello")
        result = execute_job(client, job, timeout_seconds=2)
        self.assertEqual(result.executor, "embedding")
        self.assertEqual(client.calls[0][0], "embed")

    def test_vision_requires_image(self):
        with self.assertRaisesRegex(ValueError, "requires at least one"):
            execute_job(FakeClient(), JobCard("j", ".", "inspect", selected_model="v", category="vision"), timeout_seconds=2)

    def test_ollama_thinking_can_be_disabled_for_deterministic_eval(self):
        class CaptureClient(OllamaClient):
            def _request(self, method, path, payload=None, timeout_seconds=None):
                self.payload = payload
                return {"message": {"content": "{}"}}

        client = CaptureClient("http://127.0.0.1:11434")
        client.chat("m", [{"role": "user", "content": "x"}], think=False)
        self.assertIs(client.payload["think"], False)

    def test_ollama_transient_connection_failure_retries(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"version":"ok"}'

        client = OllamaClient("http://127.0.0.1:11434", request_retries=1)
        with patch("head_chef.ollama.request.urlopen", side_effect=[url_error.URLError("restart"), Response()]), \
             patch("head_chef.ollama.time.sleep"):
            self.assertEqual(client.version(), "ok")


class PolicyTests(unittest.TestCase):
    def test_manual_coding_override_never_bypasses_review(self):
        model = ModelProfile("coder", context_tokens=32000, capabilities={"coding"})
        decision = route(RouteRequest("fix code", manual_model="coder"), [model])
        self.assertTrue(decision.coordinator_review_required)

    def test_manual_planning_override_never_bypasses_review(self):
        model = ModelProfile("planner", context_tokens=32000, capabilities={"planning"})
        decision = route(RouteRequest("make plan", required_capability="planning", manual_model="planner"), [model])
        self.assertTrue(decision.coordinator_review_required)

    def test_manual_override_rejected_outside_advertised_strength(self):
        model = ModelProfile("writer", capabilities={"writing"})
        decision = route(RouteRequest("inspect image", required_capability="vision", manual_model="writer"), [model])
        self.assertIsNone(decision.selected_model)
        self.assertIn("does not advertise vision", decision.explanation)

    def test_cloud_model_excluded_from_local_kitchen(self):
        local = ModelProfile("local", size_bytes=10_000_000, capabilities={"analysis"})
        cloud = ModelProfile("remote:cloud", size_bytes=100, capabilities={"analysis"})
        decision = route(RouteRequest("analyze", required_capability="analysis"), [cloud, local])
        self.assertEqual(decision.selected_model, "local")


class ContextTests(unittest.TestCase):
    def test_explicit_context_file_stays_inside_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ok.txt").write_text("ok", encoding="utf-8")
            self.assertEqual(_safe_project_text(root, "ok.txt"), "ok")
            with self.assertRaisesRegex(ValueError, "project-relative"):
                _safe_project_text(root, str((root / "ok.txt").resolve()))
            with self.assertRaisesRegex(ValueError, "project-relative"):
                _safe_project_text(root, "../outside.txt")

    def test_manifest_hash_and_deduplication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.txt").write_text("same", encoding="utf-8")
            (root / "b.txt").write_text("same", encoding="utf-8")
            items = package_context(root, ["a.txt", "b.txt"])
            self.assertEqual(len(items), 1)
            self.assertIn(items[0].sha256, render_package(items))

    def test_traversal_and_binary_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bad.bin").write_bytes(b"a\x00b")
            with self.assertRaisesRegex(ValueError, "project-relative"):
                package_context(root, [str((root.parent / "outside.txt").resolve())])
            with self.assertRaisesRegex(ValueError, "Binary"):
                package_context(root, ["bad.bin"])

    def test_split_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a").write_text("a" * 5, encoding="utf-8")
            (root / "b").write_text("b" * 5, encoding="utf-8")
            chunks = split_context(package_context(root, ["a", "b"]), 6)
            self.assertEqual([[x.path for x in chunk] for chunk in chunks], [["a"], ["b"]])

    def test_every_state_writer_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for operation in (
                lambda: resolve_state_dir(root, "../outside"),
                lambda: write_default_config(root, Settings(state_dir="../outside")),
                lambda: ensure_state(root, "../outside"),
            ):
                with self.assertRaises(ValueError):
                    operation()

    def test_state_symlink_cannot_escape_project(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "linked").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaisesRegex(ValueError, "escapes"):
                resolve_state_dir(root, "linked/state")


class RunAndVerificationTests(unittest.TestCase):
    def test_worker_run_requires_schema_acceptance_and_no_blockers(self):
        from head_chef.cli import _worker_run_succeeded

        self.assertTrue(_worker_run_succeeded({"blockers": []}, [], True))
        self.assertFalse(_worker_run_succeeded({"blockers": ["Need input"]}, [], True))
        self.assertFalse(_worker_run_succeeded({"blockers": []}, [], False))
        self.assertFalse(_worker_run_succeeded({"blockers": []}, ["invalid"], True))

    def test_run_attempts_are_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            runs = Path(temp)
            first = RunRecord("job", "m", "chat", 1, True, run_id="one")
            path = save_run(first, runs)
            self.assertTrue(path.exists())
            self.assertEqual(next_attempt(runs, "job"), 2)
            with self.assertRaises(FileExistsError):
                save_run(first, runs)

    def test_valid_output_still_pending_when_review_required(self):
        parsed, errors = parse_worker_output(json.dumps(VALID_RESULT))
        result = verify_output(parsed, errors, ["works"], coordinator_review_required=True)
        self.assertEqual(result.status, "pending")
        self.assertTrue(result.acceptance_complete)

    def test_invalid_prose_rejected(self):
        parsed, errors = parse_worker_output("looks good")
        result = verify_output(parsed, errors, [], coordinator_review_required=False)
        self.assertFalse(result.valid_schema)
        self.assertEqual(result.status, "needs_revision")

    def test_compound_criterion_can_be_evidenced_by_result(self):
        value = dict(VALID_RESULT)
        value["result"] = "One red circle, one blue square, and one green triangle."
        value["acceptance_check"] = [
            {"criterion": "red circle", "met": True, "evidence": "seen"},
            {"criterion": "blue square", "met": True, "evidence": "seen"},
            {"criterion": "green triangle", "met": True, "evidence": "seen"},
        ]
        result = verify_output(
            value,
            [],
            ["One red circle, one blue square, and one green triangle"],
            coordinator_review_required=False,
        )
        self.assertTrue(result.acceptance_complete)
        self.assertEqual(result.status, "accepted")

    def test_confidence_percent_is_normalized_and_audited(self):
        value = dict(VALID_RESULT)
        value["confidence"] = 85
        parsed, errors = parse_worker_output(json.dumps(value))
        self.assertEqual(errors, [])
        self.assertEqual(parsed["confidence"], 0.85)
        self.assertIn("85/100", parsed["normalizations"][0])

    def test_leading_instruction_verb_does_not_cause_false_negative(self):
        value = dict(VALID_RESULT)
        value["result"] = "One red circle, one blue square, and one green triangle."
        value["acceptance_check"] = []
        result = verify_output(
            value,
            [],
            ["Report one red circle, one blue square, and one green triangle"],
            coordinator_review_required=False,
        )
        self.assertTrue(result.acceptance_complete)

    def test_evidenced_acceptance_paraphrase_matches_conservatively(self):
        value = dict(VALID_RESULT)
        value["result"] = "Due to missing validation, failures may occur."
        value["acceptance_check"] = [{
            "criterion": "Rewrite the statement concisely",
            "met": True,
            "evidence": "Output is shorter.",
        }]
        result = verify_output(value, [], ["Produce concise rewrite"], coordinator_review_required=False)
        self.assertTrue(result.acceptance_complete)
        self.assertEqual(result.status, "accepted")

    def test_non_blocker_sentinel_is_removed(self):
        value = dict(VALID_RESULT)
        value["blockers"] = ["None"]
        parsed, errors = parse_worker_output(json.dumps(value))
        result = verify_output(parsed, errors, ["works"], coordinator_review_required=False)
        self.assertEqual(parsed["blockers"], [])
        self.assertEqual(result.status, "accepted")


class RegistryTests(unittest.TestCase):
    def test_registry_reconciliation_detects_inventory_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "registry.json"
            reconcile_registry(path, [
                ModelProfile("same", digest="a"),
                ModelProfile("gone", digest="b"),
                ModelProfile("changed", digest="old"),
            ])
            changes = reconcile_registry(path, [
                ModelProfile("same", digest="a"),
                ModelProfile("changed", digest="new"),
                ModelProfile("fresh", digest="c"),
            ])
        self.assertEqual(changes["new"], ["fresh"])
        self.assertEqual(changes["updated"], ["changed"])
        self.assertEqual(changes["removed"], ["gone"])
        self.assertEqual(changes["unchanged"], ["same"])

    def test_owner_override_adds_and_removes_capabilities(self):
        profile = ModelProfile("m", capabilities={"analysis"}, context_tokens=1000)
        apply_overrides(profile, {
            "add_capabilities": ["coding"],
            "remove_capabilities": ["analysis"],
            "context_tokens": 4096,
        })
        self.assertEqual(profile.capabilities, {"coding"})
        self.assertEqual(profile.context_tokens, 4096)

    def test_invalid_override_fails_closed(self):
        with self.assertRaises(ValueError):
            apply_overrides(ModelProfile("m"), {"surprise": True})

    def test_empty_override_has_no_false_provenance(self):
        profile = apply_overrides(ModelProfile("m"), {})
        self.assertNotIn("owner-override", profile.notes)

    def test_specialist_names_do_not_inherit_unrelated_family_roles(self):
        coder, _ = infer_capabilities("qwen2.5-coder:7b", "qwen")
        story, _ = infer_capabilities("ozan-story:latest", "llama")
        vision, _ = infer_capabilities("qwen3-vl:8b", "qwen")
        self.assertEqual(coder, {"analysis", "coding", "planning"})
        self.assertEqual(story, {"analysis", "writing"})
        self.assertEqual(vision, {"analysis", "vision", "retrieval"})


class ContractTests(unittest.TestCase):
    def test_global_evidence_fallback_and_local_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "project"
            global_state = base / "global"
            project.mkdir()
            (global_state / "benchmarks").mkdir(parents=True)
            global_evidence = global_state / "benchmarks" / "latest.json"
            global_evidence.write_text("{}", encoding="utf-8")
            with patch.dict("os.environ", {"HEAD_CHEF_GLOBAL_STATE": str(global_state)}):
                self.assertEqual(
                    _evidence_path(project, Settings(), "benchmarks/latest.json"),
                    global_evidence.resolve(),
                )
                local_evidence = project / ".head-chef" / "benchmarks" / "latest.json"
                local_evidence.parent.mkdir(parents=True)
                local_evidence.write_text("{}", encoding="utf-8")
                self.assertEqual(
                    _evidence_path(project, Settings(), "benchmarks/latest.json"),
                    local_evidence,
                )

    def test_outcomes_improve_project_and_global_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            local = base / "project" / "outcomes.jsonl"
            global_state = base / "global"
            outcome = {"model": "m", "model_digest": "d", "category": "analysis", "ok": True}
            with patch.dict("os.environ", {"HEAD_CHEF_GLOBAL_STATE": str(global_state)}):
                _append_outcome(local, outcome)
            self.assertEqual(json.loads(local.read_text(encoding="utf-8")), outcome)
            self.assertEqual(
                json.loads((global_state / "outcomes.jsonl").read_text(encoding="utf-8")),
                outcome,
            )

    def test_public_run_does_not_echo_packaged_prompt(self):
        run = RunRecord("job", "model", "chat", 1, True, prompt="private source")
        self.assertNotIn("prompt", _public_run(run))

    def test_public_job_does_not_echo_packaged_source(self):
        job = JobCard("j", ".", "task", context_text="private source")
        self.assertEqual(_public_job(job)["context_text"], "[stored in private job artifact]")

    def test_list_output_uses_contract_envelope(self):
        output = io.StringIO()
        with redirect_stdout(output):
            _json([{"name": "m"}])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["contract_version"], "head-chef.v2")
        self.assertEqual(parsed["data"][0]["name"], "m")

    def test_cook_parser_exposes_one_command_workflow(self):
        args = build_parser().parse_args([
            "cook", "--project", ".", "--task", "Analyze safely", "--acceptance", "Return result",
        ])
        self.assertEqual(args.command, "cook")
        self.assertEqual(args.acceptance, ["Return result"])

    def test_cook_parser_exposes_runtime_policy(self):
        args = build_parser().parse_args([
            "cook", "--project", ".", "--task", "Analyze safely",
            "--context-tokens", "16384", "--output-tokens", "4096", "--max-attempts", "3",
        ])
        self.assertEqual((args.context_tokens, args.output_tokens, args.max_attempts), (16384, 4096, 3))
        self.assertFalse(args.no_auto_split)

    def test_refresh_command_is_available(self):
        args = build_parser().parse_args(["refresh"])
        self.assertEqual(args.command, "refresh")
        self.assertFalse(args.no_evaluate)

    def test_refresh_lock_rejects_live_writer_and_recovers_stale_lock(self):
        from head_chef.cli import _acquire_refresh_lock

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "refresh.lock"
            path.write_text(f"{os.getpid()}\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                _acquire_refresh_lock(path)
            path.write_text("99999999\n", encoding="utf-8")
            descriptor = _acquire_refresh_lock(path)
            os.close(descriptor)

    def test_evaluated_strengths_are_digest_and_category_specific(self):
        from head_chef.cli import _evaluated_strengths

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "latest.json"
            path.write_text(json.dumps({"results": [
                {"model": "cook", "model_digest": "new", "category": "coding", "ok": True},
                {"model": "cook", "model_digest": "old", "category": "analysis", "ok": False},
            ]}), encoding="utf-8")
            strengths = _evaluated_strengths(path)
        self.assertIn(("cook", "new", "coding"), strengths)
        self.assertIn(("cook", "old", "analysis"), strengths)
        self.assertNotIn(("cook", "new", "analysis"), strengths)

    def test_cook_retries_then_creates_capability_checked_fallback_job(self):
        args = build_parser().parse_args([
            "cook", "--project", ".", "--task", "Analyze", "--max-attempts", "3",
        ])
        first = {
            "job": {"id": "j1", "selected_model": "primary", "fallback_models": ["fallback"]},
            "path": "j1.json", "child_job_paths": [],
        }
        second = {
            "job": {"id": "j2", "selected_model": "fallback", "fallback_models": []},
            "path": "j2.json", "child_job_paths": [],
        }
        calls = [
            (0, first),
            (1, None),
            (4, {"verification": {"status": "needs_revision"}}),
            (0, second),
            (0, {"verification": {"status": "accepted"}}),
        ]
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, \
             patch("head_chef.cli._captured_command", side_effect=calls) as captured, \
             patch("head_chef.cli.load_settings", return_value=(Settings(), Path(temp))), \
             patch("head_chef.cli.append_jsonl") as journal, \
             redirect_stdout(output):
            code = cmd_cook(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["job"]["id"], "j2")
        self.assertEqual(payload["recovery"][1]["next_model"], "fallback")
        self.assertEqual(captured.call_count, 5)
        journal.assert_called_once()

    def test_split_cook_dispatches_children_then_evidence_fed_synthesis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = ensure_state(root)
            child = JobCard("child", str(root), "part", selected_model="m", model_digest="d")
            synthesis = JobCard(
                "synth", str(root), "synthesize", selected_model="m", model_digest="d",
                context_limit_tokens=8192, output_limit_tokens=512, dependencies=["child"],
            )
            child_path = save_job_card(child, state / "jobs")
            synthesis_path = save_job_card(synthesis, state / "jobs")
            args = build_parser().parse_args(["cook", "--project", str(root), "--task", "task"])
            child_result = {
                "run_path": "run.json",
                "run": {"response": VALID_RESULT},
                "verification": {"status": "accepted"},
            }
            synthesis_result = {
                "run_path": "synthesis-run.json",
                "run": {"response": VALID_RESULT},
                "verification": {"status": "accepted"},
            }
            output = io.StringIO()
            with patch("head_chef.cli.load_settings", return_value=(Settings(), root)), \
                 patch("head_chef.cli._dispatch_saved_job", side_effect=[
                     (0, child_result, [{"attempt": 1, "exit_code": 0}]),
                     (0, synthesis_result, [{"attempt": 1, "exit_code": 0}]),
                 ]), redirect_stdout(output):
                code = _cook_split_jobs(
                    args,
                    {"job": {"id": "parent", "project": str(root)}},
                    [str(child_path), str(synthesis_path)],
                )
            payload = json.loads(output.getvalue())
            ready = load_job_card(Path(payload["synthesis_job_path"]))
        self.assertEqual(code, 0)
        self.assertIn('"job_id": "child"', ready.context_text)
        self.assertNotEqual(ready.id, "synth")

    def test_command_capture_keeps_json_contract_parseable(self):
        def command(_):
            _json({"value": 3})
            return 0

        code, payload = _captured_command(command, argparse.Namespace())
        self.assertEqual(code, 0)
        self.assertEqual(payload["value"], 3)

    def test_json_contract_escapes_unicode_for_windows_console(self):
        output = io.StringIO()
        with redirect_stdout(output):
            _json({"value": "step → rollback"})
        self.assertIn("\\u2192", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["value"], "step → rollback")

    def test_kitchen_assigns_only_matching_specialists(self):
        profiles = [
            ModelProfile("coder", capabilities={"coding"}),
            ModelProfile("vision", capabilities={"vision"}),
            ModelProfile("embed", capabilities={"embedding"}, notes=["embedding-only"]),
            ModelProfile("general", capabilities={"analysis", "planning", "writing", "retrieval"}),
        ]
        kitchen = build_kitchen(profiles)
        self.assertEqual(kitchen["stations"]["coding"]["model"], "coder")
        self.assertEqual(kitchen["stations"]["vision"]["model"], "vision")
        self.assertEqual(kitchen["stations"]["embedding"]["model"], "embed")
        self.assertEqual(kitchen["stations"]["image_generation"]["backend"], "comfyui")
        self.assertEqual(kitchen["stations"]["image_generation"]["model"], "sdxl-text-to-image")
        self.assertIsNone(kitchen["stations"]["video_generation"]["model"])

    def test_strength_benchmark_only_runs_advertised_category(self):
        class BenchClient(FakeClient):
            def embed(self, model, inputs, **kwargs):
                return OllamaResponse("", {"embeddings": [[0.1, 0.2, 0.3]]})

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "bench.json"
            output.write_text(json.dumps({"results": [{
                "model": "writer", "model_digest": "w1", "category": "writing", "score": 80,
            }]}), encoding="utf-8")
            payload = run_benchmarks(
                BenchClient(),
                [ModelProfile("embed", digest="abc", capabilities={"embedding"})],
                output,
                2,
                categories={"embedding", "coding"},
                assignments={"embed": {"embedding"}},
            )
            self.assertTrue(Path(payload["artifact_path"]).exists())
        self.assertEqual(len(payload["results"]), 2)
        self.assertFalse(payload["in_progress"])
        self.assertEqual(payload["completed_case_count"], 1)
        self.assertEqual(payload["total_case_count"], 1)
        embedding = next(item for item in payload["results"] if item["category"] == "embedding")
        self.assertEqual(embedding["model_digest"], "abc")

    def test_stale_digest_benchmark_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bench.json"
            path.write_text(json.dumps({
                "results": [
                    {"model": "a", "model_digest": "old", "category": "analysis", "score": 100, "ok": True, "suite_version": "s"},
                    {"model": "b", "model_digest": "b1", "category": "analysis", "score": 0, "ok": True, "suite_version": "s"},
                ],
                "benchmark": "s",
            }), encoding="utf-8")
            models = [
                ModelProfile("a", digest="new", capabilities={"analysis"}),
                ModelProfile("b", digest="b1", capabilities={"analysis"}),
            ]
            decision = route(RouteRequest("analyze", required_capability="analysis"), models, benchmark_path=path)
        a_reasons = next(item.reasons for item in decision.candidates if item.model == "a")
        self.assertFalse(any("strength" in reason for reason in a_reasons))

    def test_current_digest_failed_strength_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bench.json"
            path.write_text(json.dumps({
                "benchmark": "suite",
                "results": [{
                    "model": "bad", "model_digest": "d1", "category": "analysis",
                    "score": 0, "ok": False, "suite_version": "suite",
                }],
            }), encoding="utf-8")
            decision = route(
                RouteRequest("analyze", required_capability="analysis"),
                [
                    ModelProfile("bad", digest="d1", capabilities={"analysis"}),
                    ModelProfile("good", digest="d2", capabilities={"analysis"}),
                ],
                benchmark_path=path,
            )
        self.assertEqual(decision.selected_model, "good")
        failed = next(candidate for candidate in decision.candidates if candidate.model == "bad")
        self.assertTrue(failed.rejected)

    def test_equal_strength_prefers_materially_faster_model(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bench.json"
            path.write_text(json.dumps({
                "benchmark": "suite",
                "results": [
                    {"model": "small", "model_digest": "s", "category": "analysis", "score": 100,
                     "elapsed_seconds": 5, "ok": True, "suite_version": "suite"},
                    {"model": "large", "model_digest": "l", "category": "analysis", "score": 100,
                     "elapsed_seconds": 20, "ok": True, "suite_version": "suite"},
                ],
            }), encoding="utf-8")
            models = [
                ModelProfile("small", digest="s", capabilities={"analysis"}, parameter_billions=4),
                ModelProfile("large", digest="l", capabilities={"analysis"}, parameter_billions=26),
            ]
            decision = route(RouteRequest("analyze", required_capability="analysis"), models, benchmark_path=path)
        self.assertEqual(decision.selected_model, "small")

    def test_stale_digest_outcomes_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "outcomes.jsonl"
            path.write_text(json.dumps({
                "model": "a", "model_digest": "old", "category": "analysis",
                "ok": True, "elapsed_seconds": 1,
            }) + "\n", encoding="utf-8")
            decision = route(
                RouteRequest("analyze", required_capability="analysis"),
                [ModelProfile("a", digest="new", capabilities={"analysis"})],
                outcome_path=path,
            )
        reasons = decision.candidates[0].reasons
        self.assertFalse(any("observed" in reason for reason in reasons))

    def test_planning_eval_requires_exactly_three_bounded_steps(self):
        expected = (("validat",), ("test",), ("rollback", "revert"))
        good = json.dumps({
            "answer": "1. Validate schema. 2. Test failures. 3. Rollback safely.",
            "risks": [], "needs_review": True,
        })
        verbose = json.dumps({
            "answer": "1. Validate. 2. Test. 3. Rollback. 4. Add unrelated work.",
            "risks": [], "needs_review": True,
        })
        _, _, good_ok = _chat_score(good, expected, 1, {}, "planning")
        _, _, verbose_ok = _chat_score(verbose, expected, 1, {}, "planning")
        self.assertTrue(good_ok)
        self.assertFalse(verbose_ok)


if __name__ == "__main__":
    unittest.main()
