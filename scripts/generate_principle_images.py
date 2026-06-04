from __future__ import annotations

import math
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "static" / "assets"
OUT.mkdir(parents=True, exist_ok=True)

W, H = 1920, 1080
FONT_REG = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)


F = {
    "hero": font(56, True),
    "title": font(38, True),
    "subtitle": font(25),
    "h2": font(30, True),
    "h3": font(24, True),
    "body": font(21),
    "small": font(18),
    "tiny": font(15),
    "badge": font(17, True),
    "code": font(18),
}

P = {
    "ink": (15, 31, 58),
    "muted": (94, 112, 139),
    "line": (213, 225, 243),
    "panel": (255, 255, 255),
    "soft": (246, 250, 255),
    "soft2": (239, 247, 255),
    "blue": (39, 105, 255),
    "blue2": (83, 144, 255),
    "cyan": (25, 198, 214),
    "teal": (24, 183, 160),
    "green": (21, 190, 122),
    "orange": (255, 138, 52),
    "red": (255, 92, 108),
    "purple": (112, 95, 244),
    "slate": (47, 65, 96),
}


def text_size(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0], box[3] - box[1]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.ImageFont, max_width: int) -> list[str]:
    token_pattern = re.compile(r"[A-Za-z0-9_./:+#$-]+|\s+|.", re.S)
    lines: list[str] = []
    for para in text.split("\n"):
        current = ""
        for token in token_pattern.findall(para):
            if token.isspace():
                token = " "
            test = current + token
            if text_size(draw, test, fnt)[0] <= max_width or not current:
                current = test
                continue
            lines.append(current.rstrip())
            current = token.lstrip()
            if text_size(draw, current, fnt)[0] > max_width:
                forced = ""
                for ch in current:
                    test_forced = forced + ch
                    if text_size(draw, test_forced, fnt)[0] <= max_width or not forced:
                        forced = test_forced
                    else:
                        lines.append(forced)
                        forced = ch
                current = forced
        if current:
            lines.append(current.rstrip())
    return lines


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    fnt: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    max_width: int,
    line_gap: int = 7,
    max_lines: int | None = None,
) -> int:
    x, y = xy
    lines = wrap_text(draw, text, fnt, max_width)
    if max_lines is not None:
        lines = lines[:max_lines]
    yy = y
    line_height = text_size(draw, "国", fnt)[1] + line_gap
    for line in lines:
        draw.text((x, yy), line, font=fnt, fill=fill)
        yy += line_height
    return yy


def bg() -> Image.Image:
    image = Image.new("RGB", (W, H), (247, 251, 255))
    pixels = image.load()
    for y in range(H):
        for x in range(W):
            cx = x / W
            cy = y / H
            r = int(249 - 8 * cy - 3 * cx)
            g = int(252 - 6 * cy)
            b = int(255 - 1 * cy)
            pixels[x, y] = (r, g, b)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for x in range(0, W, 72):
        draw.line((x, 0, x, H), fill=(87, 132, 189, 16), width=1)
    for y in range(0, H, 72):
        draw.line((0, y, W, y), fill=(87, 132, 189, 13), width=1)
    draw.rounded_rectangle((54, 54, W - 54, H - 54), radius=36, fill=(255, 255, 255, 128), outline=(225, 235, 248, 190), width=2)
    return Image.alpha_composite(image.convert("RGBA"), overlay)


def shadowed(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], radius: int = 22, shadow: int = 28) -> None:
    x1, y1, x2, y2 = xy
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rounded_rectangle((x1 + 8, y1 + 12, x2 + 8, y2 + 12), radius=radius, fill=(43, 83, 132, shadow))
    layer = layer.filter(ImageFilter.GaussianBlur(12))
    draw.bitmap((0, 0), layer)


def panel(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], title: str, color: tuple[int, int, int], fill=(255, 255, 255)) -> None:
    x1, y1, x2, y2 = xy
    shadowed(draw, xy)
    draw.rounded_rectangle(xy, radius=22, fill=fill, outline=P["line"], width=2)
    draw.rounded_rectangle((x1, y1, x2, y1 + 56), radius=22, fill=color)
    draw.rectangle((x1, y1 + 34, x2, y1 + 56), fill=color)
    draw.text((x1 + 24, y1 + 15), title, font=F["h3"], fill=(255, 255, 255))


