"""Create the deterministic public OCR acceptance corpus.

The corpus contains only simulated office prose.  It is intentionally generated
at build or validation time so no private documents or fonts need to be shipped
with the application.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

CANVAS_SIZE = (1654, 2339)
DEFAULT_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
)

_TOPICS = (
    "本周行政例会确认办公区设备巡检安排，责任人须在下班前更新台账并注明处理结果。",
    "项目组依据二〇二六年度服务计划复核采购申请，预算金额为{amount}元，审批编号为{code}。",
    "会议记录要求同时保留中文说明、English label 与版本号 V{version}，便于后续查阅和复核。",
    "资料归档按照部门、日期和事项分类，纸质文件扫描后应检查页码、印章位置及附件完整性。",
    "如发现数据缺失，请在 2 个工作日内填写更正单；紧急事项通过值班电话 010-8000-{phone} 联系。",
    "仓储盘点结果显示本批次入库 {count} 箱，抽检合格率为 {rate}%，异常项目应建立跟踪记录。",
    "安全培训结束后，员工需阅读现场指引并完成签到，未参加人员由部门主管安排补训。",
    "系统升级窗口安排在周三 22:00—23:30，服务恢复后由值班同事验证登录、查询和导出功能。",
    "客户反馈应保持原意，回复内容使用清晰短句，涉及金额、日期和数量时必须再次核对。",
    "本通知适用于研发、运营、财务和客服团队；各团队可以在不改变原始编号的前提下补充备注。",
    "月度报表提交前请检查小数位、千位分隔符以及负数符号，示例值包括 1,024.50、-8.00 和 0。",
    "纸张、墨盒和备用电池由综合事务组统一领用，领用人须在清单上签名并记录物料批号。",
    "审阅人应分别确认事实、计算和措辞，修订完成后在文档末尾填写姓名、日期及复核结论。",
    "如果附件超过 25 MB，应拆分为多个文件，并在索引页写明文件名、页数和对应的业务事项。",
    "流程变更自公告发布之日起生效，旧表单保留 30 天供查询，过期资料按内部归档规则处理。",
)


def choose_font(font: str | Path | None = None) -> Path:
    """Resolve a caller supplied font or a known local CJK font."""
    candidates = [Path(font)] if font else [Path(item) for item in DEFAULT_FONT_CANDIDATES]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("CJK font not found; pass --font with a local font file")


def _format_topic(template: str, number: int) -> str:
    return template.format(
        amount=f"{(number * 1379 + 8200):,}",
        code=f"OA-{2026 + number % 3:04d}-{number:03d}",
        version=f"{number % 4 + 1}.{number % 9}.{number % 7}",
        phone=f"{number * 19 + 100:03d}",
        count=number * 3 + 18,
        rate=f"{96.2 + (number % 8) / 10:.1f}",
    )


def make_text(number: int) -> str:
    """Return deterministic office prose with at least 650 non-whitespace chars."""
    title = f"办公资料验收记录（第 {number:02d} 页）"
    paragraphs: list[str] = []
    for offset in range(16):
        template = _TOPICS[(number * 3 + offset) % len(_TOPICS)]
        paragraphs.append(_format_topic(template, number + offset))
    # The repeated but varied paragraphs create a stable, realistic OCR page and
    # leave punctuation, digits and Latin case in every generated sample.
    return title + "\n" + "\n".join(paragraphs)


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Wrap by measured glyph widths, preserving explicit paragraph breaks."""
    lines: list[str] = []
    for paragraph in text.splitlines():
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current)
    return lines


