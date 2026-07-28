import json
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import redirect_stdout
import io
from unittest.mock import patch

from head_chef.cli import (
    _assignment_changes,
    _existing_plan_files,
    build_parser,
    cmd_new_cooks,
    cmd_orchestrate,
    cmd_sprint_check,
    cmd_work,
)
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

    def test_discovers_nested_roadmap_outline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outline = root / "product" / "planning" / "ROADMAP.json"
            outline.parent.mkdir(parents=True)
            outline.write_text(json.dumps({"tasks": []}), encoding="utf-8")

            found = discover_sprint_file(root)

        self.assertEqual(found, outline.resolve())

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
        self.assertNotIn("image_generation", supporting)
        self.assertNotIn("video_generation", supporting)

    def test_classifies_image_generation_task(self):
        task = {
            "title": "Create hero concept art",
            "objective": "Produce sprite art and an icon for the new hero character",
            "acceptance_criteria": ["Matches art brief"],
            "expected_files": ["art/hero-sprite.png"],
        }
        primary, supporting = infer_stations(task)
        self.assertEqual(primary, "image_generation")
        self.assertIn("analysis", supporting)

    def test_classifies_video_generation_task(self):
        task = {
            "title": "Produce launch trailer",
            "objective": "Generate a short cutscene trailer for the release announcement",
            "acceptance_criteria": ["Trailer is under 30 seconds"],
            "expected_files": [],
        }
        primary, supporting = infer_stations(task)
        self.assertEqual(primary, "video_generation")

    def test_vision_inspection_stays_distinct_from_generation(self):
        task = {
            "title": "Inspect UI screenshot",
            "objective": "Inspect the screenshot for visual defects before release",
            "acceptance_criteria": ["No layout defects"],
            "expected_files": [],
        }
        primary, supporting = infer_stations(task)
        self.assertEqual(primary, "vision")
        self.assertNotIn("image_generation", supporting)
        self.assertNotIn("video_generation", supporting)

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

    def test_plan_assigns_approved_comfyui_template_for_image_generation(self):
        task = {
            "id": "IMG-1", "title": "Create hero concept art",
            "objective": "Produce sprite art and an icon for the new hero character",
            "phase": "art", "status": "ready", "dependencies": [],
            "acceptance_criteria": ["Matches art brief"], "expected_files": [], "evidence": [],
        }
        plan = build_sprint_plan([task], [])
        assignment = plan["tasks"][0]["primary_assignment"]
        self.assertEqual(assignment["station"], "image_generation")
        self.assertEqual(assignment["backend"], "comfyui")
        self.assertEqual(assignment["status"], "assigned")
        self.assertEqual(assignment["model"], "sdxl-text-to-image")
        self.assertTrue(assignment["review_required"])

    def test_plan_leaves_video_generation_unfilled_without_approved_template(self):
        task = {
            "id": "VID-1", "title": "Produce launch trailer",
            "objective": "Generate a short cutscene trailer for the release announcement",
            "phase": "art", "status": "ready", "dependencies": [],
            "acceptance_criteria": ["Trailer is under 30 seconds"], "expected_files": [], "evidence": [],
        }
        plan = build_sprint_plan([task], [])
        assignment = plan["tasks"][0]["primary_assignment"]
        self.assertEqual(assignment["station"], "video_generation")
        self.assertEqual(assignment["backend"], "comfyui")
        self.assertEqual(assignment["status"], "unfilled")
        self.assertIsNone(assignment["model"])

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

    def test_sprint_check_reports_existing_outline_and_plans(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outline = root / "SPRINTS.json"
            outline.write_text(json.dumps({"tasks": []}), encoding="utf-8")
            args = build_parser().parse_args([
                "sprint-check", "--project", str(root), "--no-local-analysis",
            ])
            output = io.StringIO()
            with patch(
                "head_chef.cli.sprint_commands._orchestrate_core",
                return_value=(0, {"status": "planned", "task_count": 0}),
            ) as captured, redirect_stdout(output):
                code = cmd_sprint_check(args)
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["outline_status"], "found")
        self.assertTrue(payload["planned"])
        self.assertEqual(captured.call_args.args[0].sprint_file, "SPRINTS.json")

    def test_skill_check_finds_nested_roadmap_outline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outline = root / "packages" / "app" / "planning" / "roadmap.json"
            outline.parent.mkdir(parents=True)
            outline.write_text(json.dumps({"tasks": []}), encoding="utf-8")
            args = build_parser().parse_args([
                "skill-check", "--project", str(root), "--no-local-analysis",
            ])
            output = io.StringIO()
            with patch(
                "head_chef.cli.sprint_commands._orchestrate_core",
                return_value=(0, {"status": "planned", "task_count": 0}),
            ) as captured, redirect_stdout(output):
                code = cmd_sprint_check(args)
            payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["outline_status"], "found")
        self.assertEqual(
            captured.call_args.args[0].sprint_file,
            "packages/app/planning/roadmap.json",
        )

    def test_skill_check_alias_invokes_sprint_check(self):
        args = build_parser().parse_args(["skill-check", "--project", "C:/project"])
        self.assertIs(args.func, cmd_sprint_check)

    def test_sprint_check_prompts_and_creates_missing_outline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = build_parser().parse_args(["sprint-check", "--project", str(root)])
            output = io.StringIO()
            with patch("builtins.input", return_value="yes"), patch(
                "head_chef.cli.sprint_commands._orchestrate_core",
                return_value=(0, {"status": "planned", "task_count": 0}),
            ), redirect_stdout(output):
                code = cmd_sprint_check(args)
            payload = json.loads(output.getvalue())
            outline = json.loads((root / "sprints" / "SPRINTS.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["outline_status"], "created")
        self.assertEqual(outline["tasks"], [])

    def test_sprint_check_can_decline_missing_outline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = build_parser().parse_args(["sprint-check", "--project", str(root)])
            output = io.StringIO()
            with patch("builtins.input", return_value="no"), redirect_stdout(output):
                code = cmd_sprint_check(args)
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "outline_missing")

    def test_reassign_accepts_custom_model_roster(self):
        args = build_parser().parse_args([
            "reassign", "--project", "C:/project", "--model", "coder", "--model", "reviewer",
        ])
        self.assertEqual(args.model, ["coder", "reviewer"])
        self.assertFalse(args.apply_roster)
        self.assertIs(args.func, cmd_orchestrate)

    def test_assignment_diff_detects_model_digest_and_station_changes(self):
        previous = {"tasks": [{
            "id": "T-1",
            "primary_assignment": {
                "station": "coding", "model": "old", "model_digest": "d1", "status": "assigned",
            },
        }]}
        current = {"tasks": [{
            "id": "T-1",
            "primary_assignment": {
                "station": "coding", "model": "new", "model_digest": "d2", "status": "assigned",
            },
        }]}
        changes = _assignment_changes(previous, current)
        self.assertEqual(changes[0]["change"], "reassigned")
        self.assertEqual(changes[0]["before"]["model"], "old")
        self.assertEqual(changes[0]["after"]["model_digest"], "d2")

    def test_new_cooks_runs_refresh_then_reassignment(self):
        args = build_parser().parse_args([
            "new-cooks", "--project", "C:/project", "--model", "new-coder",
        ])
        output = io.StringIO()
        with patch(
            "head_chef.cli.roster_commands._refresh_core",
            return_value=(0, {"status": "refreshed", "changes": {"new": ["new-coder"]}}),
        ) as refresh_core, patch(
            "head_chef.cli.roster_commands._orchestrate_core",
            return_value=(0, {"status": "planned", "assignment_change_count": 3}),
        ) as orchestrate_core, redirect_stdout(output):
            code = cmd_new_cooks(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(refresh_core.call_args.args[0].model, ["new-coder"])
        self.assertEqual(orchestrate_core.call_args.args[0].model, [])

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
            with patch("head_chef.cli.sprint_commands._cook_core", return_value=(0, {"status": "completed"})) as captured, \
                 redirect_stdout(output):
                code = cmd_work(args)
            cook_args = captured.call_args.args[0]
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(cook_args.model, "coder")
        self.assertEqual(cook_args.allowed_file, ["src/a.py"])
        self.assertEqual(payload["task_count"], 1)

    def test_work_parallel_dispatch_preserves_result_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = ensure_state(root)
            plan_path = state / "orchestration" / "sprint-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps({"tasks": [
                {
                    "id": "T-1", "title": "First", "objective": "Do first",
                    "actionable": True, "dependencies": [], "evidence": [],
                    "acceptance_criteria": ["works"], "expected_files": [],
                    "primary_assignment": {"station": "coding", "model": "coder-a", "status": "assigned"},
                },
                {
                    "id": "T-2", "title": "Second", "objective": "Do second",
                    "actionable": True, "dependencies": [], "evidence": [],
                    "acceptance_criteria": ["works"], "expected_files": [],
                    "primary_assignment": {"station": "coding", "model": "coder-b", "status": "assigned"},
                },
            ]}), encoding="utf-8")
            args = build_parser().parse_args([
                "work", "--project", str(root), "--all-ready", "--parallel", "2",
            ])

            # The first task's dispatch takes longer than the second's, so if the pool did not
            # preserve submission order in the output (e.g. by naively collecting whichever
            # future finished first), T-2 would end up before T-1 in the results.
            def slower_for_first_task(cook_args):
                if cook_args.model == "coder-a":
                    time.sleep(0.05)
                return 0, {"status": "completed", "model": cook_args.model}

            output = io.StringIO()
            with patch(
                "head_chef.cli.sprint_commands._cook_core", side_effect=slower_for_first_task,
            ) as cook_core, redirect_stdout(output):
                code = cmd_work(args)
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(cook_core.call_count, 2)
        self.assertEqual([item["task_id"] for item in payload["results"]], ["T-1", "T-2"])
        self.assertEqual([item["model"] for item in payload["results"]], ["coder-a", "coder-b"])

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
            with patch("head_chef.cli.sprint_commands._cook_core", return_value=(0, {"status": "completed"})) as captured, \
                 redirect_stdout(output):
                code = cmd_work(args)
            cook_args = captured.call_args.args[0]
        self.assertEqual(code, 0)
        self.assertEqual(cook_args.acceptance, [
            "Provide concrete bounded guidance for coordinator action",
            "Map guidance to every sprint acceptance criterion",
        ])

    def test_coordinator_acceptance_unlocks_dependency_with_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = ensure_state(root)
            plan_path = state / "orchestration" / "sprint-plan.json"
            plan_path.parent.mkdir(parents=True)
            assignment = {
                "station": "coding", "model": "coder", "status": "assigned",
                "review_required": True,
            }
            plan_path.write_text(json.dumps({"tasks": [
                {
                    "id": "T-1", "title": "First", "objective": "Implement first",
                    "status": "ready", "actionable": True, "dependencies": [],
                    "evidence": [], "acceptance_criteria": ["first works"],
                    "expected_files": [], "primary_assignment": assignment,
                },
                {
                    "id": "T-2", "title": "Second", "objective": "Use first",
                    "status": "ready", "actionable": False, "dependencies": ["T-1"],
                    "evidence": [], "acceptance_criteria": ["second works"],
                    "expected_files": [], "primary_assignment": assignment,
                },
            ]}), encoding="utf-8")
            primary_payload = {
                "run": {
                    "run_id": "run-one",
                    "response": {"result": "Implemented bounded first-step proposal."},
                },
            }
            first_args = build_parser().parse_args([
                "work", "--project", str(root), "--task-id", "T-1",
            ])
            with patch("head_chef.cli.sprint_commands._cook_core", return_value=(0, primary_payload)), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(cmd_work(first_args), 0)
            ledger = json.loads(
                (state / "orchestration" / "work-ledger.json").read_text(encoding="utf-8")
            )
            self.assertEqual(ledger["tasks"]["T-1"]["status"], "coordinator_review_required")

            second_args = build_parser().parse_args([
                "work", "--project", str(root), "--accept-task", "T-1",
            ])
            output = io.StringIO()
            with patch("head_chef.cli.sprint_commands._cook_core", return_value=(0, primary_payload)) as captured, \
                 redirect_stdout(output):
                self.assertEqual(cmd_work(second_args), 0)
            cook_args = captured.call_args.args[0]
            payload = json.loads(output.getvalue())
        self.assertTrue(any(
            note.startswith("Accepted dependency T-1 run run-one")
            for note in cook_args.context_note
        ))
        self.assertEqual(payload["results"][0]["task_id"], "T-2")

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
            with patch("head_chef.cli.sprint_commands._cook_core") as captured, redirect_stdout(output):
                code = cmd_work(args)
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["results"][0]["status"], "coordinator_required")
        captured.assert_not_called()

    def test_work_dispatches_image_generation_task_to_visual_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = ensure_state(root)
            plan_path = state / "orchestration" / "sprint-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps({"tasks": [{
                "id": "IMG-1", "title": "Create hero art", "objective": "Generate concept art for the hero",
                "actionable": True, "acceptance_criteria": ["Matches brief"], "expected_files": [],
                "dependencies": [], "evidence": [],
                "primary_assignment": {
                    "station": "image_generation", "model": "sdxl-text-to-image",
                    "status": "assigned", "backend": "comfyui",
                },
            }]}), encoding="utf-8")
            args = build_parser().parse_args(["work", "--project", str(root)])
            output = io.StringIO()
            with patch(
                "head_chef.cli.sprint_commands._dispatch_visual_task",
                return_value=(0, {"status": "coordinator_review_required"}),
            ) as dispatch, redirect_stdout(output):
                code = cmd_work(args)
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        dispatch.assert_called_once()
        self.assertEqual(payload["results"][0]["category"], "image_generation")
        self.assertEqual(payload["results"][0]["result"]["status"], "coordinator_review_required")

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
            with patch("head_chef.cli.sprint_commands._cook_core") as captured, redirect_stdout(output):
                code = cmd_work(args)
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["results"][0]["status"], "input_required")
        captured.assert_not_called()

    def test_work_dispatches_through_station_disagreement_but_flags_it(self):
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
            with patch(
                "head_chef.cli.sprint_commands._cook_core", return_value=(0, {"status": "completed"}),
            ) as cook_core, redirect_stdout(output):
                code = cmd_work(args)
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        cook_core.assert_called_once()
        disagreement = payload["results"][0]["station_disagreement"]
        self.assertEqual(disagreement["deterministic_station"], "analysis")
        self.assertEqual(disagreement["local_recommended_station"], "coding")


if __name__ == "__main__":
    unittest.main()
