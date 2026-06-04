import tempfile
import unittest
from pathlib import Path

import web_tool


class LocalMarkdownAssetsTest(unittest.TestCase):
    def test_relative_markdown_images_are_copied_to_job_input(self) -> None:
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as job_tmp:
            source_dir = Path(source_tmp)
            article = source_dir / "article.md"
            image = source_dir / "assets" / "flow.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            article.write_text("![flow](assets/flow.png)", encoding="utf-8")

            web_tool.combine_saved_files([article], Path(job_tmp))

            copied = Path(job_tmp) / "input" / "assets" / "flow.png"
            self.assertTrue(copied.exists())
            self.assertEqual(copied.read_bytes(), image.read_bytes())


if __name__ == "__main__":
    unittest.main()
