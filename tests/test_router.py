import unittest

from head_chef.models import ModelProfile
from head_chef.router import RouteRequest, classify_task, route


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.models = [
            ModelProfile(
                name="qwen2.5-coder:7b",
                parameter_size="7B",
                parameter_billions=7,
                context_tokens=32768,
                capabilities={"analysis", "coding", "planning"},
                notes=["coding-specialist"],
            ),
            ModelProfile(
                name="gemma4:26b",
                parameter_size="26B",
                parameter_billions=26,
                context_tokens=16384,
                capabilities={"analysis", "planning", "writing", "retrieval"},
            ),
            ModelProfile(
                name="qwen3-vl:8b-instruct",
                parameter_size="8B",
                parameter_billions=8,
                context_tokens=32768,
                capabilities={"analysis", "vision", "planning"},
                notes=["vision-capable-name"],
            ),
            ModelProfile(
                name="qwen3-embedding:0.6b",
                parameter_size="0.6B",
                parameter_billions=0.6,
                capabilities={"embedding"},
                notes=["embedding-only"],
            ),
        ]

    def test_classifies_coding(self):
        self.assertEqual(classify_task("Fix this Python function and add a test"), "coding")

    def test_routes_code_to_coder(self):
        decision = route(RouteRequest(task="Fix a Python bug"), self.models)
        self.assertEqual(decision.selected_model, "qwen2.5-coder:7b")

    def test_routes_vision_to_vl(self):
        decision = route(RouteRequest(task="Inspect this screenshot for visual defects"), self.models)
        self.assertEqual(decision.selected_model, "qwen3-vl:8b-instruct")

    def test_large_general_task_prefers_large_model(self):
        decision = route(RouteRequest(task="Analyze the architecture and recommend a roadmap"), self.models)
        self.assertEqual(decision.selected_model, "gemma4:26b")

    def test_rejects_embedding_manual_override_for_generation(self):
        decision = route(
            RouteRequest(task="Write a design", manual_model="qwen3-embedding:0.6b"),
            self.models,
        )
        self.assertIsNone(decision.selected_model)


if __name__ == "__main__":
    unittest.main()