def plain_panel(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], color=P["line"], fill=(255, 255, 255), radius=22) -> None:
    shadowed(draw, xy, radius=radius, shadow=18)
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=color, width=2)


def chip(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], text: str, color: tuple[int, int, int], fill=(255, 255, 255)) -> None:
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=(y2 - y1) // 2, fill=fill, outline=color, width=2)
    tw, th = text_size(draw, text, F["badge"])
    draw.text((x1 + (x2 - x1 - tw) / 2, y1 + (y2 - y1 - th) / 2 - 2), text, font=F["badge"], fill=color)


def header(draw: ImageDraw.ImageDraw, title: str, subtitle: str, chips: list[tuple[str, tuple[int, int, int]]]) -> None:
    draw.text((94, 76), title, font=F["hero"], fill=P["ink"])
    draw.text((98, 150), subtitle, font=F["subtitle"], fill=P["muted"])
    draw.rounded_rectangle((98, 190, 395, 198), radius=4, fill=P["blue"])
    draw.rounded_rectangle((410, 190, 575, 198), radius=4, fill=P["teal"])
    x = W - 100
    for text, color in reversed(chips):
        tw, _ = text_size(draw, text, F["badge"])
        width = tw + 42
        chip(draw, (x - width, 88, x, 128), text, color)
        x -= width + 16


def arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int], color: tuple[int, int, int], width: int = 8) -> None:
    x1, y1 = start
    x2, y2 = end
    draw.line((x1, y1, x2, y2), fill=color, width=width)
    angle = math.atan2(y2 - y1, x2 - x1)
    size = 28
    p1 = (x2 - size * math.cos(angle) + size * 0.58 * math.sin(angle), y2 - size * math.sin(angle) - size * 0.58 * math.cos(angle))
    p2 = (x2 - size * math.cos(angle) - size * 0.58 * math.sin(angle), y2 - size * math.sin(angle) + size * 0.58 * math.cos(angle))
    draw.polygon([end, p1, p2], fill=color)


def icon_doc(draw: ImageDraw.ImageDraw, x: int, y: int, color: tuple[int, int, int]) -> None:
    draw.rounded_rectangle((x, y, x + 115, y + 132), radius=14, fill=(248, 251, 255), outline=(214, 226, 244), width=2)
    draw.polygon([(x + 82, y), (x + 115, y + 34), (x + 82, y + 34)], fill=(226, 236, 250))
    for i, w in enumerate([72, 88, 78, 56]):
        draw.rounded_rectangle((x + 20, y + 50 + i * 19, x + 20 + w, y + 57 + i * 19), radius=3, fill=(182, 197, 219))
    draw.rounded_rectangle((x + 24, y + 96, x + 80, y + 119), radius=5, fill=(*color, 48))


def icon_file(draw: ImageDraw.ImageDraw, x: int, y: int, label: str, color: tuple[int, int, int]) -> None:
    draw.rounded_rectangle((x, y, x + 68, y + 82), radius=10, fill=(255, 255, 255), outline=(*color, 180), width=2)
    draw.polygon([(x + 46, y), (x + 68, y + 22), (x + 46, y + 22)], fill=(*color, 55))
    draw.text((x + 14, y + 32), label, font=F["badge"], fill=color)


