import tempfile
from pathlib import Path
import unittest

from head_chef.jobs import create_job_card, load_job_card, render_worker_prompt, save_job_card
from head_chef.router import RouteDecision


class JobCardTests(unittest.TestCase):
    def test_round_trip(self):
        decision = RouteDecision(
            category="coding",
            selected_model="qwen2.5-coder:7b",
            confidence=0.9,
            candidates=[],
            budget=None,
            requires_split=False,
            coordinator_review_required=True,
            explanation="test",
        )
        job = create_job_card(
            project="C:/Project",
            task="Add validation",
            decision=decision,
            allowed_files=["src/config.py"],
            acceptance_criteria=["Bad JSON returns an error"],
            context_text="def load_settings(): pass",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = save_job_card(job, Path(temp))
            loaded = load_job_card(path)
        self.assertEqual(loaded.task, job.task)
        self.assertEqual(loaded.allowed_files, ["src/config.py"])
        self.assertIn("load_settings", loaded.context_text)

    def test_prompt_contains_safety_boundary(self):
        decision = RouteDecision(
            category="analysis",
            selected_model="gemma4:12b",
            confidence=0.8,
            candidates=[],
            budget=None,
            requires_split=False,
            coordinator_review_required=True,
            explanation="test",
        )
        prompt = render_worker_prompt(create_job_card("x", "Review", decision))
        self.assertIn("Do not claim commands or tests were run", prompt)
        self.assertIn("RISKS, VERIFICATION NEEDED, and BLOCKERS", prompt)


if __name__ == "__main__":
    unittest.main()
