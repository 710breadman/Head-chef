import json
from pathlib import Path
import tempfile
import unittest

from head_chef.context_packager import package_context, render_package, split_context
from head_chef.contracts import WORKER_OUTPUT_SCHEMA
from head_chef.executors import execute_job
from head_chef.jobs import JobCard
from head_chef.models import ModelProfile
from head_chef.ollama import OllamaResponse
from head_chef.registry import apply_overrides
from head_chef.cli import _json
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


class ContractTests(unittest.TestCase):
    def test_list_output_uses_contract_envelope(self):
        output = io.StringIO()
        with redirect_stdout(output):
            _json([{"name": "m"}])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["contract_version"], "head-chef.v2")
        self.assertEqual(parsed["data"][0]["name"], "m")


if __name__ == "__main__":
    unittest.main()
