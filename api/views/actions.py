import logging
import traceback
import io
import re
import base64
import requests as req
from PIL import Image

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.conf import settings
from django.http import HttpResponse

import cairosvg

from ..models import PurchasedTemplate
from ..serializers import FieldUpdateSerializer
from ..svg_updater import update_svg_from_field_updates
from ..font_injector import inject_fonts_into_svg
from ..watermark import WaterMark
from wallet.views import send_wallet_update

logger = logging.getLogger(__name__)

# Try to import Playwright renderer
try:
    from ..playwright_renderer import render_svg_with_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    render_svg_with_playwright = None

# Try to import WeasyPrint renderer
try:
    from ..weasyprint_renderer import render_svg_with_weasyprint
    WEASYPRINT_AVAILABLE = True
except ImportError:
    WEASYPRINT_AVAILABLE = False
    render_svg_with_weasyprint = None


class DownloadDoc(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        print("=" * 60)
        print("=== DownloadDoc POST request started ===")
        print(f"User: {request.user.username if request.user.is_authenticated else 'Anonymous'}")
        
        output_type = request.data.get("type", "pdf").lower()
        purchased_template_id = request.data.get("purchased_template_id")
        template_name = request.data.get("template_name", "")
        side = request.data.get("side", "front")  # "front" or "back" for split downloads
        
        print(f"Output type: {output_type}")
        print(f"Purchased template ID: {purchased_template_id}")
        print(f"Template name: {template_name}")
        print(f"Side: {side}")

        if not purchased_template_id:
            print("ERROR: purchased_template_id is required")
            return Response({"error": "purchased_template_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            purchased_template = PurchasedTemplate.objects.select_related('template').prefetch_related(
                'template__fonts'
            ).only(
                'svg', 'svg_file', 'test', 'name', 'keywords', 
                'template__id', 'template__keywords'
            ).get(id=purchased_template_id, buyer=request.user)
            
            if purchased_template.svg_file:
                with purchased_template.svg_file.open('rb') as f:
                    svg_content = f.read().decode('utf-8')
            else:
                svg_content = purchased_template.svg
            if not template_name:
                template_name = purchased_template.name or ""
        except PurchasedTemplate.DoesNotExist:
            print("ERROR: Purchased template not found")
            return Response({"error": "Purchased template not found"}, status=status.HTTP_404_NOT_FOUND)

        if not svg_content or "</svg>" not in svg_content:
            print("ERROR: Invalid or missing SVG content")
            return Response({"error": "Invalid or missing SVG content"}, status=status.HTTP_400_BAD_REQUEST)

        if output_type not in ("pdf", "png"):
            print(f"ERROR: Unsupported output type: {output_type}")
            return Response({"error": "Unsupported type. Only 'pdf' and 'png' are allowed."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            print("Starting download processing...")
            safe_name = re.sub(r'[^\w\s-]', '', template_name).strip() if template_name else ""
            safe_name = re.sub(r'[-\s]+', '-', safe_name) if safe_name else ""
            
            split_direction = None
            if purchased_template:
                    keywords_to_check = []
                    if purchased_template.keywords:
                        keywords_to_check.extend(purchased_template.keywords)
                    if purchased_template.template and purchased_template.template.keywords:
                        keywords_to_check.extend(purchased_template.template.keywords)
                    
                    keywords_to_check = [str(k).lower().strip() for k in keywords_to_check if k]
                    
                    if "horizontal-split-download" in keywords_to_check:
                        split_direction = "horizontal"
                    elif "vertical-split-download" in keywords_to_check:
                        split_direction = "vertical"
                    elif "split-download" in keywords_to_check:
                        split_direction = "horizontal"
                    
                    if not safe_name and purchased_template.name:
                        safe_name = re.sub(r'[^\w\s-]', '', purchased_template.name).strip()
                        safe_name = re.sub(r'[-\s]+', '-', safe_name) if safe_name else ""
            
            print("Checking for fonts to inject...")
            fonts_to_inject = []
            if purchased_template and purchased_template.template:
                fonts_to_inject = list(purchased_template.template.fonts.all())
                print(f"Found {len(fonts_to_inject)} font(s) to inject")
            
            has_fonts = bool(fonts_to_inject)
            print(f"Has fonts: {has_fonts}")
            
            if has_fonts:
                print("Injecting fonts into SVG...")
                svg_with_fonts = inject_fonts_into_svg(svg_content, fonts_to_inject, embed_base64=True)
                
                if output_type == "pdf":
                    if PLAYWRIGHT_AVAILABLE:
                        output = render_svg_with_playwright(svg_with_fonts, "pdf")
                    else:
                        output = cairosvg.svg2pdf(bytestring=svg_content.encode("utf-8"))
                else:  # PNG
                    if PLAYWRIGHT_AVAILABLE:
                        output = render_svg_with_playwright(svg_with_fonts, "png")
                    else:
                        output = cairosvg.svg2png(bytestring=svg_content.encode("utf-8"))
            else:
                if output_type == "pdf":
                    output = cairosvg.svg2pdf(bytestring=svg_content.encode("utf-8"))
                else:  # PNG
                    output = cairosvg.svg2png(bytestring=svg_content.encode("utf-8"))
            
            if output_type == "pdf":
                content_type = "application/pdf"
                filename = f"{safe_name}.pdf" if safe_name else "output.pdf"
            else:  # PNG
                content_type = "image/png"
                filename = f"{safe_name}.png" if safe_name else "output.png"

            user = request.user
            user.downloads += 1
            user.save()
            
            if split_direction:
                return self._handle_split_download(output, output_type, user, safe_name, split_direction, side)
            
            response = HttpResponse(output, content_type=content_type)
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response

        except Exception as e:
            error_traceback = traceback.format_exc()
            return Response({"error": str(e), "traceback": error_traceback}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def _handle_split_download(self, output, output_type, user, safe_name="", split_direction="horizontal", side="front"):
        try:
            if output_type == "png":
                image = Image.open(io.BytesIO(output))
                width, height = image.size
                
                if split_direction == "vertical":
                    left_half = image.crop((0, 0, width // 2, height))
                    right_half = image.crop((width // 2, 0, width, height))
                    selected_half = left_half if side == "front" else right_half
                else:
                    top_half = image.crop((0, 0, width, height // 2))
                    bottom_half = image.crop((0, height // 2, width, height))
                    selected_half = top_half if side == "front" else bottom_half
                
                half_buffer = io.BytesIO()
                selected_half.save(half_buffer, format='PNG')
                half_bytes = half_buffer.getvalue()
                
                filename = f"{safe_name}_{side}.png" if safe_name else f"document_{side}.png"
                response = HttpResponse(half_bytes, content_type='image/png')
                response["Content-Disposition"] = f'attachment; filename="{filename}"'
                return response
                
            else:  # PDF
                try:
                    from pdf2image import convert_from_bytes
                except ImportError:
                    return Response({"error": "PDF splitting requires pdf2image."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
                images = convert_from_bytes(output)
                if not images:
                    return Response({"error": "Failed to convert PDF to image"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
                image = images[0]
                width, height = image.size
                
                if split_direction == "vertical":
                    left_half = image.crop((0, 0, width // 2, height))
                    right_half = image.crop((width // 2, 0, width, height))
                    selected_half = left_half if side == "front" else right_half
                else:
                    top_half = image.crop((0, 0, width, height // 2))
                    bottom_half = image.crop((0, height // 2, width, height))
                    selected_half = top_half if side == "front" else bottom_half
                
                half_buffer = io.BytesIO()
                selected_half.save(half_buffer, format='PNG')
                half_bytes = half_buffer.getvalue()
                
                filename = f"{safe_name}_{side}.png" if safe_name else f"document_{side}.png"
                response = HttpResponse(half_bytes, content_type='image/png')
                response["Content-Disposition"] = f'attachment; filename="{filename}"'
                return response
                
        except Exception as e:
            error_traceback = traceback.format_exc()
            return Response({"error": f"Failed to split document: {str(e)}", "traceback": error_traceback}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RemoveBackgroundView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        try:
            uploaded_file = request.FILES.get('image')
            if not uploaded_file:
                return Response({"error": "No image file provided"}, status=status.HTTP_400_BAD_REQUEST)
            
            if uploaded_file.size > 10 * 1024 * 1024:  # 10MB limit
                return Response({"error": "File too large (max 10MB)"}, status=status.HTTP_400_BAD_REQUEST)
            
            user = request.user
            charge_amount = 0.20
            
            if not hasattr(user, "wallet") or user.wallet.balance < charge_amount:
                return Response({"error": "Insufficient wallet balance."}, status=status.HTTP_400_BAD_REQUEST)
            
            api_key = settings.REMOVEBG_API_KEY
            if not api_key:
                return Response({"error": "Remove.bg API key not configured."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            image_data = uploaded_file.read()
            response = req.post(
                'https://api.remove.bg/v1.0/removebg',
                files={'image_file': image_data},
                data={'size': 'auto'},
                headers={'X-Api-Key': api_key},
                timeout=30
            )
            
            if response.status_code == 200:
                user.wallet.debit(charge_amount, description="Background removal (Remove.bg)")
                send_wallet_update(user, False)
                result_base64 = base64.b64encode(response.content).decode('utf-8')
                return Response({
                    "success": True,
                    "image": f"data:image/png;base64,{result_base64}",
                    "message": "Background removed successfully"
                })
            else:
                error_msg = response.json().get('errors', [{}])[0].get('title', 'Unknown error')
                raise Exception(f"Remove.bg API error: {error_msg}")
            
        except Exception as e:
            return Response({"error": f"Background removal failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
