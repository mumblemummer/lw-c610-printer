"""
360 DPI Label Bitmap & Preview Renderer for Epson LW-C610.

Supports:
  - Korean & English TrueType fonts on Windows (맑은 고딕, 굴림, 바탕, 돋움, 궁서, Arial, Consolas, Segoe UI)
  - Auto-fit font size to tape height or manual point/dot size
  - Exact ink-bounding-box text measurement (prevents any left/right glyph overhang clipping)
  - Bold font variant selection
  - Multi-line text with left / center / right horizontal alignment
  - Frame styles: None, Rectangle, Rounded Rectangle, Thick Box, Pill/Capsule
  - QR Code generation (left or right of text, or standalone QR)
  - Custom image loading (monochrome threshold / Floyd-Steinberg dither)
  - Visual WYSIWYG preview with physical tape background, printable zone guides, and cut/margin markers
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

from lwc610_protocol import DPI, TAPE_PRINTABLE_DOTS, TAPE_WIDTH_MM, dots_to_mm, mm_to_dots

try:
    import qrcode
except ImportError:
    qrcode = None


# Windows Korean & Latin Font Candidates
WINDOWS_FONT_MAP: Dict[str, Tuple[str, str]] = {
    "맑은 고딕 (Malgun Gothic)": ("malgun.ttf", "malgunbd.ttf"),
    "굴림 (Gulim)": ("gulim.ttc", "gulim.ttc"),
    "바탕 (Batang)": ("batang.ttc", "batang.ttc"),
    "궁서 (Gungsuh)": ("gungsuh.ttc", "gungsuh.ttc"),
    "Arial": ("arial.ttf", "arialbd.ttf"),
    "Segoe UI": ("segoeui.ttf", "segoeuib.ttf"),
    "Consolas (고정폭)": ("consola.ttf", "consolab.ttf"),
    "Impact": ("impact.ttf", "impact.ttf"),
}

FRAME_STYLES = [
    "없음 (None)",
    "직사각형 (Rectangle)",
    "둥근 사각형 (Rounded)",
    "굵은 테두리 (Thick Box)",
    "캡슐형 (Capsule)",
]

LINE_RATIO_MODES = [
    "동일 크기 (1:1)",
    "1줄 크게 · 제목+부제 (6:4)",
    "2줄 크게 · 부제+본문 (4:6)",
]


@dataclass
class LabelDesign:
    """Parameters defining a label to be rendered at 360 DPI."""
    text: str = "안녕하세요 LW-C610"
    font_name: str = "맑은 고딕 (Malgun Gothic)"
    auto_font_size: bool = True
    font_size_px: int = 84
    bold: bool = True
    invert_colors: bool = False
    h_align: str = "center"  # "left", "center", "right"
    frame_style: str = "없음 (None)"
    line_ratio_mode: str = "동일 크기 (1:1)"
    line_gap_dots: int = 8
    line_divider: bool = False
    qr_text: str = ""
    qr_position: str = "left"  # "left", "right", "only"
    image_path: str = ""
    image_dither: bool = False
    tape_code: int = 4  # Default 4 = 12mm (144 printable dots)
    fixed_length_mm: float = 0.0  # 0.0 = auto length from content
    margin_mm: float = 0.0
    lead_offset_mm: float = 0.0
    inner_padding_mm: float = 2.5


def load_font(font_name: str, size_px: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a TrueType font from C:\\Windows\\Fonts with fallback."""
    fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    regular_file, bold_file = WINDOWS_FONT_MAP.get(
        font_name, ("malgun.ttf", "malgunbd.ttf")
    )
    chosen = bold_file if bold else regular_file
    candidates = [
        os.path.join(fonts_dir, chosen),
        os.path.join(fonts_dir, regular_file),
        os.path.join(fonts_dir, "malgunbd.ttf" if bold else "malgun.ttf"),
        os.path.join(fonts_dir, "arial.ttf"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, max(8, int(size_px)))
            except Exception:
                continue
    return ImageFont.load_default()


def _render_single_line_tight(
    line_text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> Optional[Image.Image]:
    """
    Render a single line of text onto a generous scratch canvas and crop to the
    exact ink bounding box (eliminating TrueType top/bottom internal leading and
    preventing any left/right glyph overhang clipping).
    """
    if not line_text or not line_text.strip():
        return None

    dummy = Image.new("L", (16, 16), 255)
    ddraw = ImageDraw.Draw(dummy)
    bbox = ddraw.textbbox((0, 0), line_text, font=font)
    est_w = max(16, bbox[2] - bbox[0])
    est_h = max(16, bbox[3] - bbox[1])

    pad = 64
    scratch = Image.new("L", (est_w + pad * 2, est_h + pad * 2), 255)
    sdraw = ImageDraw.Draw(scratch)
    sdraw.text((pad - bbox[0], pad - bbox[1]), line_text, fill=0, font=font)

    inv = ImageOps.invert(scratch)
    ink_box = inv.getbbox()
    if ink_box is None:
        return None
    return scratch.crop(ink_box)


def _stack_line_images(
    line_imgs: List[Image.Image],
    align: str = "center",
    line_gap_dots: int = 8,
    line_divider: bool = False,
) -> Optional[Image.Image]:
    """
    Stack per-line tight ink images vertically with exact `line_gap_dots` spacing
    and optional horizontal divider line between lines.
    """
    if not line_imgs:
        return None
    if len(line_imgs) == 1:
        return line_imgs[0]

    gap = max(0, int(line_gap_dots))
    div_extra = 4 if line_divider else 0
    eff_gap = max(gap, 6) + div_extra if line_divider else gap

    max_w = max(img.width for img in line_imgs)
    total_h = sum(img.height for img in line_imgs) + eff_gap * (len(line_imgs) - 1)

    out = Image.new("L", (max_w, max(1, total_h)), 255)
    draw = ImageDraw.Draw(out)

    cur_y = 0
    for idx, img in enumerate(line_imgs):
        if align == "left":
            x = 0
        elif align == "right":
            x = max_w - img.width
        else:
            x = (max_w - img.width) // 2
        out.paste(img, (x, cur_y))
        cur_y += img.height

        if idx < len(line_imgs) - 1:
            if line_divider:
                mid_y = cur_y + eff_gap // 2
                draw.line([(0, mid_y), (max_w - 1, mid_y)], fill=0, width=2)
            cur_y += eff_gap

    return out


def _render_text_tight(
    text: str,
    font_name: str,
    bold: bool,
    avail_height: int,
    auto_font_size: bool = True,
    manual_font_size_px: int = 84,
    align: str = "center",
    line_ratio_mode: str = "동일 크기 (1:1)",
    line_gap_dots: int = 8,
    line_divider: bool = False,
) -> Optional[Image.Image]:
    """
    Render 1-line, 2-line, or multi-line text with tight per-line ink cropping so
    2-line labels maximize vertical tape usage without TrueType internal leading waste.
    Supports proportional 2-line sizing (1:1, 6:4 title+subtitle, 4:6 subtitle+main).
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None

    n_lines = len(lines)
    gap = max(0, int(line_gap_dots))
    eff_gap = (max(gap, 6) + 4) if (line_divider and n_lines > 1) else gap
    total_gaps = eff_gap * (n_lines - 1)
    usable_ink_h = max(12 * n_lines, avail_height - 2 - total_gaps)

    # Determine height share per line when 2 lines use 6:4 or 4:6 ratio
    weights: List[float] = [1.0 / n_lines] * n_lines
    if n_lines == 2:
        if "6:4" in line_ratio_mode or "1줄 크게" in line_ratio_mode:
            weights = [0.60, 0.40]
        elif "4:6" in line_ratio_mode or "2줄 크게" in line_ratio_mode:
            weights = [0.40, 0.60]

    use_weighted_lines = n_lines == 2 and weights[0] != weights[1]

    if not auto_font_size:
        line_imgs: List[Image.Image] = []
        for idx, ln in enumerate(lines):
            scale = (weights[idx] / 0.5) if use_weighted_lines else 1.0
            fsize = max(10, int(round(manual_font_size_px * scale)))
            font = load_font(font_name, fsize, bold=bold)
            img = _render_single_line_tight(ln, font)
            if img is not None:
                line_imgs.append(img)
        return _stack_line_images(line_imgs, align=align, line_gap_dots=gap, line_divider=line_divider)

    # Auto-fit mode:
    if use_weighted_lines:
        # Binary-search each line independently to fit its allocated vertical share
        line_imgs = []
        for idx, ln in enumerate(lines):
            target_h = max(12, int(usable_ink_h * weights[idx]))
            best_img: Optional[Image.Image] = None
            lo, hi = 10, max(16, int(target_h * 1.6))
            while lo <= hi:
                mid = (lo + hi) // 2
                f_test = load_font(font_name, mid, bold=bold)
                cand = _render_single_line_tight(ln, f_test)
                if cand is not None and cand.height <= target_h:
                    best_img = cand
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best_img is None:
                best_img = _render_single_line_tight(ln, load_font(font_name, 12, bold=bold))
            if best_img is not None:
                line_imgs.append(best_img)
        return _stack_line_images(line_imgs, align=align, line_gap_dots=gap, line_divider=line_divider)

    # Uniform font size across all lines (1:1): binary-search a single font size
    # whose stacked tight-ink height fits within avail_height - 2
    best_stack: Optional[Image.Image] = None
    lo, hi = 10, max(16, int((usable_ink_h / n_lines) * 1.65))
    while lo <= hi:
        mid = (lo + hi) // 2
        f_test = load_font(font_name, mid, bold=bold)
        cand_imgs = []
        for ln in lines:
            c = _render_single_line_tight(ln, f_test)
            if c is not None:
                cand_imgs.append(c)
        stacked = _stack_line_images(cand_imgs, align=align, line_gap_dots=gap, line_divider=line_divider)
        if stacked is not None and stacked.height <= avail_height - 2:
            best_stack = stacked
            lo = mid + 1
        else:
            hi = mid - 1

    if best_stack is None:
        f_min = load_font(font_name, 12, bold=bold)
        cand_imgs = [c for c in (_render_single_line_tight(ln, f_min) for ln in lines) if c is not None]
        best_stack = _stack_line_images(cand_imgs, align=align, line_gap_dots=gap, line_divider=line_divider)

    return best_stack


def _make_qr_bitmap(qr_data: str, target_height: int) -> Optional[Image.Image]:
    """Generate a crisp 1-bit QR code scaled to fit target_height."""
    if not qr_data.strip() or qrcode is None:
        return None
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=4,
        border=1,
    )
    qr.add_data(qr_data.strip())
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("L")
    side = max(24, target_height)
    return qr_img.resize((side, side), Image.Resampling.NEAREST)


def _load_custom_image(image_path: str, target_height: int, dither: bool = False) -> Optional[Image.Image]:
    """Load and resize a user image to fit target_height across the tape."""
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        img = Image.open(image_path)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[3])
            img = bg.convert("L")
        else:
            img = img.convert("L")

        w, h = img.size
        if h <= 0 or w <= 0:
            return None
        new_h = max(16, target_height)
        new_w = max(16, int(round(w * (new_h / h))))
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        if dither:
            return resized.convert("1").convert("L")
        return resized
    except Exception:
        return None


def render_label_bitmap(design: LabelDesign) -> Image.Image:
    """
    Render the printable label content bitmap (horizontal orientation:
      height = printable_dots for design.tape_code,
      width  = content length in dots along the tape).
    """
    printable_dots = TAPE_PRINTABLE_DOTS.get(design.tape_code, 144)
    has_frame = design.frame_style != "없음 (None)" and not design.frame_style.startswith("없음")

    frame_inset = max(8, printable_dots // 12) if has_frame else 4
    avail_height = max(16, printable_dots - frame_inset * 2)
    inner_pad_x = max(16, mm_to_dots(design.inner_padding_mm))
    if has_frame:
        inner_pad_x = max(inner_pad_x, printable_dots // 5)

    # 1. Optional QR code & custom image
    qr_img = _make_qr_bitmap(design.qr_text, avail_height) if design.qr_text.strip() else None
    custom_img = _load_custom_image(design.image_path, avail_height, dither=design.image_dither)

    # 2. Text measurement & auto-sizing via exact per-line ink bounding box
    text_str = "" if (qr_img is not None and design.qr_position == "only") else design.text
    text_img: Optional[Image.Image] = None
    if text_str.strip():
        text_img = _render_text_tight(
            text=text_str,
            font_name=design.font_name,
            bold=design.bold,
            avail_height=avail_height,
            auto_font_size=design.auto_font_size,
            manual_font_size_px=design.font_size_px,
            align=design.h_align,
            line_ratio_mode=design.line_ratio_mode,
            line_gap_dots=design.line_gap_dots,
            line_divider=design.line_divider,
        )

    text_w = text_img.width if text_img is not None else 0

    # 3. Compute total content width along the tape
    elements_w = 0
    gap = max(14, printable_dots // 8)
    count_elems = 0

    if qr_img is not None:
        elements_w += qr_img.width
        count_elems += 1
    if custom_img is not None:
        elements_w += custom_img.width
        count_elems += 1
    if text_w > 0:
        elements_w += text_w
        count_elems += 1

    if count_elems > 1:
        elements_w += gap * (count_elems - 1)

    auto_total_w = max(72, elements_w + inner_pad_x * 2)
    if design.fixed_length_mm > 0:
        total_w = max(72, mm_to_dots(design.fixed_length_mm))
    else:
        total_w = auto_total_w

    # 4. Draw onto horizontal grayscale canvas (white=255, black=0)
    canvas = Image.new("L", (total_w, printable_dots), 255)
    draw = ImageDraw.Draw(canvas)

    start_x = inner_pad_x
    free_w = max(0, total_w - inner_pad_x * 2 - elements_w)
    if design.h_align == "center":
        start_x += free_w // 2
    elif design.h_align == "right":
        start_x += free_w

    cur_x = start_x

    # Draw QR on left if requested
    if qr_img is not None and design.qr_position in ("left", "only"):
        qy = (printable_dots - qr_img.height) // 2
        canvas.paste(qr_img, (cur_x, qy))
        cur_x += qr_img.width + gap

    # Draw custom image if present
    if custom_img is not None:
        iy = (printable_dots - custom_img.height) // 2
        canvas.paste(custom_img, (cur_x, iy))
        cur_x += custom_img.width + gap

    # Paste tight ink-cropped text image vertically centered
    if text_img is not None:
        ty = (printable_dots - text_img.height) // 2
        canvas.paste(text_img, (cur_x, ty))
        cur_x += text_img.width + gap

    # Draw QR on right if requested
    if qr_img is not None and design.qr_position == "right":
        qy = (printable_dots - qr_img.height) // 2
        canvas.paste(qr_img, (cur_x, qy))

    # 5. Draw Frame / Border
    if has_frame:
        rect = [1, 1, total_w - 2, printable_dots - 2]
        stroke = max(2, printable_dots // 36)
        if "직사각형" in design.frame_style:
            draw.rectangle(rect, outline=0, width=stroke)
        elif "둥근" in design.frame_style:
            radius = max(8, printable_dots // 5)
            draw.rounded_rectangle(rect, radius=radius, outline=0, width=stroke)
        elif "굵은" in design.frame_style:
            draw.rectangle(rect, outline=0, width=max(4, stroke * 2))
        elif "캡슐" in design.frame_style:
            radius = (printable_dots - 4) // 2
            draw.rounded_rectangle(rect, radius=radius, outline=0, width=stroke)

    # 6. Invert colors if requested (white text on black background)
    if design.invert_colors:
        canvas = ImageOps.invert(canvas)

    return canvas


def render_preview_image(
    label_bitmap: Image.Image,
    tape_code: int = 4,
    margin_mm: float = 0.0,
    lead_offset_mm: float = 0.0,
) -> Image.Image:
    """
    Create a realistic visual preview image showing:
      - Full physical tape height (including unprintable top/bottom tape borders)
      - Lead & trail margins (margin_mm + lead_offset_mm) with dashed red cut/margin lines
      - Printable zone boundaries (subtle blue dotted guide)
      - Exact 1-bit thresholded print preview matching LW-C610 output
    """
    printable_dots = label_bitmap.height
    content_w = label_bitmap.width
    tape_mm = TAPE_WIDTH_MM.get(tape_code, 12) or 12
    full_tape_dots = max(printable_dots + 16, mm_to_dots(tape_mm))
    margin_dots = max(0, mm_to_dots(margin_mm))
    lead_dots = margin_dots + max(0, mm_to_dots(lead_offset_mm))
    trail_dots = margin_dots

    total_preview_w = content_w + lead_dots + trail_dots
    pad_outer = 18

    img_w = total_preview_w + pad_outer * 2
    img_h = full_tape_dots + pad_outer * 2

    preview = Image.new("RGB", (img_w, img_h), (232, 236, 242))
    draw = ImageDraw.Draw(preview)

    # Draw tape drop shadow & white tape body
    tape_x0 = pad_outer
    tape_y0 = pad_outer
    tape_x1 = pad_outer + total_preview_w
    tape_y1 = pad_outer + full_tape_dots

    draw.rectangle([tape_x0 + 3, tape_y0 + 3, tape_x1 + 3, tape_y1 + 3], fill=(195, 202, 212))
    draw.rectangle([tape_x0, tape_y0, tape_x1, tape_y1], fill=(255, 255, 255), outline=(150, 160, 175), width=2)

    # Printable vertical band offset inside the physical tape
    print_y0 = tape_y0 + (full_tape_dots - printable_dots) // 2
    print_y1 = print_y0 + printable_dots

    # Shade non-printable top/bottom margins of the tape slightly
    if print_y0 > tape_y0 + 1:
        draw.rectangle([tape_x0 + 1, tape_y0 + 1, tape_x1 - 1, print_y0], fill=(247, 249, 252))
    if print_y1 < tape_y1 - 1:
        draw.rectangle([tape_x0 + 1, print_y1, tape_x1 - 1, tape_y1 - 1], fill=(247, 249, 252))

    # Threshold bitmap at 140 (0x8C) just like CommandFilter::bitmapToRaster
    bw = label_bitmap.convert("L").point(lambda p: 0 if p < 140 else 255, mode="L")
    rgb_content = Image.merge("RGB", (bw, bw, bw))
    preview.paste(rgb_content, (tape_x0 + lead_dots, print_y0))

    # Draw subtle horizontal printable boundary lines
    for x in range(tape_x0 + 4, tape_x1 - 4, 10):
        draw.line([(x, print_y0), (min(x + 5, tape_x1 - 4), print_y0)], fill=(180, 205, 235), width=1)
        draw.line([(x, print_y1), (min(x + 5, tape_x1 - 4), print_y1)], fill=(180, 205, 235), width=1)

    # Draw vertical dashed margin / content boundary lines
    left_margin_x = tape_x0 + lead_dots
    right_margin_x = tape_x0 + lead_dots + content_w
    for y in range(tape_y0 + 4, tape_y1 - 4, 10):
        y_end = min(y + 5, tape_y1 - 4)
        draw.line([(left_margin_x, y), (left_margin_x, y_end)], fill=(225, 100, 100), width=1)
        draw.line([(right_margin_x, y), (right_margin_x, y_end)], fill=(225, 100, 100), width=1)

    return preview