def icon_ocr(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    c = P["teal"]
    for dx, dy, sx, sy in [(0, 0, 1, 1), (80, 0, -1, 1), (0, 80, 1, -1), (80, 80, -1, -1)]:
        draw.line((x + dx, y + dy, x + dx + sx * 24, y + dy), fill=c, width=5)
        draw.line((x + dx, y + dy, x + dx, y + dy + sy * 24), fill=c, width=5)
    draw.text((x + 21, y + 29), "OCR", font=F["badge"], fill=c)


def icon_shield(draw: ImageDraw.ImageDraw, x: int, y: int, color=P["green"]) -> None:
    draw.polygon([(x + 48, y), (x + 88, y + 18), (x + 80, y + 82), (x + 48, y + 104), (x + 16, y + 82), (x + 8, y + 18)], fill=(*color, 44), outline=color)
    draw.line((x + 30, y + 53, x + 43, y + 67, x + 68, y + 35), fill=color, width=6)


def icon_magnifier(draw: ImageDraw.ImageDraw, x: int, y: int, color=P["orange"]) -> None:
    draw.ellipse((x, y, x + 70, y + 70), outline=color, width=6)
    draw.line((x + 55, y + 55, x + 96, y + 96), fill=color, width=7)
    draw.line((x + 35, y + 18, x + 35, y + 45), fill=color, width=5)


def icon_editor(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=16, fill=(255, 255, 255), outline=(214, 226, 244), width=2)
    draw.rectangle((x1, y1, x2, y1 + 52), fill=(245, 249, 255))
    for i, t in enumerate(["H", "B", "I", "</>", "Link"]):
        draw.text((x1 + 24 + i * 45, y1 + 16), t, font=F["small"], fill=(94, 112, 139))
    draw.text((x1 + 28, y1 + 82), "技术分享：系统接入与配置实践", font=F["h3"], fill=P["ink"])
    for i, w in enumerate([310, 365, 260, 340]):
        draw.rounded_rectangle((x1 + 28, y1 + 128 + i * 24, x1 + 28 + w, y1 + 137 + i * 24), radius=4, fill=(193, 205, 224))
    draw.rounded_rectangle((x2 - 190, y1 + 134, x2 - 42, y1 + 232), radius=10, fill=(220, 237, 255))
    draw.polygon([(x2 - 170, y1 + 210), (x2 - 125, y1 + 166), (x2 - 82, y1 + 210)], fill=(118, 174, 255))
    draw.rounded_rectangle((x1 + 30, y1 + 252, x2 - 28, y1 + 320), radius=10, fill=(247, 249, 252), outline=(229, 236, 247))


def feature_strip(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], items: list[tuple[str, str, tuple[int, int, int]]]) -> None:
    x1, y1, x2, y2 = xy
    plain_panel(draw, xy, fill=(250, 253, 255), radius=18)
    width = (x2 - x1) / len(items)
    for i, (title, desc, color) in enumerate(items):
        cx = int(x1 + width * i + 34)
        cy = y1 + 30
        draw.ellipse((cx, cy, cx + 50, cy + 50), fill=(*color, 30), outline=color, width=2)
        draw.text((cx + 72, y1 + 30), title, font=F["h3"], fill=P["ink"])
        draw.text((cx + 72, y1 + 62), desc, font=F["tiny"], fill=P["muted"])
        if i:
            draw.line((int(x1 + width * i), y1 + 28, int(x1 + width * i), y2 - 28), fill=P["line"], width=1)


def step_tabs(draw: ImageDraw.ImageDraw, y: int, steps: list[str]) -> None:
    x = 96
    gap = 16
    tab_w = int((W - 192 - gap * (len(steps) - 1)) / len(steps))
    for i, step in enumerate(steps):
        draw.rounded_rectangle((x, y, x + tab_w, y + 50), radius=12, fill=(239, 247, 255), outline=(207, 222, 246), width=2)
        text = f"{i + 1} {step}"
        tw, th = text_size(draw, text, F["badge"])
        draw.text((x + (tab_w - tw) / 2, y + 15), text, font=F["badge"], fill=P["ink"])
        x += tab_w + gap


def save(image: Image.Image, name: str) -> None:
    path = OUT / name
    image.convert("RGB").save(path, quality=94, optimize=True)
    print(f"{path} {path.stat().st_size}")


