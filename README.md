# Auto Image Redactor

本工具用于把飞书 SOP 文档、本地 Word/PDF/Markdown/HTML/图片里的截图批量脱敏，生成适合发布到 **CSDN** 的安全文章。

它的核心目标是：**尽量保持原文内容和排版不变，只替换需要打码的图片区域**。

## 能做什么

- 从飞书文档链接读取文章内容，底层调用本机 `lark-cli`。
- 支持上传 Markdown、HTML、Word、PDF、图片等本地文件。
- 自动提取文章图片，OCR 识别截图文字。
- 按脱敏规则识别公司、供应商、客户、账号、手机号、邮箱、税号、银行账号等敏感内容。
- 对 SAP 截图做保守判断，尽量避免误打码系统标准字段、菜单、按钮、页签和固定标签。
- 逐张图片流式显示处理结果，支持暂停/继续。
- 支持单张图片重新识别，可选择基于原图或当前打码图。
- 支持 CPA OpenAI-compatible API 做行业提示词增强判断。
- 生成安全文章、处理报告和可下载压缩包。
- 辅助复制富文本并打开 CSDN，最终发布仍由用户手动确认。

## 安装

```bash
python -m pip install -r requirements.txt
```

如需读取飞书链接，请先安装并登录 `lark-cli`，并确保当前账号有文档访问权限。

## 启动网页工具

Windows 可双击：

```text
start-web-tool.bat
```

或使用 CLI：

```bash
python auto_image_redactor_cli.py serve
```

打开：

```text
http://127.0.0.1:8866
```

## CLI 用法

本项目已提供适合 Agent 调用的命令行入口。

检查环境：

```bash
python auto_image_redactor_cli.py health
```

处理本地文件：

```bash
python auto_image_redactor_cli.py process article.docx --terms "某某公司,某某供应商" --jsonl
```

处理多个本地文件：

```bash
python auto_image_redactor_cli.py process article.md image1.png image2.png --terms "某某公司"
```

处理飞书链接：

```bash
python auto_image_redactor_cli.py lark "https://example.feishu.cn/docx/..." --terms "某某公司,某某客户" --jsonl
```

启用 CPA 大模型辅助判断：

```bash
python auto_image_redactor_cli.py process article.docx --use-cpa --api-key "sk-xxxx" --model "gpt-5.5"
```

使用行业提示词文件：

```bash
python auto_image_redactor_cli.py process article.docx --use-cpa --industry-prompt-file prompts/sap.txt
```

单张图片重新识别：

```bash
python auto_image_redactor_cli.py rerun-image <job_id> 2 --image-source original --instruction "只打码真实供应商名称，不要打码 SAP 标准字段"
```

`--image-source` 可选：

| 值 | 含义 |
|---|---|
| `original` | 基于文档原图重新识别 |
| `masked` | 基于当前打码图重新识别 |

CLI 默认输出 JSON；加 `--jsonl` 后会输出逐步进度，方便 Agent 实时监听。

## 脱敏词库

仓库只提供安全示例：

```text
sensitive_terms.example.txt
```

本地使用时可复制为：

```text
sensitive_terms.txt
```

`sensitive_terms.txt` 已被 `.gitignore` 忽略，避免把真实公司、供应商、客户等敏感词误传到公开仓库。

## CPA API 配置

工具使用 OpenAI-compatible 接口。默认 Base URL：

```text
https://cpa.fengsha.online/v1
```

网页端可在右上角“设置”中测试连接、获取模型、选择模型。

CLI 可传：

```bash
--use-cpa --api-key "sk-xxxx" --base-url "https://cpa.fengsha.online/v1" --model "gpt-5.5"
```

## CSDN 发布说明

工具会生成安全文章，并提供“复制富文本并打开 CSDN”。

为了避免误发布，**最终点击发布按钮必须由用户自己确认**。

## 给 Agent 的说明

Agent 自动化前请先阅读：

```text
LLM.TXT
```

里面包含 CLI 命令、输出字段、隐私边界和推荐操作流程。

## 不会上传到 GitHub 的内容

`.gitignore` 默认排除：

- 本地运行结果：`runs/`
- 临时输出：`output/`
- 测试结果：`test-results/`
- 日志：`*.log`
- 私有行业提示词：`industry_prompts/`
- 真实脱敏词库：`sensitive_terms.txt`
- 本地 Word/PDF/Excel/PPT 样例文件

这样可以降低公开仓库泄露敏感内容的风险。
