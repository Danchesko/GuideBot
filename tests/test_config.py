import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bishkek_food_finder.config import get_city_config


class ConfigTests(unittest.TestCase):
    def test_city_paths(self):
        for city in ("almaty", "bishkek"):
            with self.subTest(city=city):
                config = get_city_config(city)
                self.assertEqual(config["db_path"], f"data/{city}.db")
                self.assertEqual(config["chroma_path"], f"data/chroma_{city}")

    def test_unknown_city(self):
        with self.assertRaises(ValueError):
            get_city_config("unknown")

    def test_missing_review_key_fails_before_creating_data(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-m", "bishkek_food_finder.scraper.reviews", "--dry-run"],
                cwd=directory,
                env={**os.environ, "REVIEWS_API_KEY": ""},
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("Set REVIEWS_API_KEY", result.stderr)
            self.assertFalse((Path(directory) / "data").exists())
            self.assertFalse((Path(directory) / "logs").exists())


if __name__ == "__main__":
    unittest.main()
