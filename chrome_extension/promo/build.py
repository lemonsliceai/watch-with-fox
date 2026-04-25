"""Compose Chrome Web Store promo tiles for Couchverse.

Reuses the website's neo-brutalist design language: yolk/cream backgrounds,
thick ink borders, hard offset shadows, fox & alien character art.

Run:  python3 chrome_extension/promo/build.py
"""

from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# Brand palette (from couchverse-web/app/globals.css)
INK = (42, 24, 16)
INK_SOFT = (90, 58, 40)
PAPER = (255, 250, 236)
CREAM = (250, 242, 220)
CREAM_DARK = (240, 227, 191)
FOX = (255, 106, 26)
FOX_DEEP = (196, 74, 8)
ALIEN = (126, 212, 194)
ALIEN_DEEP = (62, 157, 138)
CHEEK = (244, 163, 184)
YOLK = (252, 212, 73)
PLUM = (110, 60, 138)

HERE = Path(__file__).resolve().parent
ICONS = HERE.parent / "icons"

BRICOLAGE = str(HERE / "Bricolage-ExtraBold.ttf")
FOX_PORTRAIT = ICONS / "fox_2x3.jpg"
ALIEN_PORTRAIT = ICONS / "alien_2x3.jpg"
OUT = HERE


# ---------- helpers ----------

def font(path: str, size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=size, index=index)


