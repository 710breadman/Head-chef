import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from unittest.mock import patch

from head_chef.cli import _existing_plan_files, build_parser, cmd_work
from head_chef.storage import ensure_state
from head_chef.models import ModelProfile
from head_chef.ollama import OllamaResponse
from head_chef.orchestration import (
    add_local_phase_analysis,
    build_sprint_plan,
    discover_sprint_file,
    infer_stations,
    load_sprint_tasks,
    profile_sprint_task,
)


class FakePlanningClient:
    def chat(self, model, messages, **kwargs):
        self.kwargs = kwargs
        self.options = kwargs["options"]
        supplied = json.loads(messages[0]["content"].split("\n\n", 1)[1])
        return OllamaResponse(json.dumps({
            "phase_summary": f"{supplied['phase']} needs bounded execution.",
            "task_notes": [
                {
                    "id": task["id"],
                    "needs": ["acceptance evidence"],
                    "risks": ["scope drift"],
                    "recommended_primary_station": "coding",
                    "recommended_supporting_stations": ["analysis"],
                    "local_scope": "full",
                }
                for task in supplied["tasks"]
            ],
        }), {})


class IncompletePlanningClient:
    def chat(self, model, messages, **kwargs):
        return OllamaResponse(json.dumps({"phase_summary": "partial", "task_notes": []}), {})