def _draw_page(text: str, font_path: Path, number: int) -> Image.Image:
    image = Image.new("RGB", CANVAS_SIZE, "white")
    draw = ImageDraw.Draw(image)
    body = ImageFont.truetype(str(font_path), 30, index=0)
    title = ImageFont.truetype(str(font_path), 48, index=0)
    margin = 110
    # Keep the source text as a single reading-order stream.  Title emphasis is
    # visual only; the ground truth remains exact and whitespace-insensitive.
    paragraphs = text.splitlines()
    y = 95
    title_lines = wrap_text(draw, paragraphs[0], title, CANVAS_SIZE[0] - 2 * margin)
    for line in title_lines:
        draw.text((margin, y), line, fill=(25, 25, 25), font=title)
        y += 70
    y += 22
    for paragraph in paragraphs[1:]:
        for line in wrap_text(draw, paragraph, body, CANVAS_SIZE[0] - 2 * margin):
            draw.text((margin, y), line, fill=(28, 28, 28), font=body)
            y += 46
        y += 9
    return image


def _photo_variant(image: Image.Image, number: int) -> Image.Image:
    """Apply modest deterministic page-photo artifacts while retaining readability."""
    width, height = image.size
    # QUAD maps the output rectangle to a slightly skewed source quadrilateral.
    skew = 8 + number % 7
    # PIL's QUAD order is upper-left, lower-left, lower-right, upper-right.
    quad = (skew, 4, 4, height - skew, width - skew, height - 8, width - 5, skew)
    result = image.transform(image.size, Image.Transform.QUAD, quad, resample=Image.Resampling.BICUBIC)
    result = result.rotate((-1 if number % 2 else 1) * (0.45 + (number % 4) * 0.12), resample=Image.Resampling.BICUBIC, expand=False, fillcolor="white")

    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    strength = 18 + number % 10
    shadow_draw.rectangle((0, 0, width, 105), fill=(0, 0, 0, strength))
    shadow_draw.rectangle((0, height - 120, width, height), fill=(0, 0, 0, strength // 2))
    shadow = shadow.filter(ImageFilter.GaussianBlur(34))
    return Image.alpha_composite(result.convert("RGBA"), shadow).convert("RGB")


def generate_dataset(output: Path, font: str | Path | None = None) -> dict:
    """Generate 20 scan and 20 simulated photo pages and their exact truth."""
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("output directory must be empty or absent")
    output.mkdir(parents=True, exist_ok=True)
    font_path = choose_font(font)
    image_dir = output / "images"
    truth_dir = output / "ground-truth"
    image_dir.mkdir()
    truth_dir.mkdir()
    cases: list[dict[str, object]] = []
    for index in range(40):
        case_class = "scan" if index < 20 else "photo"
        case_id = f"{case_class}-{index % 20 + 1:02d}"
        text = make_text(index + 1)
        image = _draw_page(text, font_path, index + 1)
        suffix = ".png" if case_class == "scan" else ".jpg"
        if case_class == "photo":
            image = _photo_variant(image, index + 1)
        image_rel = Path("images") / f"{case_id}{suffix}"
        truth_rel = Path("ground-truth") / f"{case_id}.json"
        image_path = output / image_rel
        image.save(image_path, format="PNG" if suffix == ".png" else "JPEG", quality=92, optimize=True)
        characters = len("".join(text.split()))
        truth = {"schema": 1, "id": case_id, "class": case_class, "width": CANVAS_SIZE[0], "height": CANVAS_SIZE[1], "text": text, "characters": characters}
        (output / truth_rel).write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        cases.append({"id": case_id, "class": case_class, "image": image_rel.as_posix(), "ground_truth": truth_rel.as_posix(), "characters": characters})
    manifest = {"schema": 1, "canvas": {"width": CANVAS_SIZE[0], "height": CANVAS_SIZE[1]}, "cases": cases, "total_characters": sum(int(case["characters"]) for case in cases)}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if manifest["total_characters"] < 20_000:
        raise ValueError("generated corpus is below the 20,000 character acceptance threshold")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic public OCR acceptance samples")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--font", type=Path, help="local CJK font (never copied into the corpus)")
    args = parser.parse_args()
    manifest = generate_dataset(args.output, args.font)
    print(f"Generated {len(manifest['cases'])} pages and {manifest['total_characters']} non-whitespace characters.")


if __name__ == "__main__":
    main()
