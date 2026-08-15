import math
import os
import re
from io import BytesIO

from django.conf import settings
from lxml import etree

from .font_injector import inject_fonts_into_svg
from .svg_updater import update_svg_from_field_updates
from .svg_utils import apply_svg_patches
from .watermark import WaterMark


MAX_RENDER_SVG_BYTES = int(getattr(settings, "API_RENDER_MAX_SVG_BYTES", 50 * 1024 * 1024))
MAX_RENDER_PIXELS = int(getattr(settings, "API_RENDER_MAX_PIXELS", 25_000_000))
MAX_RENDER_DIMENSION = int(getattr(settings, "API_RENDER_MAX_DIMENSION", 8_192))
MAX_RENDER_OUTPUT_BYTES = int(getattr(settings, "API_RENDER_MAX_OUTPUT_BYTES", 50 * 1024 * 1024))

_BLOCKED_ELEMENTS = frozenset({
    "script", "foreignobject", "iframe", "object", "embed", "audio", "video",
    "animate", "animatemotion", "animatetransform", "set", "discard", "mpath", "link",
})
_SAFE_DATA_RESOURCE = re.compile(
    r"^data:(?:image/(?:png|jpeg|jpg|webp)|font/[a-z0-9.+-]+|application/(?:font-woff|font-woff2));base64,[a-z0-9+/]+={0,2}\Z",
    re.I,
)
_LEGACY_SAFE_RASTER = re.compile(
    r"^data:img/(?:png|jpeg|jpg|webp);base64,[a-z0-9+/]+={0,2}\Z",
    re.I,
)
_NUMBER = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)")
_CSS_VALUE_ATTRIBUTES = frozenset({
    "style", "fill", "stroke", "filter", "clip-path", "mask", "cursor",
    "marker", "marker-start", "marker-mid", "marker-end",
})


class RenderInputError(ValueError):
    pass


def _normalize_safe_resource(value: str) -> str | None:
    if _SAFE_DATA_RESOURCE.match(value):
        return value
    if _LEGACY_SAFE_RASTER.match(value):
        return re.sub(r"^data:img/", "data:image/", value, count=1, flags=re.I)
    return None


def _unsafe_css(value: str) -> bool:
    if "\\" in value:
        return True
    value = re.sub(r"/\*.*?\*/", "", value, flags=re.S)
    if re.search(r"(?:expression\s*\(|javascript:|@import|-moz-binding|behavior\s*:)", value, re.I):
        return True
    url_pattern = r"url\s*\(\s*(['\"]?)(.*?)\1\s*\)"
    for match in re.finditer(url_pattern, value, re.I):
        target = match.group(2).strip()
        if not target.startswith("#") and not _SAFE_DATA_RESOURCE.match(target):
            return True
    without_safe_urls = re.sub(url_pattern, "", value, flags=re.I)
    if re.search(r"(?:https?|ftp):|//", without_safe_urls, re.I):
        return True
    return False


