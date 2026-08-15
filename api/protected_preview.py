import base64
import binascii
import json
import os
import re
from pathlib import Path

from django.conf import settings
from lxml import etree

from .font_injector import inject_fonts_into_svg
from .rendering import _read_svg_file, sanitize_svg_for_render, svg_dimensions
from .svg_utils import apply_svg_patches


class ProtectedPreviewError(ValueError):
    pass


PROTECTED_PREVIEW_VERSION = 2


_DATA_IMAGE = re.compile(
    r"^data:(?:image|img)/(png|jpeg|jpg|webp);base64,([A-Za-z0-9+/]+={0,2})$",
    re.I,
)
_CONTENT_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
}


def _preview_directory(template) -> Path:
    version = int(template.updated_at.timestamp())
    return Path(settings.MEDIA_ROOT) / "api" / "protected_previews" / str(template.id) / str(version)


def _source_svg(template) -> str:
    svg = sanitize_svg_for_render(_read_svg_file(template.svg_file))
    if template.svg_patches:
        svg = apply_svg_patches(svg, template.svg_patches)
    fonts = list(template.fonts.all())
    if fonts:
        svg = inject_fonts_into_svg(svg, fonts, embed_base64=True)
    return sanitize_svg_for_render(svg)


def _find_element(root, element_id: str):
    matches = root.xpath(
        "//*[@id=$identifier or @data-internal-id=$identifier or @data-name=$identifier]",
        identifier=element_id,
    )
    return matches[0] if matches else None


def _field_targets(template, root):
    targets = []
    seen = set()
    for field in template.form_fields or []:
        field_type = (field.get("type") or "text").lower()
        if field_type in {"upload", "file", "sign", "qrcode", "barcode"}:
            raise ProtectedPreviewError(
                f"Protected preview does not yet support {field_type!r} fields."
            )
        options = field.get("options") or []
        descriptors = (
            [
                {
                    "element_id": option.get("svgElementId"),
                    "option_value": str(option.get("value", "")),
                }
                for option in options
            ]
            if options
            else [{"element_id": field.get("svgElementId") or field.get("id"), "option_value": None}]
        )
        for descriptor in descriptors:
            element_id = descriptor["element_id"]
            if not element_id or element_id in seen:
                continue
            element = _find_element(root, element_id)
            if element is None:
                raise ProtectedPreviewError(f"Preview element {element_id!r} was not found.")
            tag = etree.QName(element).localname.lower()
            if tag not in {"text", "image"}:
                raise ProtectedPreviewError(
                    f"Protected preview does not yet support <{tag}> field element {element_id!r}."
                )
            if element.getparent() is not root:
                raise ProtectedPreviewError("Protected preview currently requires top-level field elements.")
            if tag == "text" and len(element.xpath(".//*[local-name()='tspan']")) > 1:
                raise ProtectedPreviewError("Protected preview currently supports one text run per field element.")
            targets.append({
                **descriptor,
                "field_id": field.get("id"),
                "field_type": field_type,
                "inverted": bool(field.get("inverted")),
                "tag": tag,
                "element": element,
            })
            seen.add(element_id)
    return targets


def _assert_static_artwork_precedes_fields(root, targets):
    dynamic = {target["element"] for target in targets}
    children = [
        child
        for child in root
        if etree.QName(child).localname.lower() not in {"defs", "title", "desc", "metadata"}
    ]
    first_dynamic = next((index for index, child in enumerate(children) if child in dynamic), None)
    if first_dynamic is None:
        raise ProtectedPreviewError("The template has no protected preview fields.")
    if any(child not in dynamic for child in children[first_dynamic:]):
        raise ProtectedPreviewError(
            "Protected preview currently requires static artwork to appear before editable SVG layers."
        )


def _write_image_asset(directory: Path, target, asset_index: int):
    element = target["element"]
    href = element.get("href") or element.get("{http://www.w3.org/1999/xlink}href") or ""
    match = _DATA_IMAGE.fullmatch(href.strip())
    if not match:
        raise ProtectedPreviewError(
            f"Protected preview only accepts embedded PNG, JPEG, or WebP field images ({target['element_id']!r})."
        )
    extension = "jpg" if match.group(1).lower() in {"jpeg", "jpg"} else match.group(1).lower()
    try:
        payload = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProtectedPreviewError("A protected preview image is invalid.") from exc
    if not payload or len(payload) > 25 * 1024 * 1024:
        raise ProtectedPreviewError("A protected preview image is missing or too large.")
    asset_id = f"layer-{asset_index}"
    filename = f"{asset_id}.{extension}"
    (directory / filename).write_bytes(payload)
    return asset_id, filename, _CONTENT_TYPES[match.group(1).lower()]


