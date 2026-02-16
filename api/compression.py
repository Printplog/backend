import io
import os
import re
import base64
from PIL import Image

def compress_image_data(base64_data, quality=60):
    """
    Compresses base64 image data and returns a new base64 string.
    Optimizes PNGs instead of converting to JPEG to preserve transparency and SVG compatibility.
    """
    try:
        # Decode base64
        if ',' in base64_data:
            header, data = base64_data.split(',', 1)
        else:
            header, data = "data:image/png;base64", base64_data
            
        image_bytes = base64.b64decode(data)
        img = Image.open(io.BytesIO(image_bytes))
        
        output = io.BytesIO()
        
        # Determine format (default to PNG for safety if unknown or RGBA)
        fmt = img.format if img.format else 'PNG'
        
        # If it's a huge image, resize it safely (max 1800px width)
        if img.width > 1800:
            ratio = 1800 / img.width
            new_height = int(img.height * ratio)
            img = img.resize((1800, new_height), Image.Resampling.LANCZOS)

        if fmt == 'JPEG':
            img.save(output, format='JPEG', quality=quality, optimize=True)
            mime_type = "image/jpeg"
        else:
            # Safer Optimization:
            # 1. Resize (done above)
            # 2. Quantize to 256 colors (significant size reduction, keeps transparency)
            # This is "lossy" for colors but maintains PNG structure and transparency.
            if img.mode != 'P':
                img = img.quantize(colors=256, method=2)
            
            img.save(output, format='PNG', optimize=True)
            mime_type = "image/png"
            
        compressed_data = base64.b64encode(output.getvalue()).decode('utf-8')
        
        return f"data:{mime_type};base64,{compressed_data}"
    except Exception as e:
        print(f"Error compressing embedded image: {e}")
        return base64_data

def compress_svg_images(svg_text, quality=60):
    """
    Finds all <image> tags with base64 data and compresses them.
    """
    def replacement(match):
        prefix = match.group(1)
        base64_data = match.group(2)
        suffix = match.group(3)
        
        # Only compress if it looks like a large base64 string
        if len(base64_data) > 1000:
            compressed = compress_image_data(base64_data, quality)
            return f'{prefix}{compressed}{suffix}'
        return match.group(0)

    # Match <image ... xlink:href="data:image/..." or data:img/... ... /> or href="..."
    pattern = r'(<image[^>]*?\s(?:xlink:href|href)=["\'])(data:(?:image|img)\/[^;]+;base64,[^"\']+)(["\'][^>]*?>)'
    return re.sub(pattern, replacement, svg_text, flags=re.IGNORECASE)

def compress_image(image_field, quality=60, max_width=1200):
    """
    Compresses an ImageField files in-place.
    Now more aggressive: converts PNG to JPEG to save space.
    """
    if not image_field:
        return

    try:
        from django.core.files.base import ContentFile
        img = Image.open(image_field)
        
        # 1. Resize if too large (1200px is plenty for thumbnails/banners)
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
            
        # 2. Prepare for saving
        # Convert to RGB if we want to save as JPEG (removes transparency)
        if img.mode in ("RGBA", "P"):
            # Check if it actually has transparency
            if img.mode == "RGBA":
                # Create a white background
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3]) # 3 is the alpha channel
                img = background
            else:
                img = img.convert("RGB")

        output = io.BytesIO()
        
        # 3. Save as JPEG (much smaller than PNG for these docs)
        img.save(output, format='JPEG', quality=quality, optimize=True)
            
        # 4. Replace the file content in memory
        # Note: We keep the original filename but the content is now JPEG
        # Django/Browsers handle this okay usually, but ideally we'd change extension.
        # However, to avoid DB migration issues, we'll stick to the content change.
        new_content = ContentFile(output.getvalue())
        image_field.save(image_field.name, new_content, save=False)
        
    except Exception as e:
        print(f"Error compressing image {getattr(image_field, 'name', 'unknown')}: {e}")
