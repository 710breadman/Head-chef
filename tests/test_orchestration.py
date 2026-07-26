import json
from pathlib import Path
import tempfile
import unittest

from head_chef.cli import build_parser
from head_chef.models import ModelProfile
from head_chef.ollama import OllamaResponse
from head_chef.orchestration import (
    add_local_phase_analysis,
    build_sprint_plan,
    discover_sprint_file,
    infer_stations,
    load_sprint_tasks,
)


class FakePlanningClient:
    def chat(self, model, messages, **kwargs):
        self.kwargs = kwargs
        self.options = kwargs["options"]
        supplied = json.loads(messages[0]["content"].split("\n\n", 1)[1])
        return OllamaResponse(json.dumps({
            "phase_summary": f"{supplied['phase']} needs bounded execution.",
            "task_notes": [
                {"id": task["id"], "needs": ["acceptance evidence"], "risks": ["scope drift"]}
                for task in supplied["tasks"]
            ],
        }), {})


class IncompletePlanningClient:
    def chat(self, model, messages, **kwargs):
        return OllamaResponse(json.dumps({"phase_summary": "partial", "task_notes": []}), {})


class SprintOrchestrationTests(unittest.TestCase):
    def test_discovers_manifest_and_merges_task_contracts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "sprints" / "tasks").mkdir(parents=True)
            manifest = root / "sprints" / "SPRINTS.json"
            manifest.write_text(json.dumps({
                "task_contracts": "sprints/tasks/*.json",
                "tasks": [{
                    "id": "T-1", "title": "Visual room", "phase": "p1",
                    "status": "ready", "dependencies": [],
                }],
            }), encoding="utf-8")
            (root / "sprints" / "tasks" / "p1.json").write_text(json.dumps({
                "phase": "p1",
                "tasks": [{
                    "id": "T-1",
                    "objective": "Build a room and capture a screenshot",
                    "acceptance_criteria": ["Scene loads"],
                    "expected_files": ["game/room.gd"],
                }],
            }), encoding="utf-8")
            found = discover_sprint_file(root)
            tasks, sources = load_sprint_tasks(root, found)
        self.assertEqual(found, manifest.resolve())
        self.assertEqual(tasks[0]["objective"], "Build a room and capture a screenshot")
        self.assertEqual(tasks[0]["status"], "ready")
        self.assertEqual(len(sources), 2)

    def test_assigns_primary_and_supporting_stations(self):
        task = {
            "title": "Build sprite room",
            "objective": "Implement a pixel sprite and capture screenshots",
            "acceptance_criteria": ["Scene loads", "Screenshot is crisp"],
            "expected_files": ["game/room.gd"],
        }
        primary, supporting = infer_stations(task)
        self.assertEqual(primary, "coding")
        self.assertIn("vision", supporting)
        self.assertIn("analysis", supporting)

    def test_startup_does_not_false_match_art_role(self):
        task = {
            "title": "Startup smoke test",
            "objective": "Fail on startup errors",
            "acceptance_criteria": ["Headless run succeeds"],
            "expected_files": ["tools/validate.ps1"],
        }
        primary, supporting = infer_stations(task)
        self.assertEqual(primary, "coding")
        self.assertNotIn("vision", supporting)

    def test_plan_marks_dependency_ready_and_assigns_models(self):
        profiles = [
            ModelProfile("coder", capabilities={"coding"}),
            ModelProfile("planner", capabilities={"planning", "analysis", "vision"}),
        ]
        tasks = [
            {
                "id": "T-0", "title": "Done", "objective": "Done", "phase": "p",
                "status": "done", "dependencies": [], "acceptance_criteria": [],
                "expected_files": [], "evidence": [],
            },
            {
                "id": "T-1", "title": "Implement script", "objective": "Implement code", "phase": "p",
                "status": "ready", "dependencies": ["T-0"], "acceptance_criteria": [],
                "expected_files": ["src/a.py"], "evidence": [],
            },
        ]
        plan = build_sprint_plan(tasks, profiles)
        self.assertEqual(plan["actionable_task_ids"], ["T-1"])
        self.assertEqual(plan["tasks"][1]["primary_assignment"]["model"], "coder")

    def test_local_analysis_covers_each_phase(self):
        plan = {
            "tasks": [{
                "id": "T-1", "title": "Task", "objective": "Do task", "phase": "p1",
                "status": "ready", "dependencies": [], "acceptance_criteria": ["works"],
                "expected_files": ["a.py"],
            }],
        }
        client = FakePlanningClient()
        add_local_phase_analysis(plan, client, "planner", timeout_seconds=2)
        self.assertEqual(plan["local_phase_analysis"]["errors"], [])
        self.assertTrue(plan["local_phase_analysis"]["advisory_only"])
        self.assertEqual(plan["local_phase_analysis"]["reviews"][0]["task_notes"][0]["id"], "T-1")
        self.assertEqual(client.options["temperature"], 0.1)
        self.assertNotIn("think", client.kwargs)

    def test_local_analysis_rejects_incomplete_phase_coverage(self):
        plan = {
            "tasks": [{
                "id": "T-1", "title": "Task", "objective": "Do task", "phase": "p1",
                "status": "ready", "dependencies": [], "acceptance_criteria": ["works"],
                "expected_files": ["a.py"],
            }],
        }
        add_local_phase_analysis(plan, IncompletePlanningClient(), "planner", timeout_seconds=2)
        self.assertEqual(plan["local_phase_analysis"]["reviews"], [])
        self.assertIn("every task", plan["local_phase_analysis"]["errors"][0])

    def test_parser_exposes_orchestration_command(self):
        args = build_parser().parse_args(["orchestrate", "--project", "C:/project"])
        self.assertEqual(args.command, "orchestrate")
        self.assertFalse(args.no_local_analysis)


if __name__ == "__main__":
    unittest.main()