def overall() -> None:
    image = bg()
    draw = ImageDraw.Draw(image)
    header(
        draw,
        "飞书/Word/PDF 一键转 CSDN 安全草稿",
        "自动识别截图敏感文字，脱敏处理后生成可发布的 CSDN 草稿",
        [("安全脱敏", P["teal"]), ("隐私保护", P["blue"]), ("一键转换", P["purple"]), ("可直接发布", P["green"])],
    )
    panel(draw, (96, 242, 718, 720), "1. 来源文档", P["blue2"])
    draw.text((150, 322), "飞书文档", font=F["h2"], fill=P["ink"])
    draw.text((150, 360), "在线文档链接 / 知识库 / 云空间文件", font=F["small"], fill=P["muted"])
    icon_doc(draw, 150, 405, P["blue"])
    for i, w in enumerate([350, 390, 310, 365]):
        draw.rounded_rectangle((300, 418 + i * 34, 300 + w, 430 + i * 34), radius=5, fill=(194, 207, 226))
    draw.text((352, 572), "或上传本地文件", font=F["small"], fill=P["muted"])
    icon_file(draw, 215, 610, "W", P["blue"])
    icon_file(draw, 335, 610, "PDF", P["red"])
    icon_file(draw, 480, 610, "MD", P["purple"])
    label_y = 676
    draw.rounded_rectangle((140, label_y, 674, label_y + 40), radius=16, fill=(231, 250, 246), outline=(176, 231, 222))
    draw.text((168, label_y + 10), "只处理指定内容，保护数据安全", font=F["badge"], fill=P["teal"])

    panel(draw, (792, 242, 1410, 720), "2. OCR 识别与隐私保护", P["teal"])
    draw.text((845, 310), "① 识别文字", font=F["small"], fill=P["slate"])
    arrow(draw, (988, 324), (1058, 324), P["teal"], 5)
    draw.text((1080, 310), "② 检测敏感信息", font=F["small"], fill=P["slate"])
    arrow(draw, (1238, 324), (1308, 324), P["teal"], 5)
    draw.text((1266, 310), "③ 自动脱敏处理", font=F["small"], fill=P["slate"])
    plain_panel(draw, (840, 372, 1036, 628), fill=(255, 255, 255), radius=18)
    icon_ocr(draw, 898, 430)
    draw.text((890, 540), "提取截图文字", font=F["small"], fill=P["muted"])
    plain_panel(draw, (1084, 372, 1280, 628), fill=(255, 255, 255), radius=18)
    icon_magnifier(draw, 1132, 420)
    for i, item in enumerate(["手机号", "姓名", "证件号", "邮箱", "密钥/Token"]):
        draw.rounded_rectangle((1116, 522 + i * 26, 1250, 542 + i * 26), radius=6, fill=(255, 244, 240), outline=(255, 210, 198))
        draw.text((1128, 523 + i * 26), item, font=F["tiny"], fill=P["slate"])
        draw.text((1212, 523 + i * 26), "••••••", font=F["tiny"], fill=P["red"])
    plain_panel(draw, (1250, 372, 1390, 628), fill=(255, 255, 255), radius=18)
    icon_shield(draw, 1268, 410)
    for i, w in enumerate([80, 120, 100, 130, 95]):
        draw.rounded_rectangle((1268, 534 + i * 22, 1268 + min(w, 86), 544 + i * 22), radius=4, fill=(191, 203, 222))
        draw.rounded_rectangle((1362, 534 + i * 22, 1378, 544 + i * 22), radius=4, fill=(180, 180, 180))
    draw.rounded_rectangle((838, 654, 1366, 704), radius=18, fill=(239, 247, 255), outline=(205, 225, 255))
    draw.text((864, 670), "多种脱敏策略：马赛克、隐藏、替换等，防止隐私泄露", font=F["small"], fill=P["slate"])

    panel(draw, (1450, 242, 1824, 720), "3. 安全的 CSDN 草稿", P["blue"])
    icon_editor(draw, (1492, 314, 1784, 640))
    draw.rounded_rectangle((1490, 662, 1786, 710), radius=16, fill=(235, 244, 255), outline=(199, 221, 255))
    draw.text((1518, 676), "脱敏完成，可直接检查草稿", font=F["badge"], fill=P["blue"])

    arrow(draw, (718, 482), (792, 482), P["teal"], 12)
    arrow(draw, (1410, 482), (1450, 482), P["blue"], 12)
    feature_strip(
        draw,
        (96, 750, 1824, 840),
        [
            ("精准识别", "OCR 高精度提取文字", P["blue"]),
            ("智能脱敏", "自动识别并处理敏感信息", P["purple"]),
            ("安全可靠", "本地处理，不留存原始数据", P["teal"]),
            ("高效便捷", "一键生成 CSDN 草稿", P["blue"]),
            ("合规放心", "保护隐私，安心分享", P["green"]),
        ],
    )
    step_tabs(draw, 888, ["导入文章", "填写敏感词", "开始脱敏", "检查图片", "发布前确认"])
    save(image, "principle-overall.png")


