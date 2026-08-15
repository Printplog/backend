from decimal import Decimal
from io import BytesIO
import os
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from PIL import Image

from api.cache_utils import invalidate_template_cache
from api.models import Template, Tool
from api.rendering import sanitize_svg_for_render, svg_dimensions
from api.svg_parser import parse_svg_to_form_fields


DEMO_TEMPLATES = (
    {
        "name": "Boarding Pass1_Fixed",
        "file": "Boarding Pass1_Fixed.svg",
        "minimum_fields": 20,
        "keywords": ["boarding pass", "flight", "travel", "api"],
    },
    {
        "name": "Boarding Pass 5",
        "file": "Boarding pass5.svg",
        "minimum_fields": 20,
        "keywords": ["boarding pass", "airline", "travel", "api"],
    },
    {
        "name": "Three Way Flight Itinerary",
        "file": "Flight Itinerary 3 way Ticket3.svg",
        "minimum_fields": 25,
        "keywords": ["flight itinerary", "multi city", "travel", "api"],
    },
    {
        "name": "Return Flight Itinerary",
        "file": "Flight Itinerary Return One-way Ticket.svg",
        "minimum_fields": 18,
        "keywords": ["flight itinerary", "return flight", "travel", "api"],
    },
)

# Kept for callers that import the original primary demo constants.
DEMO_TEMPLATE_NAME = DEMO_TEMPLATES[0]["name"]
DEMO_TEMPLATE_FILE = DEMO_TEMPLATES[0]["file"]
LEGACY_GENERATED_DEMOS = (
    "Demo Event Pass",
    "Demo Course Certificate",
    "Demo Service Receipt",
)


def raster_banner(svg: str) -> bytes:
    from playwright.sync_api import sync_playwright

    safe_svg = sanitize_svg_for_render(svg)
    width, height = svg_dimensions(safe_svg)
    scale = min(1200 / width, 900 / height, 1)
    target_width = max(1, round(width * scale))
    target_height = max(1, round(height * scale))
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "html,body{margin:0;padding:0;background:#fff;overflow:hidden}"
        f"svg{{display:block;width:{target_width}px;height:{target_height}px}}"
        "</style></head><body>" + safe_svg + "</body></html>"
    )
    launch_options = {"headless": True, "args": ["--disable-dev-shm-usage"]}
    executable_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or None
    if executable_path:
        launch_options["executable_path"] = executable_path
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(
                viewport={"width": target_width, "height": target_height},
                java_script_enabled=False,
                service_workers="block",
            )
            page = context.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(html, wait_until="load", timeout=20_000)
            rendered = page.locator("svg").screenshot(
                type="png",
                animations="disabled",
                scale="css",
                timeout=60_000,
            )
        finally:
            browser.close()
    image = Image.open(BytesIO(rendered)).convert("RGB")
    output = BytesIO()
    image.save(output, format="WEBP", quality=84, method=6)
    return output.getvalue()


class Command(BaseCommand):
    help = "Seed the hosted API demo from real SharpToolz travel template assets."

    def handle(self, *args, **options):
        tool, _ = Tool.objects.update_or_create(
            name="Travel Documents",
            defaults={
                "description": "Real boarding pass and itinerary layouts available through SharpToolz.",
                "price": Decimal("5.00"),
                "is_active": True,
            },
        )

        # These exact rows were created by an earlier version of this command.
        # Remove them so the customer demo proves the real SharpToolz asset.
        Template.objects.filter(name__in=LEGACY_GENERATED_DEMOS).delete()

        template_root = Path(settings.BASE_DIR).parent / "templates"
        for specification in DEMO_TEMPLATES:
            asset_path = template_root / specification["file"]
            if not asset_path.is_file():
                raise CommandError(f"Missing real SharpToolz template asset: {asset_path}")
            svg = asset_path.read_text(encoding="utf-8")
            fields = parse_svg_to_form_fields(svg)
            minimum_fields = specification["minimum_fields"]
            if len(fields) < minimum_fields:
                raise CommandError(
                    f"{asset_path.name} produced only {len(fields)} fields; expected at least {minimum_fields}."
                )

            template = Template.objects.filter(name=specification["name"]).first()
            if template is None:
                template = Template(name=specification["name"])
            template.type = "tool"
            template.tool = tool
            template.is_active = True
            template.hot = True
            template.keywords = specification["keywords"]
            template._raw_svg_data = svg
            template.save()

            # Template listings must never fetch the editable SVG. Store a flattened
            # raster thumbnail even when an old demo row reused svg_file as banner.
            banner_path = f"templates/banners/api-demo-{template.id}.webp"
            if default_storage.exists(banner_path):
                default_storage.delete(banner_path)
            default_storage.save(banner_path, ContentFile(raster_banner(svg)))
            Template.objects.filter(pk=template.pk).update(banner=banner_path)
            template.banner.name = banner_path
            self.stdout.write(self.style.SUCCESS(
                f"Seeded real template {template.name} ({template.id}) from {asset_path} - {len(fields)} fields"
            ))

        invalidate_template_cache()