def _browser_metadata(svg: str, entries: list[dict], base_path: Path):
    from playwright.sync_api import sync_playwright

    width, height = svg_dimensions(svg)
    executable_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or None
    launch_options = {"headless": True, "args": ["--disable-dev-shm-usage"]}
    if executable_path:
        launch_options["executable_path"] = executable_path
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "html,body{margin:0;padding:0;background:transparent;overflow:hidden}"
        f"svg{{display:block;width:{width}px;height:{height}px}}"
        "</style></head><body>" + svg + "</body></html>"
    )
    script = r"""
    (entries) => {
      const svg = document.querySelector("svg");
      const all = Array.from(svg.querySelectorAll("*"));
      const numberValue = (list, fallback) => {
        try { return list && list.numberOfItems ? list.getItem(0).value : fallback; }
        catch (_) { return fallback; }
      };
      const matrixValue = (element) => {
        const matrix = element.getCTM();
        return matrix
          ? { a: matrix.a, b: matrix.b, c: matrix.c, d: matrix.d, e: matrix.e, f: matrix.f }
          : { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 };
      };
      const find = (identifier) => all.find((element) =>
        element.id === identifier ||
        element.getAttribute("data-internal-id") === identifier ||
        element.getAttribute("data-name") === identifier
      );
      return entries.map((entry) => {
        const element = find(entry.element_id);
        if (!element) throw new Error(`Missing protected preview element: ${entry.element_id}`);
        const style = getComputedStyle(element);
        const common = {
          ...entry,
          order: all.indexOf(element),
          opacity: Number.parseFloat(style.opacity || "1"),
          matrix: matrixValue(element),
        };
        if (entry.kind === "image") {
          return {
            ...common,
            x: element.x && element.x.baseVal ? element.x.baseVal.value : 0,
            y: element.y && element.y.baseVal ? element.y.baseVal.value : 0,
            width: element.width && element.width.baseVal ? element.width.baseVal.value : 0,
            height: element.height && element.height.baseVal ? element.height.baseVal.value : 0,
          };
        }
        const run = element.querySelector("tspan") || element;
        const runStyle = getComputedStyle(run);
        return {
          ...common,
          matrix: matrixValue(run),
          x: numberValue(run.x && run.x.baseVal, numberValue(element.x && element.x.baseVal, 0)),
          y: numberValue(run.y && run.y.baseVal, numberValue(element.y && element.y.baseVal, 0)),
          fontFamily: runStyle.fontFamily || style.fontFamily,
          fontSize: Number.parseFloat(runStyle.fontSize || style.fontSize || "16"),
          fontWeight: runStyle.fontWeight || style.fontWeight || "400",
          fontStyle: runStyle.fontStyle || style.fontStyle || "normal",
          letterSpacing: runStyle.letterSpacing || style.letterSpacing || "0px",
          fill: runStyle.fill || style.fill || "#000000",
          textAnchor: runStyle.textAnchor || style.textAnchor || "start",
        };
      });
    }
    """
    hide_script = r"""
    (identifiers) => {
      const elements = Array.from(document.querySelectorAll("svg *"));
      for (const identifier of identifiers) {
        const element = elements.find((candidate) =>
          candidate.id === identifier ||
          candidate.getAttribute("data-internal-id") === identifier ||
          candidate.getAttribute("data-name") === identifier
        );
        if (element) element.style.setProperty("visibility", "hidden", "important");
      }
    }
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
                java_script_enabled=True,
                service_workers="block",
            )
            page = context.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(html, wait_until="load", timeout=15_000)
            page.locator("svg").wait_for(state="visible", timeout=5_000)
            metadata = page.evaluate(script, entries)
            page.evaluate(hide_script, [entry["element_id"] for entry in entries])
            page.locator("svg").screenshot(
                path=str(base_path),
                type="png",
                animations="disabled",
                omit_background=True,
                scale="css",
            )
            return width, height, metadata
        finally:
            browser.close()


def build_protected_preview(template) -> dict:
    directory = _preview_directory(template)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        cached = json.loads(manifest_path.read_text(encoding="utf-8"))
        if cached.get("version") == PROTECTED_PREVIEW_VERSION:
            return cached

    directory.mkdir(parents=True, exist_ok=True)
    svg = _source_svg(template)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False, huge_tree=False)
    root = etree.fromstring(svg.encode("utf-8"), parser=parser)
    targets = _field_targets(template, root)
    _assert_static_artwork_precedes_fields(root, targets)

    assets = {
        "base": {"filename": "base.png", "content_type": "image/png"},
    }
    entries = []
    image_index = 0
    for target in targets:
        entry = {
            "element_id": target["element_id"],
            "field_id": target["field_id"],
            "field_type": target["field_type"],
            "option_value": target["option_value"],
            "inverted": target["inverted"],
            "kind": target["tag"],
        }
        if target["tag"] == "image":
            asset_id, filename, content_type = _write_image_asset(directory, target, image_index)
            image_index += 1
            assets[asset_id] = {"filename": filename, "content_type": content_type}
            entry["asset_id"] = asset_id
        entries.append(entry)

    width, height, layers = _browser_metadata(svg, entries, directory / "base.png")
    manifest = {
        "version": PROTECTED_PREVIEW_VERSION,
        "width": width,
        "height": height,
        "base_asset_id": "base",
        "layers": sorted(layers, key=lambda layer: layer["order"]),
        "assets": assets,
    }
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    temporary.replace(manifest_path)
    return manifest


def public_protected_manifest(template, asset_url_builder) -> dict:
    manifest = build_protected_preview(template)
    return {
        "version": manifest["version"],
        "width": manifest["width"],
        "height": manifest["height"],
        "base_url": asset_url_builder(manifest["base_asset_id"]),
        "layers": [
            {
                **{key: value for key, value in layer.items() if key != "element_id"},
                **(
                    {"asset_url": asset_url_builder(layer["asset_id"])}
                    if layer.get("asset_id")
                    else {}
                ),
            }
            for layer in manifest["layers"]
        ],
    }


def protected_asset(template, asset_id: str):
    if not re.fullmatch(r"[a-z0-9-]{1,40}", asset_id or ""):
        raise ProtectedPreviewError("Invalid protected preview asset.")
    manifest = build_protected_preview(template)
    record = manifest["assets"].get(asset_id)
    if not record:
        raise ProtectedPreviewError("Protected preview asset not found.")
    path = (_preview_directory(template) / record["filename"]).resolve()
    directory = _preview_directory(template).resolve()
    if directory not in path.parents or not path.is_file():
        raise ProtectedPreviewError("Protected preview asset not found.")
    return path, record["content_type"]