def lark_cli() -> None:
    image = bg()
    draw = ImageDraw.Draw(image)
    header(
        draw,
        "飞书 CLI 识别和读取链接原理",
        "工具真正调用的是本机 lark-cli 当前授权账号，而不是浏览器里正在打开的飞书网页",
        [("docx", P["blue"]), ("wiki", P["purple"]), ("file", P["orange"]), ("权限校验", P["green"])],
    )
    panel(draw, (92, 245, 495, 735), "1. 粘贴链接后先分类", P["blue"])
    draw.text((132, 330), "工具只看 URL 路径", font=F["h2"], fill=P["ink"])
    for i, (kind, desc, color) in enumerate([
        ("/docx/", "新版在线文档", P["blue"]),
        ("/wiki/", "知识库节点", P["purple"]),
        ("/file/", "云空间文件", P["orange"]),
    ]):
        y = 402 + i * 88
        draw.rounded_rectangle((135, y, 450, y + 58), radius=14, fill=(246, 250, 255), outline=(217, 228, 244))
        draw.text((156, y + 15), kind, font=F["badge"], fill=color)
        draw.text((250, y + 15), desc, font=F["small"], fill=P["slate"])
    draw_wrapped(draw, (135, 674), "这一步只负责判断路线，不代表已经有读取权限。", F["small"], P["muted"], 320, 5)

    panel(draw, (600, 245, 1190, 735), "2. 三种链接走三条读取路线", P["teal"])
    routes = [
        ("A", "docx 在线文档", "docs +fetch 直接读取正文和图片引用", P["blue"]),
        ("B", "wiki 知识库", "wiki +node-get 找真实文档，再 docs +fetch", P["purple"]),
        ("C", "file 云空间", "drive +inspect 读信息，再 drive +download 下载文件", P["orange"]),
    ]
    for i, (idx, title_text, desc, color) in enumerate(routes):
        y = 330 + i * 115
        draw.rounded_rectangle((640, y, 1150, y + 82), radius=16, fill=(255, 255, 255), outline=color, width=2)
        draw.ellipse((666, y + 18, 712, y + 64), fill=(*color, 40), outline=color, width=2)
        draw.text((682, y + 29), idx, font=F["badge"], fill=color)
        draw.text((735, y + 15), title_text, font=F["h3"], fill=P["ink"])
        draw.text((735, y + 48), desc, font=F["small"], fill=P["muted"])
    draw.rounded_rectangle((640, 690, 1150, 720), radius=12, fill=(236, 249, 246), outline=(190, 233, 225))
    draw.text((660, 696), "所有路线最终都会进入同一套图片脱敏流水线", font=F["badge"], fill=P["teal"])

    panel(draw, (1290, 245, 1828, 735), "3. lark-cli 用当前 profile 授权访问", P["green"])
    draw.rounded_rectangle((1338, 330, 1780, 520), radius=16, fill=(15, 31, 58))
    for i, line in enumerate([
        "docs +fetch --as user",
        "wiki +node-get --as user",
        "drive +inspect --as user",
        "drive +download --as user",
    ]):
        draw.text((1368, 365 + i * 34), line, font=F["code"], fill=(206, 230, 255))
    draw_wrapped(draw, (1340, 560), "飞书网页能打开，只说明浏览器账号有权限；工具是否能读，取决于 lark-cli 当前授权账号和 scope。", F["body"], P["slate"], 440, 7)

    arrow(draw, (495, 490), (600, 490), P["teal"], 11)
    arrow(draw, (1190, 490), (1290, 490), P["green"], 11)

    feature_strip(
        draw,
        (92, 790, 1828, 900),
        [
            ("网页能看但工具读不到", "通常是 CLI 授权账号和网页账号不同", P["blue"]),
            ("file 链接缺权限", "补 drive:drive.metadata:readonly drive:file:download", P["orange"]),
            ("读取成功后", "docx/wiki 生成 Markdown；file 下载后按本地文件处理", P["green"]),
        ],
    )
    step_tabs(draw, 942, ["检查 CLI 账号", "生成授权链接", "选择企业账号", "补高级权限", "重新读取链接"])
    save(image, "principle-lark-cli.png")