def measure(draw: ImageDraw.ImageDraw, text: str, fnt) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=fnt)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def crop_face(path: Path, size: int, focus_y_frac: float = 0.32) -> Image.Image:
    """Center-crop a portrait JPG to a square focused on the face."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    side = min(w, h)
    cx = w // 2
    cy = int(h * focus_y_frac)
    left = max(0, cx - side // 2)
    top = max(0, cy - side // 2)
    if top + side > h:
        top = h - side
    if left + side > w:
        left = w - side
    img = img.crop((left, top, left + side, top + side))
    return img.resize((size, size), Image.LANCZOS)


def circle_mask(size: int) -> Image.Image:
    # PIL's ellipse() on an L canvas isn't antialiased, so supersample 4x and
    # downsample with LANCZOS. Yields smooth edges instead of staircased ones.
    s = size * 4
    m = Image.new("L", (s, s), 0)
    ImageDraw.Draw(m).ellipse((0, 0, s - 1, s - 1), fill=255)
    return m.resize((size, size), Image.LANCZOS)


def paste_circle(canvas: Image.Image, src: Image.Image, cx: int, cy: int,
                 radius: int, border_color=INK, border_width: int = 4,
                 bg_fill=None):
    size = radius * 2
    if src.size != (size, size):
        src = src.resize((size, size), Image.LANCZOS)
    # Build the framed circle (image + border) on its own RGBA canvas at 4x,
    # then composite onto the JPEG canvas. Same supersampling trick gives the
    # circular border antialiased edges too.
    s = size * 4
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    if bg_fill is not None:
        ImageDraw.Draw(layer).ellipse((0, 0, s - 1, s - 1), fill=bg_fill + (255,))
    # Paste image through a circular mask drawn at 4x
    img_big = src.resize((s, s), Image.LANCZOS).convert("RGBA")
    mask_big = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask_big).ellipse((0, 0, s - 1, s - 1), fill=255)
    layer.paste(img_big, (0, 0), mask_big)
    # Border ring
    ImageDraw.Draw(layer).ellipse(
        (0, 0, s - 1, s - 1),
        outline=border_color + (255,),
        width=border_width * 4,
    )
    layer = layer.resize((size, size), Image.LANCZOS)
    canvas.paste(layer, (cx - radius, cy - radius), layer)


def paste_rotated_card(canvas: Image.Image, src: Image.Image,
                       center: tuple[int, int], size: tuple[int, int],
                       angle_deg: float, radius: int,
                       border_color=INK, border_width: int = 6,
                       shadow_offset: tuple[int, int] = (10, 10),
                       label: str | None = None,
                       label_bg=PAPER):
    """Paste src as a rounded-rect card, rotated, with a hard offset shadow."""
    cw, ch = size
    # Build the card on a transparent canvas at native size
    card = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    # Card body (rounded rect with the character image cropped/scaled to fill)
    img_h = ch - (28 if label else 0)
    sw, sh = src.size
    # cover-fit the source into (cw, img_h)
    scale = max(cw / sw, img_h / sh)
    rw, rh = int(sw * scale), int(sh * scale)
    fitted = src.resize((rw, rh), Image.LANCZOS)
    fx = (cw - rw) // 2
    fy = (img_h - rh) // 2
    # Mask: rounded-top rect (full rounded if no label)
    mask = Image.new("L", (cw, ch), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, cw - 1, ch - 1), radius=radius, fill=255)
    # Image area only:
    image_area_mask = Image.new("L", (cw, img_h), 0)
    iam = ImageDraw.Draw(image_area_mask)
    if label:
        # Round only the top corners
        iam.rounded_rectangle((0, 0, cw - 1, img_h + radius), radius=radius, fill=255)
    else:
        iam.rounded_rectangle((0, 0, cw - 1, img_h - 1), radius=radius, fill=255)
    img_layer = Image.new("RGBA", (cw, img_h), (0, 0, 0, 0))
    img_layer.paste(fitted, (fx, fy))
    img_layer.putalpha(image_area_mask)
    card.paste(img_layer, (0, 0), img_layer)

    if label:
        # Bottom strip
        strip = Image.new("RGBA", (cw, 28), label_bg + (255,))
        strip_mask = Image.new("L", (cw, 28), 0)
        ImageDraw.Draw(strip_mask).rounded_rectangle(
            (0, -radius, cw - 1, 28 - 1), radius=radius, fill=255
        )
        card.paste(strip, (0, ch - 28), strip_mask)
        # Divider
        ImageDraw.Draw(card).line(
            [(0, ch - 28), (cw - 1, ch - 28)], fill=INK, width=3
        )
        # Label text
        label_font = font(BRICOLAGE, 16)
        lw, lh = measure(ImageDraw.Draw(card), label, label_font)
        ImageDraw.Draw(card).text(
            ((cw - lw) // 2, ch - 28 + (28 - lh) // 2 - 2),
            label, fill=INK, font=label_font,
        )

    # Outer border
    ImageDraw.Draw(card).rounded_rectangle(
        (0, 0, cw - 1, ch - 1), radius=radius,
        outline=border_color, width=border_width,
    )

    # Build shadow on a slightly larger canvas, then composite both
    pad = 24
    big = Image.new("RGBA", (cw + pad * 2, ch + pad * 2), (0, 0, 0, 0))
    # Hard offset shadow (solid color, no blur — matches website style)
    shadow = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((0, 0, cw - 1, ch - 1), radius=radius, fill=INK + (255,))
    big.paste(shadow, (pad + shadow_offset[0], pad + shadow_offset[1]), shadow)
    big.paste(card, (pad, pad), card)

    rotated = big.rotate(angle_deg, resample=Image.BICUBIC, expand=True)
    rx, ry = rotated.size
    canvas.paste(rotated, (center[0] - rx // 2, center[1] - ry // 2), rotated)


def pill(canvas: Image.Image, xy: tuple[int, int], text: str, fnt,
         bg=PAPER, fg=INK, border=INK, border_w=3, pad=(18, 10),
         dot_color=None, shadow=None):
    """Draw a rounded 'pill' button at (x, y) anchored top-left, return its bbox."""
    x, y = xy
    tw, th = measure(ImageDraw.Draw(canvas), text, fnt)
    extra = 22 if dot_color else 0
    w = tw + pad[0] * 2 + extra
    h = th + pad[1] * 2
    radius = h // 2
    if shadow:
        sx, sy = shadow
        ImageDraw.Draw(canvas).rounded_rectangle(
            (x + sx, y + sy, x + sx + w, y + sy + h), radius=radius, fill=INK
        )
    ImageDraw.Draw(canvas).rounded_rectangle(
        (x, y, x + w, y + h), radius=radius, fill=bg, outline=border, width=border_w
    )
    text_x = x + pad[0]
    if dot_color:
        dot_r = th // 2
        ImageDraw.Draw(canvas).ellipse(
            (x + pad[0], y + (h - dot_r * 2) // 2,
             x + pad[0] + dot_r * 2, y + (h - dot_r * 2) // 2 + dot_r * 2),
            fill=dot_color,
        )
        text_x += dot_r * 2 + 8
    ImageDraw.Draw(canvas).text((text_x, y + pad[1] - 2), text, fill=fg, font=fnt)
    return (x, y, x + w, y + h)


def speech_bubble(canvas: Image.Image, xy: tuple[int, int], text_lines: list[str],
                  fnt, bg=PAPER, fg=INK, border=INK, border_w=3,
                  pad=(20, 16), tail_side="left", radius=18,
                  shadow=(6, 6)):
    """Draw a speech bubble with a tail; xy is top-left anchor."""
    x, y = xy
    line_h = fnt.size + 6
    widths = [measure(ImageDraw.Draw(canvas), ln, fnt)[0] for ln in text_lines]
    tw = max(widths)
    th = line_h * len(text_lines)
    w = tw + pad[0] * 2
    h = th + pad[1] * 2
    if shadow:
        sx, sy = shadow
        ImageDraw.Draw(canvas).rounded_rectangle(
            (x + sx, y + sy, x + sx + w, y + sy + h), radius=radius, fill=INK
        )
    ImageDraw.Draw(canvas).rounded_rectangle(
        (x, y, x + w, y + h), radius=radius, fill=bg, outline=border, width=border_w
    )
    for i, ln in enumerate(text_lines):
        ImageDraw.Draw(canvas).text(
            (x + pad[0], y + pad[1] + i * line_h - 2), ln, fill=fg, font=fnt
        )
    # Tail (triangle, with border)
    if tail_side == "left":
        tx, ty = x + 36, y + h
        pts = [(tx, ty - 2), (tx + 18, ty - 2), (tx + 4, ty + 22)]
    else:
        tx, ty = x + w - 56, y + h
        pts = [(tx, ty - 2), (tx + 18, ty - 2), (tx + 14, ty + 22)]
    # Fill triangle in bg
    ImageDraw.Draw(canvas).polygon(pts, fill=bg)
    # Outline left + bottom edges only (hide top edge that overlaps the bubble)
    ImageDraw.Draw(canvas).line([pts[0], pts[2]], fill=border, width=border_w)
    ImageDraw.Draw(canvas).line([pts[2], pts[1]], fill=border, width=border_w)


def sticker(canvas: Image.Image, center: tuple[int, int], text: str, fnt,
            bg=YOLK, fg=INK, angle_deg=-6):
    """Draw a tilted rounded-pill sticker with hard shadow."""
    tw, th = measure(ImageDraw.Draw(canvas), text, fnt)
    pad = (18, 10)
    w = tw + pad[0] * 2
    h = th + pad[1] * 2
    radius = h // 2
    pad_outer = 24
    big = Image.new("RGBA", (w + pad_outer * 2, h + pad_outer * 2), (0, 0, 0, 0))
    bd = ImageDraw.Draw(big)
    # shadow
    bd.rounded_rectangle(
        (pad_outer + 5, pad_outer + 5, pad_outer + w + 5, pad_outer + h + 5),
        radius=radius, fill=INK,
    )
    bd.rounded_rectangle(
        (pad_outer, pad_outer, pad_outer + w, pad_outer + h),
        radius=radius, fill=bg, outline=INK, width=3,
    )
    bd.text((pad_outer + pad[0], pad_outer + pad[1] - 2), text, fill=fg, font=fnt)
    rotated = big.rotate(angle_deg, resample=Image.BICUBIC, expand=True)
    rx, ry = rotated.size
    canvas.paste(rotated, (center[0] - rx // 2, center[1] - ry // 2), rotated)


# ---------- SMALL PROMO 440x280 ----------

def make_small():
    W, H = 440, 280
    img = Image.new("RGB", (W, H), YOLK)
    draw = ImageDraw.Draw(img)
    # No outer rounded border — JPEG has no alpha to clip the corners cleanly.
    # Yolk fills edge-to-edge; the listing UI provides its own framing.

    M = 36  # uniform margin

    # Two-line headline, top-aligned
    h_font = font(BRICOLAGE, 52)
    line_h = 58
    draw.text((M, M), "NEVER WATCH", fill=INK, font=h_font)
    draw.text((M, M + line_h), "ALONE.", fill=INK, font=h_font)

    # Hand-drawn yolk-deep underline tick under "ALONE." for accent
    line2_w, _ = measure(draw, "ALONE.", h_font)
    underline_y = M + line_h + 56
    draw.line(
        [(M, underline_y), (M + line2_w, underline_y)],
        fill=FOX_DEEP, width=6,
    )

    # Bottom row: character duo + wordmark stack
    avatar_r = 30
    pair_cx_left = M + avatar_r
    pair_cx_right = pair_cx_left + avatar_r * 2 + 12
    avatar_cy = H - M - avatar_r

    fox_face = crop_face(FOX_PORTRAIT, 240, focus_y_frac=0.28)
    alien_face = crop_face(ALIEN_PORTRAIT, 240, focus_y_frac=0.30)
    paste_circle(img, fox_face, cx=pair_cx_left, cy=avatar_cy,
                 radius=avatar_r, border_width=3)
    paste_circle(img, alien_face, cx=pair_cx_right, cy=avatar_cy,
                 radius=avatar_r, border_width=3)

    # Wordmark stack to the right of avatars
    wm_font = font(BRICOLAGE, 22)
    sub_font = font(BRICOLAGE, 15)
    text_x = pair_cx_right + avatar_r + 18
    wm = "COUCHVERSE"
    sub = "Live AI hecklers in your tab"
    _, wm_h = measure(draw, wm, wm_font)
    _, sub_h = measure(draw, sub, sub_font)
    block_h = wm_h + 6 + sub_h
    block_y = avatar_cy - block_h // 2 - 4
    draw.text((text_x, block_y), wm, fill=INK, font=wm_font)
    draw.text((text_x, block_y + wm_h + 6), sub, fill=INK_SOFT, font=sub_font)

    img.save(OUT / "small-promo-440x280.jpg", "JPEG", quality=92, optimize=True)
    print(f"wrote {OUT / 'small-promo-440x280.jpg'}")


# ---------- MARQUEE 1400x560 ----------

def make_marquee():
    W, H = 1400, 560
    img = Image.new("RGB", (W, H), CREAM)
    draw = ImageDraw.Draw(img)

    # Outer ink frame
    draw.rectangle((0, 0, W - 1, H - 1), outline=INK, width=10)

    # Layout margins — bumped on the right and bottom to give the composition room
    LX = 80
    RX = W - 76  # right boundary for content (cards live to the left of this)
    BY = H - 64  # bottom boundary for content

    # ===== LEFT HALF: copy + CTA =====
    pill_font = font(BRICOLAGE, 22)
    pill(img, (LX, 64), "LIVE NOW IN YOUR TAB", pill_font,
         bg=PAPER, dot_color=FOX, pad=(20, 10),
         shadow=(4, 4))

    # Headline (2 lines, big)
    headline_font = font(BRICOLAGE, 100)
    line1_y = 124
    line2_y = line1_y + 106
    draw.text((LX, line1_y), "Never watch", fill=INK, font=headline_font)
    # "alone again." with yolk highlight under "alone"
    alone_w, _ = measure(draw, "alone", headline_font)
    draw.rectangle(
        (LX - 2, line2_y + 56, LX + alone_w + 6, line2_y + 92),
        fill=YOLK,
    )
    draw.text((LX, line2_y), "alone", fill=INK, font=headline_font)
    draw.text((LX + alone_w, line2_y), " again.", fill=INK, font=headline_font)

    # Subhead — single line keeps the right column from feeling crowded
    sub_font = font(BRICOLAGE, 24)
    sub_y = line2_y + 124
    draw.text(
        (LX, sub_y),
        "Fox & Alien — live AI hecklers in your Chrome tab.",
        fill=INK_SOFT, font=sub_font,
    )

    # CTA pill (fox bg, paper text, hard shadow) — pulled up off the bottom
    cta_font = font(BRICOLAGE, 26)
    cta_y = sub_y + 56
    pill(img, (LX, cta_y), "Add to Chrome — Free.", cta_font,
         bg=FOX, fg=PAPER, pad=(26, 16), shadow=(8, 8))

    # ===== RIGHT HALF: characters + speech bubble + stickers =====
    fox_img = Image.open(FOX_PORTRAIT).convert("RGB")
    alien_img = Image.open(ALIEN_PORTRAIT).convert("RGB")

    # Cards live entirely inside the 76px right margin and 64px bottom margin
    # (with rotation/shadow overhead accounted for).
    card_w, card_h = 220, 320
    cards_cy = 300
    fox_cx = 935
    alien_cx = 1155
    paste_rotated_card(
        img, fox_img,
        center=(fox_cx, cards_cy), size=(card_w, card_h),
        angle_deg=-4, radius=22, border_width=5,
        shadow_offset=(8, 8), label="FOX", label_bg=FOX,
    )
    paste_rotated_card(
        img, alien_img,
        center=(alien_cx, cards_cy), size=(card_w, card_h),
        angle_deg=4, radius=22, border_width=5,
        shadow_offset=(8, 8), label="ALIEN", label_bg=ALIEN,
    )

    # Speech bubble — sits above the fox card; the bubble itself is the "hot take",
    # so no separate HOT TAKE sticker (which would clutter and overlap the text).
    bubble_font = font(BRICOLAGE, 22)
    speech_bubble(
        img, (885, 80),
        ['"He just said \'synergy\'',
         'unironically. Mute him."'],
        bubble_font, pad=(22, 14), tail_side="left", radius=20,
        shadow=(6, 6),
    )

    # Single accent sticker, anchored to the alien card
    stk_font = font(BRICOLAGE, 18)
    sticker(img, (1235, 475), "UNHINGED", stk_font, bg=ALIEN, angle_deg=6)

    img.save(OUT / "marquee-promo-1400x560.jpg", "JPEG", quality=92, optimize=True)
    print(f"wrote {OUT / 'marquee-promo-1400x560.jpg'}")


if __name__ == "__main__":
    make_small()
    make_marquee()
