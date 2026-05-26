# 开源方案参考记录

本工具优先站在现成方案上组合，不闭门造车。

## 已采用的方向

| 能力 | 当前选择 | 原因 |
|---|---|---|
| 飞书文档读取 | `lark-cli docs +fetch --api-version v2` | 官方 CLI，能直接读取飞书文档并返回 Markdown/XML |
| 本地 OCR | RapidOCR | 开源、本地运行、支持中文截图，不默认上传敏感图片 |
| 图片打码 | Pillow + OCR 坐标 | 实现简单、可控，不破坏文章正文 |
| Word 解析 | Mammoth | 开源，适合把 `.docx` 转成 HTML |
| PDF 处理 | PyMuPDF | 开源，先把 PDF 每页转成图片再统一走脱敏流程 |
| CSDN 发布助手 | 浏览器登录态 + 复制/打开编辑器 | CSDN 没有稳定公开写文章 API，开源方案多采用浏览器自动化思路 |

## 后续可继续参考

| 方向 | 候选 |
|---|---|
| 更强 PDF/版面解析 | MinerU、marker、docling |
| 更强 OCR | PaddleOCR、RapidOCR 新版能力 |
| 多平台发布 | 各类 browser automation / Selenium / Playwright 发布器 |
| 自动化确认前草稿填充 | Playwright 或 Chrome CDP，但最终发布按钮仍交给用户 |

## 当前取舍

- **不托管 CSDN 账号密码**：安全风险高，且容易触发平台风控。
- **不直接点击发布按钮**：最终发布需要用户确认。
- **默认本地脱敏**：敏感截图不上传；只有用户打开 CPA 模式时才用 API 辅助判断。
- **先保证 CSDN 场景闭环**：其它平台先不扩展，避免范围过大。