def redaction() -> None:
    image = bg()
    draw = ImageDraw.Draw(image)
    header(
        draw,
        "图片识别、判断与打码原理",
        "先把截图里的文字和坐标找出来，再只遮住真正敏感的业务值，正文尽量不动",
        [("OCR 定位", P["teal"]), ("敏感检测", P["orange"]), ("SAP 过滤", P["purple"]), ("报告可查", P["green"])],
    )
    panel(draw, (92, 250, 580, 760), "1. 原始截图", P["blue2"])
    draw.text((140, 336), "SAP 截图示意", font=F["h2"], fill=P["ink"])
    rows = [("公司代码", "1000", False), ("供应商", "某某供应商", True), ("物料编码", "MAT-001", False), ("邮箱", "demo@corp.com", True), ("银行账号", "6222 8888 9999", True)]
    y = 410
    for key, value, sensitive in rows:
        draw.text((145, y), key, font=F["h3"], fill=P["slate"])
        draw.text((285, y), value, font=F["body"], fill=P["slate"])
        if sensitive:
            draw.rounded_rectangle((278, y - 8, 528, y + 32), radius=8, fill=(255, 82, 145, 62), outline=P["red"], width=2)
        y += 70

    panel(draw, (680, 250, 1210, 760), "2. 识别与判断引擎", P["teal"])
    steps = [
        ("① OCR 识字定位", "输出 text / box / confidence，box 就是后续打码坐标", P["teal"]),
        ("② 候选敏感值", "敏感词、手机号、邮箱、税号、银行卡等规则先命中", P["orange"]),
        ("③ SAP 误伤过滤", "菜单、按钮、页签、字段名、物料编码尽量保留", P["purple"]),
    ]
    for i, (title_text, desc, color) in enumerate(steps):
        yy = 330 + i * 115
        draw.rounded_rectangle((720, yy, 1170, yy + 82), radius=16, fill=(255, 255, 255), outline=color, width=2)
        draw.text((744, yy + 15), title_text, font=F["h3"], fill=P["ink"])
        draw.text((744, yy + 50), desc, font=F["small"], fill=P["muted"])
    draw.rounded_rectangle((720, 690, 1170, 730), radius=14, fill=(239, 247, 255), outline=(205, 223, 248))
    draw.text((744, 701), "可选 CPA：只发送 OCR 文字行和上下文，不上传整张截图", font=F["small"], fill=P["slate"])

    panel(draw, (1310, 250, 1828, 760), "3. 马赛克替换与结果报告", P["blue"])
    draw.text((1360, 330), "命中坐标 → 外扩 padding → 只处理命中区域", font=F["h3"], fill=P["ink"])
    draw.rounded_rectangle((1360, 392, 1778, 560), radius=16, fill=(246, 250, 255), outline=(213, 225, 243))
    for i, w in enumerate([320, 370, 280, 340]):
        yy = 422 + i * 28
        draw.rounded_rectangle((1392, yy, 1392 + w, yy + 10), radius=4, fill=(190, 204, 224))
    draw.rounded_rectangle((1440, 470, 1630, 512), radius=8, fill=(158, 158, 158))
    draw.rounded_rectangle((1655, 520, 1750, 548), radius=8, fill=(158, 158, 158))
    for i, (name, desc, color) in enumerate([("report.md", "用户看的图片检查报告", P["blue"]), ("report.json", "Agent 读的结构化命中结果", P["purple"])]):
        yy = 610 + i * 58
        draw.rounded_rectangle((1360, yy, 1778, yy + 42), radius=12, fill=(255, 255, 255), outline=(214, 226, 244))
        draw.text((1382, yy + 10), name, font=F["badge"], fill=color)
        draw.text((1510, yy + 10), desc, font=F["small"], fill=P["muted"])

    arrow(draw, (580, 505), (680, 505), P["teal"], 12)
    arrow(draw, (1210, 505), (1310, 505), P["blue"], 12)
    feature_strip(
        draw,
        (92, 800, 1828, 910),
        [
            ("漏打码", "补充敏感词，或对单张图重新识别", P["red"]),
            ("误打码", "改用精确匹配，或写“不要打 SAP 标准字段”", P["orange"]),
            ("只改图片", "正文内容尽量保持不变", P["teal"]),
            ("可复查", "每张图都能放大查看命中区域", P["green"]),
        ],
    )
    step_tabs(draw, 950, ["OCR 找字", "命中候选", "SAP 过滤", "生成遮罩", "替换图片"])
    save(image, "principle-image-redaction.png")


