import json
from pathlib import Path
import tempfile
import unittest

from head_chef.context_packager import package_context, render_package, split_context
from head_chef.contracts import WORKER_OUTPUT_SCHEMA
from head_chef.executors import execute_job
from head_chef.jobs import JobCard
from head_chef.models import ModelProfile
from head_chef.models import infer_capabilities
from head_chef.ollama import OllamaResponse
from head_chef.registry import apply_overrides
from head_chef.cli import _json
from head_chef.kitchen import build_kitchen
from contextlib import redirect_stdout
import io
from head_chef.router import RouteRequest, route
from head_chef.runs import RunRecord, next_attempt, save_run
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
    def test_list_output_uses_contract_envelope(self):
        output = io.StringIO()
        with redirect_stdout(output):
            _json([{"name": "m"}])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["contract_version"], "head-chef.v2")
        self.assertEqual(parsed["data"][0]["name"], "m")

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


if __name__ == "__main__":
    unittest.main()
