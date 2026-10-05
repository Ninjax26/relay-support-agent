import unittest
from scripts.check_publish import inspect


class PublicationTests(unittest.TestCase):
    def test_runtime_and_private_artifacts_are_blocked(self):
        for name in [".env", "data/supportops.sqlite3", "docs/relay-preview.png", ".impeccable/hook.cache.json", ".aws/credentials"]:
            self.assertTrue(inspect(name, b"local artifact"))

    def test_credential_is_detected_without_printing_it(self):
        key = "AIza" + "a" * 35
        result = inspect("server.py", ("TOKEN = '" + key + "'").encode())
        self.assertIn("Google API key", result)
        self.assertNotIn(key, repr(result))

    def test_empty_environment_template_is_allowed(self):
        self.assertEqual(inspect(".env.example", b"GEMINI_API_KEY=\nOPENAI_API_KEY=\nPORT=8000\n"), [])


if __name__ == "__main__":
    unittest.main()
