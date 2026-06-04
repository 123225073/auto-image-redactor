import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_tool


class LarkFileAndCsdnEditorTest(unittest.TestCase):
    def test_lark_file_url_is_detected(self) -> None:
        self.assertTrue(web_tool.is_lark_file_url("https://tenant.feishu.cn/file/PLvFbNWKnoqWqAxdeMlcK0f6n4f"))
        self.assertEqual(
            web_tool.extract_lark_file_token("https://tenant.feishu.cn/file/PLvFbNWKnoqWqAxdeMlcK0f6n4f?from=copy"),
            "PLvFbNWKnoqWqAxdeMlcK0f6n4f",
        )
        self.assertFalse(web_tool.is_lark_file_url("https://tenant.feishu.cn/docx/ABCDEF"))

    def test_lark_file_missing_scope_gets_actionable_message(self) -> None:
        raw = {
            "ok": False,
            "error": {
                "type": "authorization",
                "subtype": "missing_scope",
                "message": "missing required scope(s): drive:file:download",
                "missing_scopes": ["drive:file:download"],
            },
        }
        completed = subprocess.CompletedProcess(["lark-cli"], 1, json.dumps(raw), "")
        with patch.object(web_tool, "run_lark_cli", return_value=completed):
            with self.assertRaises(web_tool.UserVisibleError) as ctx:
                web_tool.download_lark_file("https://tenant.feishu.cn/file/FILETOKEN", Path(tempfile.mkdtemp()))
        message = str(ctx.exception)
        self.assertIn("drive:file:download", message)
        self.assertIn("云空间文件", message)

    def test_csdn_copy_payload_writes_editor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "abcdef123456"
            output_dir = job_dir / "output"
            output_dir.mkdir(parents=True)
            (output_dir / "article.html").write_text("<title>Hello</title>\n# Hello\n正文", encoding="utf-8")
            (output_dir / "report.md").write_text("", encoding="utf-8")
            (output_dir / "report.json").write_text('{"images":[]}', encoding="utf-8")
            (job_dir / "job.json").write_text(
                json.dumps({"outputFile": "article.html", "source": "test", "notices": []}),
                encoding="utf-8",
            )
            with patch.object(web_tool, "write_system_clipboard") as clipboard:
                with patch.object(
                    web_tool,
                    "write_csdn_editor_via_browser",
                    return_value={"ok": True, "method": "textarea", "pageUrl": "https://mp.csdn.net/mp_blog/creation/editor"},
                ) as writer:
                    result = web_tool.prepare_csdn_editor_payload(job_dir, 9222)
        clipboard.assert_called_once()
        writer.assert_called_once()
        self.assertEqual(result["csdnEditorWrite"]["method"], "textarea")
        self.assertEqual(result["title"], "Hello")


if __name__ == "__main__":
    unittest.main()