class InvalidStationPlanningClient:
    def chat(self, model, messages, **kwargs):
        supplied = json.loads(messages[0]["content"].split("\n\n", 1)[1])
        return OllamaResponse(json.dumps({
            "phase_summary": "invalid",
            "task_notes": [{
                "id": supplied["tasks"][0]["id"],
                "needs": [],
                "risks": [],
                "recommended_primary_station": "coding",
                "recommended_supporting_stations": ["coding", "coding"],
                "local_scope": "full",
            }],
        }), {})


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

    def test_command_execution_task_is_local_advisory(self):
        task = {
            "title": "Headless smoke test",
            "objective": "Run the project and fail on startup errors",
            "acceptance_criteria": ["Headless run exits successfully"],
            "expected_files": ["tools/validate.ps1"],
        }
        profile = profile_sprint_task(task)
        self.assertEqual(profile["primary_station"], "coding")
        self.assertEqual(profile["local_scope"], "advisory")
        self.assertEqual(profile["required_tools"], ["shell"])
        self.assertTrue(profile["coordinator_action_required"])

    def test_owner_license_decision_abstains_from_primary_model(self):
        task = {
            "title": "Select license",
            "objective": "Choose license for public release",
            "acceptance_criteria": ["Owner approves license"],
            "expected_files": ["LICENSE"],
        }
        primary, supporting = infer_stations(task)
        profile = profile_sprint_task(task)
        self.assertIsNone(primary)
        self.assertIn("analysis", supporting)
        self.assertEqual(profile["local_scope"], "none")
        self.assertEqual(profile["risk"], "high")

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
        self.assertEqual(plan["tasks"][1]["primary_assignment"]["status"], "assigned")
        self.assertIn("score_evidence", plan["tasks"][1]["primary_assignment"])

    def test_deterministic_policy_repeats_exact_assignment(self):
        task = {
            "id": "T-1", "title": "Implement script", "objective": "Implement code",
            "phase": "p", "status": "ready", "dependencies": [],
            "acceptance_criteria": ["works"], "expected_files": ["src/a.py"], "evidence": [],
        }
        profiles = [
            ModelProfile("coder", digest="c1", capabilities={"coding"}),
            ModelProfile("analyst", digest="a1", capabilities={"analysis"}),
        ]
        first = build_sprint_plan([task], profiles)
        second = build_sprint_plan([task], profiles)
        self.assertEqual(first, second)

    def test_plan_preserves_explicit_abstention(self):
        task = {
            "id": "L-1", "title": "Select license", "objective": "Choose license",
            "phase": "release", "status": "ready", "dependencies": [],
            "acceptance_criteria": ["Owner approves"], "expected_files": ["LICENSE"], "evidence": [],
        }
        plan = build_sprint_plan(
            [task],
            [ModelProfile("analyst", capabilities={"analysis"})],
        )
        assignment = plan["tasks"][0]["primary_assignment"]
        self.assertEqual(assignment["status"], "abstained")
        self.assertIsNone(assignment["model"])
        self.assertEqual(plan["tasks"][0]["task_profile"]["local_scope"], "none")

    def test_local_analysis_covers_each_phase(self):
        plan = {
            "tasks": [{
                "id": "T-1", "title": "Task", "objective": "Do task", "phase": "p1",
                "status": "ready", "dependencies": [], "acceptance_criteria": ["works"],
                "expected_files": ["a.py"],
                "task_profile": {"local_scope": "full"},
                "primary_assignment": {"station": "coding"},
            }],
        }
        client = FakePlanningClient()
        add_local_phase_analysis(plan, client, "planner", timeout_seconds=2)
        self.assertEqual(plan["local_phase_analysis"]["errors"], [])
        self.assertTrue(plan["local_phase_analysis"]["advisory_only"])
        self.assertEqual(plan["local_phase_analysis"]["reviews"][0]["task_notes"][0]["id"], "T-1")
        self.assertEqual(client.options["temperature"], 0)
        self.assertIs(client.kwargs["think"], False)
        self.assertTrue(plan["tasks"][0]["local_assignment_review"]["agrees_with_deterministic_profile"])
        self.assertEqual(plan["local_phase_analysis"]["assignment_disagreement_count"], 0)
        self.assertEqual(plan["local_phase_analysis"]["station_disagreement_count"], 0)
        self.assertEqual(plan["local_phase_analysis"]["scope_disagreement_count"], 0)

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

    def test_local_analysis_rejects_invalid_station_topology(self):
        plan = {
            "tasks": [{
                "id": "T-1", "title": "Task", "objective": "Do task", "phase": "p1",
                "status": "ready", "dependencies": [], "acceptance_criteria": [],
                "expected_files": [],
            }],
        }
        add_local_phase_analysis(plan, InvalidStationPlanningClient(), "planner", timeout_seconds=2)
        self.assertEqual(plan["local_phase_analysis"]["reviews"], [])
        self.assertIn("invalid task note shape", plan["local_phase_analysis"]["errors"][0])

    def test_local_assignment_disagreement_is_visible_but_advisory(self):
        plan = {
            "tasks": [{
                "id": "T-1", "title": "Task", "objective": "Do task", "phase": "p1",
                "status": "ready", "dependencies": [], "acceptance_criteria": ["works"],
                "expected_files": [], "task_profile": {"local_scope": "full"},
                "primary_assignment": {"station": "planning"},
            }],
        }
        add_local_phase_analysis(plan, FakePlanningClient(), "reviewer", timeout_seconds=2)
        review = plan["tasks"][0]["local_assignment_review"]
        self.assertFalse(review["agrees_with_deterministic_profile"])
        self.assertFalse(review["station_agrees"])
        self.assertTrue(review["scope_agrees"])
        self.assertTrue(review["advisory_only"])
        self.assertTrue(plan["tasks"][0]["assignment_review_required"])
        self.assertEqual(plan["local_phase_analysis"]["assignment_disagreement_count"], 1)
        self.assertEqual(plan["local_phase_analysis"]["station_disagreement_count"], 1)

    def test_parser_exposes_orchestration_command(self):
        args = build_parser().parse_args(["orchestrate", "--project", "C:/project"])
        self.assertEqual(args.command, "orchestrate")
        self.assertFalse(args.no_local_analysis)

    def test_work_dispatches_ready_task_to_preassigned_local_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
            state = ensure_state(root)
            plan_path = state / "orchestration" / "sprint-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps({"tasks": [{
                "id": "T-1", "title": "Implement", "objective": "Change code",
                "actionable": True, "dependencies": [], "evidence": [],
                "acceptance_criteria": ["works"],
                "expected_files": ["src/a.py", "../outside", "src"],
                "primary_assignment": {"station": "coding", "model": "coder"},
            }]}), encoding="utf-8")
            args = build_parser().parse_args(["work", "--project", str(root)])
            output = io.StringIO()
            with patch("head_chef.cli._captured_command", return_value=(0, {"status": "completed"})) as captured, \
                 redirect_stdout(output):
                code = cmd_work(args)
            cook_args = captured.call_args.args[1]
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(cook_args.model, "coder")
        self.assertEqual(cook_args.allowed_file, ["src/a.py"])
        self.assertEqual(payload["task_count"], 1)

    def test_work_converts_advisory_task_to_honest_guidance_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = ensure_state(root)
            plan_path = state / "orchestration" / "sprint-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps({"tasks": [{
                "id": "A-1", "title": "Validate", "objective": "Run the application",
                "actionable": True, "dependencies": [], "evidence": [],
                "acceptance_criteria": ["Application launches"],
                "expected_files": [],
                "task_profile": {"local_scope": "advisory"},
                "primary_assignment": {
                    "station": "analysis", "model": "analyst", "status": "assigned",
                },
            }]}), encoding="utf-8")
            args = build_parser().parse_args(["work", "--project", str(root)])
            output = io.StringIO()
            with patch("head_chef.cli._captured_command", return_value=(0, {"status": "completed"})) as captured, \
                 redirect_stdout(output):
                code = cmd_work(args)
            cook_args = captured.call_args.args[1]
        self.assertEqual(code, 0)
        self.assertEqual(cook_args.acceptance, [
            "Provide concrete bounded guidance for coordinator action",
            "Map guidance to every sprint acceptance criterion",
        ])

    def test_plan_files_reject_escape_and_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ok.txt").write_text("ok", encoding="utf-8")
            (root / "folder").mkdir()
            self.assertEqual(
                _existing_plan_files(root, ["ok.txt", "../escape", "folder", "C:/absolute"]),
                ["ok.txt"],
            )

    def test_work_does_not_dispatch_abstained_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = ensure_state(root)
            plan_path = state / "orchestration" / "sprint-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps({"tasks": [{
                "id": "L-1", "title": "License", "objective": "Choose",
                "actionable": True, "acceptance_criteria": [], "expected_files": [],
                "dependencies": [], "evidence": [],
                "primary_assignment": {
                    "station": None, "model": None, "status": "abstained",
                    "reason": "Owner decision",
                },
            }]}), encoding="utf-8")
            args = build_parser().parse_args(["work", "--project", str(root)])
            output = io.StringIO()
            with patch("head_chef.cli._captured_command") as captured, redirect_stdout(output):
                code = cmd_work(args)
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["results"][0]["status"], "coordinator_required")
        captured.assert_not_called()

    def test_work_waits_for_required_vision_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = ensure_state(root)
            plan_path = state / "orchestration" / "sprint-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps({"tasks": [{
                "id": "V-1", "title": "Inspect", "objective": "Inspect image",
                "actionable": True, "acceptance_criteria": [], "expected_files": [],
                "dependencies": [], "evidence": [],
                "primary_assignment": {
                    "station": "vision", "model": "vl", "status": "conditional",
                    "required_inputs": ["image"],
                },
            }]}), encoding="utf-8")
            args = build_parser().parse_args(["work", "--project", str(root)])
            output = io.StringIO()
            with patch("head_chef.cli._captured_command") as captured, redirect_stdout(output):
                code = cmd_work(args)
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["results"][0]["status"], "input_required")
        captured.assert_not_called()

    def test_work_stops_on_station_disagreement_until_reviewed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = ensure_state(root)
            plan_path = state / "orchestration" / "sprint-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps({"tasks": [{
                "id": "D-1", "title": "Task", "objective": "Do",
                "actionable": True, "acceptance_criteria": [], "expected_files": [],
                "dependencies": [], "evidence": [],
                "primary_assignment": {
                    "station": "analysis", "model": "analyst", "status": "assigned",
                },
                "local_assignment_review": {
                    "station_agrees": False,
                    "recommended_primary_station": "coding",
                },
            }]}), encoding="utf-8")
            args = build_parser().parse_args(["work", "--project", str(root)])
            output = io.StringIO()
            with patch("head_chef.cli._captured_command") as captured, redirect_stdout(output):
                code = cmd_work(args)
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["results"][0]["status"], "assignment_review_required")
        captured.assert_not_called()


if __name__ == "__main__":
    unittest.main()
