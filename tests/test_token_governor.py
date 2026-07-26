import unittest

from head_chef.token_governor import estimate_tokens, evaluate_budget


class TokenGovernorTests(unittest.TestCase):
    def test_empty_text(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_safe_budget(self):
        result = evaluate_budget("small task", 8192, 2048, 1024)
        self.assertEqual(result.status, "safe")
        self.assertEqual(result.recommended_chunks, 1)

    def test_split_budget(self):
        result = evaluate_budget("x" * 40000, 8192, 2048, 1024)
        self.assertEqual(result.status, "split")
        self.assertGreater(result.recommended_chunks, 1)


if __name__ == "__main__":
    unittest.main()