def csdn_cookie() -> None:
    image = bg()
    draw = ImageDraw.Draw(image)
    header(
        draw,
        "CSDN Cookie 获取与草稿写入原理",
        "不硬读浏览器数据库，而是连接你主动打开的 CSDN 自动浏览器，读取当前登录态",
        [("自动浏览器", P["blue"]), ("CDP 读取", P["purple"]), ("上传组件", P["teal"]), ("只写草稿", P["green"])],
    )
    panel(draw, (90, 248, 528, 740), "1. 打开 CSDN 自动浏览器", P["blue"])
    draw.rounded_rectangle((138, 330, 480, 556), radius=16, fill=(255, 255, 255), outline=(214, 226, 244))
    draw.rectangle((138, 330, 480, 374), fill=(242, 247, 255))
    for i, c in enumerate([(255, 100, 110), (255, 185, 60), (40, 205, 140)]):
        draw.ellipse((160 + i * 28, 346, 174 + i * 28, 360), fill=c)
    draw.text((166, 410), "https://mp.csdn.net/...", font=F["small"], fill=P["slate"])
    draw.text((166, 470), "用户在这里正常登录 CSDN", font=F["h3"], fill=P["ink"])
    draw_wrapped(draw, (140, 610), "浏览器带独立用户目录和远程调试端口 9222，不污染日常浏览器。", F["body"], P["slate"], 340)

    panel(draw, (628, 248, 1190, 740), "2. CDP 读取登录态", P["purple"])
    draw.rounded_rectangle((675, 330, 1140, 530), radius=16, fill=(15, 31, 58))
    for i, line in enumerate(["--remote-debugging-port=9222", "尝试 127.0.0.1 / localhost / [::1]", "Network.getCookies(CSDN 相关网址)", "校验 UserToken 等关键凭证"]):
        draw.text((710, 372 + i * 36), line, font=F["code"], fill=(216, 234, 255))
    draw_wrapped(draw, (680, 585), "如果没有完整登录态，工具会提示刷新创作页或重新登录，不会继续上传。", F["body"], P["slate"], 460)

    panel(draw, (1290, 248, 1828, 740), "3. 上传图片并写入草稿", P["teal"])
    icon_editor(draw, (1338, 330, 1780, 610))
    draw.rounded_rectangle((1350, 635, 1770, 702), radius=16, fill=(236, 250, 247), outline=(187, 231, 224))
    draw.text((1376, 652), "window.csdn.upload.uploadImg(...)", font=F["code"], fill=P["teal"])
    draw.text((1376, 680), "自动适配 CKEditor / Markdown，只写草稿", font=F["small"], fill=P["slate"])

    arrow(draw, (528, 494), (628, 494), P["purple"], 12)
    arrow(draw, (1190, 494), (1290, 494), P["teal"], 12)

    feature_strip(
        draw,
        (90, 790, 1828, 900),
        [
            ("旧方式为何失败", "浏览器数据库加密，手动 document.cookie 可能不完整", P["orange"]),
            ("新方式优势", "连接专用自动浏览器，拿到完整页面登录态", P["blue"]),
            ("安全边界", "不保存 Cookie、不绕过登录、不自动点击发布", P["green"]),
        ],
    )
    step_tabs(draw, 942, ["打开自动浏览器", "用户登录 CSDN", "检查自动登录", "检查上传组件", "写入草稿"])
    save(image, "principle-csdn-cookie.png")


def main() -> None:
    overall()
    lark_cli()
    redaction()
    csdn_cookie()


if __name__ == "__main__":
    main()
