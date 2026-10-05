"""Validate avatar downloads before passing the same image to cards and models."""

import io

from PIL import Image, ImageOps


def normalize_avatar(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("头像尺寸过大")
        image.load()  # Reject truncated downloads instead of silently losing the avatar.
        image = ImageOps.exif_transpose(image).convert("RGBA")
        image.thumbnail((640, 640))
        canvas = Image.new("RGBA", image.size, "white")
        canvas.alpha_composite(image)
        output = io.BytesIO()
        canvas.convert("RGB").save(output, "PNG")
        return output.getvalue()
