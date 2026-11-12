"""Looking at a picture file (a photo sent from the phone, a saved screenshot...)."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from pydantic import Field

from jarvis.safety import Access, Assessment
from jarvis.tools.base import ImagePart, Tool, ToolContext, ToolError, ToolInput, ToolResult, Trimmed


class ViewImageInput(ToolInput):
    path: Trimmed = Field(description="A picture file (PNG, JPEG, GIF, WebP, BMP) inside the approved folders.")


def _shrink(path: Path, max_edge: int = 1568) -> tuple[bytes, str, tuple[int, int]]:
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80)
        return out.getvalue(), "image/jpeg", img.size


def _assess(a: ViewImageInput, ctx: ToolContext) -> Assessment:
    return Assessment(title=f"Look at the picture {a.path[-80:]}", reads_private=True, ingests_untrusted=True)


async def _run(a: ViewImageInput, ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(a.path, Access.READ)
    if not path.is_file():
        raise ToolError(f"{path} is not a file.")
    try:
        data, mime, size = await asyncio.to_thread(_shrink, path)
    except Exception as exc:  # noqa: BLE001 - not a picture, or damaged
        raise ToolError(f"{path.name} could not be opened as a picture: {exc}") from exc
    return ToolResult({"path": str(path), "width": size[0], "height": size[1]}, f"looked at {path.name}", images=[ImagePart(mime, data)],
                      untrusted_source=f"the picture {path.name}")


IMAGE_TOOLS: list[Tool] = [
    Tool("view_image", "Look at a picture file, such as a photo or screenshot the user sent from their phone.", ViewImageInput, _run, _assess, group="files"),
]
