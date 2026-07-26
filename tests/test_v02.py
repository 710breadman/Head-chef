import argparse
import json
from pathlib import Path
import tempfile
import unittest

from head_chef.context_packager import package_context, render_package, split_context
from head_chef.config import Settings, resolve_state_dir, write_default_config
from head_chef.contracts import WORKER_OUTPUT_SCHEMA
from head_chef.executors import execute_job
from head_chef.jobs import JobCard
from head_chef.models import ModelProfile
from head_chef.models import infer_capabilities
from head_chef.ollama import OllamaClient, OllamaResponse
from head_chef.registry import apply_overrides
from head_chef.cli import _captured_command, _json, _public_run, build_parser
from head_chef.kitchen import build_kitchen
from head_chef.benchmark import _chat_score, benchmark_model, run_benchmarks
from contextlib import redirect_stdout
import io
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
    def test_public_run_does_not_echo_packaged_prompt(self):
        run = RunRecord("job", "model", "chat", 1, True, prompt="private source")
        self.assertNotIn("prompt", _public_run(run))

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