def sanitize_svg_for_render(svg: str) -> str:
    if not isinstance(svg, str) or not svg or len(svg.encode("utf-8")) > MAX_RENDER_SVG_BYTES:
        raise RenderInputError("The SVG is missing or too large.")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_comments=True,
    )
    try:
        root = etree.fromstring(svg.encode("utf-8"), parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise RenderInputError("The SVG is invalid.") from exc
    if etree.QName(root).localname.lower() != "svg":
        raise RenderInputError("The document root must be SVG.")
    if root.getroottree().docinfo.doctype:
        raise RenderInputError("SVG document type declarations are not allowed.")

    nodes = list(root.iter())
    if len(nodes) > 100_000:
        raise RenderInputError("The SVG contains too many elements.")
    for element in nodes:
        tag = etree.QName(element).localname.lower()
        if tag in _BLOCKED_ELEMENTS:
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
            continue
        if tag == "style" and _unsafe_css(element.text or ""):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
            continue
        for raw_name, raw_value in list(element.attrib.items()):
            name = etree.QName(raw_name).localname.lower()
            value = str(raw_value).strip()
            if name.startswith("on") or value.lower().startswith("javascript:"):
                del element.attrib[raw_name]
                continue
            if name in {"href", "src"} and not value.startswith("#"):
                safe_resource = _normalize_safe_resource(value)
                if safe_resource:
                    element.attrib[raw_name] = safe_resource
                else:
                    del element.attrib[raw_name]
                continue
            if name in _CSS_VALUE_ATTRIBUTES and _unsafe_css(value):
                del element.attrib[raw_name]

    rendered = etree.tostring(root, encoding="unicode", xml_declaration=False)
    if len(rendered.encode("utf-8")) > MAX_RENDER_SVG_BYTES:
        raise RenderInputError("The assembled SVG is too large.")
    return rendered


def _read_svg_file(file_field) -> str:
    if not file_field:
        return ""
    with file_field.open("rb") as handle:
        raw = handle.read(MAX_RENDER_SVG_BYTES + 1)
    if len(raw) > MAX_RENDER_SVG_BYTES:
        raise RenderInputError("The SVG is too large.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RenderInputError("The SVG is not valid UTF-8.") from exc


def assemble_document_svg(document) -> str:
    source = document.svg_file or (document.template.svg_file if document.template else None)
    svg = sanitize_svg_for_render(_read_svg_file(source))
    if document.svg_patches:
        svg = apply_svg_patches(svg, document.svg_patches)
    if document.form_fields:
        updates = [
            {"id": field.get("id"), "value": field.get("currentValue")}
            for field in document.form_fields
            if field.get("id") and "currentValue" in field
        ]
        if updates:
            svg, _ = update_svg_from_field_updates(svg, document.form_fields, updates)
    fonts = list(document.fonts.all())
    if not fonts and document.template:
        fonts = list(document.template.fonts.all())
    if fonts:
        svg = inject_fonts_into_svg(svg, fonts, embed_base64=True)
    if document.test:
        svg = WaterMark().add_watermark(svg)
    return sanitize_svg_for_render(svg)


def svg_dimensions(svg: str) -> tuple[int, int]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False, huge_tree=False)
    root = etree.fromstring(svg.encode("utf-8"), parser=parser)

    def numeric(attribute):
        match = _NUMBER.match(root.get(attribute, ""))
        return float(match.group(1)) if match else None

    width = numeric("width")
    height = numeric("height")
    if not width or not height:
        parts = re.split(r"[\s,]+", root.get("viewBox", "").strip())
        if len(parts) == 4:
            try:
                width = width or float(parts[2])
                height = height or float(parts[3])
            except ValueError:
                pass
    width = math.ceil(width or 800)
    height = math.ceil(height or 600)
    if width < 1 or height < 1 or width > MAX_RENDER_DIMENSION or height > MAX_RENDER_DIMENSION:
        raise RenderInputError("The SVG dimensions are outside the render limit.")
    return width, height


def render_svg_with_chromium(svg: str, output_format: str) -> bytes:
    if output_format not in {"png", "pdf"}:
        raise RenderInputError("Unsupported render format.")
    width, height = svg_dimensions(svg)
    scale = 2 if width * height * 4 <= MAX_RENDER_PIXELS else 1
    if width * height * (scale ** 2) > MAX_RENDER_PIXELS:
        raise RenderInputError("The rendered document would be too large.")

    from playwright.sync_api import sync_playwright

    executable_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or None
    launch_options = {
        "headless": True,
        "args": ["--disable-dev-shm-usage"],
    }
    if executable_path:
        launch_options["executable_path"] = executable_path

    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "html,body{margin:0;padding:0;background:#fff;overflow:hidden}"
        f"svg{{display:block;width:{width}px;height:{height}px}}"
        "</style></head><body>" + svg + "</body></html>"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=scale,
                java_script_enabled=False,
                service_workers="block",
            )
            page = context.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(html, wait_until="load", timeout=15_000)
            page.locator("svg").wait_for(state="visible", timeout=5_000)
            if output_format == "png":
                return page.locator("svg").screenshot(type="png", animations="disabled", scale="device")
            return page.pdf(
                width=f"{width}px",
                height=f"{height}px",
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
        finally:
            browser.close()


def verify_render_output(payload: bytes, output_format: str) -> None:
    if not payload or len(payload) > MAX_RENDER_OUTPUT_BYTES:
        raise RenderInputError("The renderer returned an invalid file.")
    if output_format == "png":
        from PIL import Image

        image = Image.open(BytesIO(payload))
        image.verify()
    elif not payload.startswith(b"%PDF-"):
        raise RenderInputError("The renderer returned an invalid PDF.")
