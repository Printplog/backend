import re
import hashlib
from django.core.cache import cache

# Pre-compile regex patterns for better performance
VIEWBOX_PATTERN = re.compile(r'viewBox=["\']([^"\']+)["\']')
WIDTH_PATTERN = re.compile(r'width=["\']([^"\'px]+)')
HEIGHT_PATTERN = re.compile(r'height=["\']([^"\'px]+)')
WATERMARK_PATTERN = re.compile(
    r'<g\s+transform="rotate\([^)]+\)"[^>]*>\s*'
    r'<text\s+[^>]*pointer-events="none"[^>]*>'
    r'(?:TEST DOCUMENT|FAKE DOCUMENT)</text>\s*</g>',
    re.IGNORECASE | re.DOTALL
)

class WaterMark():
    def add_watermark(self, svg_content):
        """Add simple random watermarks to SVG with caching for performance"""
        if not svg_content or '</svg>' not in svg_content:
            return svg_content
        
        # Create cache key from SVG content hash
        # This allows us to cache watermarked SVGs to avoid reprocessing
        svg_hash = hashlib.md5(svg_content.encode('utf-8')).hexdigest()
        cache_key = f"svg_watermark_{svg_hash}"
        
        # Try to get from cache (cache for 24 hours since watermarks are deterministic)
        cached_result = cache.get(cache_key)
        if cached_result:
            return cached_result
        
        # Get SVG dimensions
        width, height = self.get_svg_size(svg_content)
        
        # Calculate number of watermarks based on SVG dimensions (width and height)
        # Use pixel-based calculation to ensure multiple rows even for small images
        area = width * height
        
        # Calculate watermark density based on SVG size
        # For small SVGs: aim for watermarks every 80-120 pixels
        # For medium SVGs: aim for watermarks every 120-180 pixels  
        # For large SVGs: aim for watermarks every 180-250 pixels
        
        # Base calculation on both width and height - many more watermarks
        # Use much tighter spacing to fit many more rows and columns
        if width < 200 or height < 200:  # Very small SVGs (ID cards, small docs)
            # Many more: calculate based on dimensions
            cols_target = max(10, int(width / 20))  # More rows: 1 per 20px width
            rows_target = max(10, int(height / 20))  # More rows: 1 per 20px height
            watermark_count = cols_target * rows_target
        elif width < 400 or height < 400:  # Small SVGs
            cols_target = max(15, int(width / 25))  # More rows: 1 per 25px
            rows_target = max(15, int(height / 25))  # More rows: 1 per 25px
            watermark_count = cols_target * rows_target
        elif width < 700 or height < 700:  # Medium SVGs
            cols_target = max(22, int(width / 30))  # More rows: 1 per 30px
            rows_target = max(22, int(height / 30))  # More rows: 1 per 30px
            watermark_count = cols_target * rows_target
        elif width < 1000 or height < 1000:  # Large SVGs (A4 size ~800x1100)
            cols_target = max(30, int(width / 35))  # More rows: 1 per 35px
            rows_target = max(30, int(height / 35))  # More rows: 1 per 35px
            watermark_count = cols_target * rows_target
        else:  # Very large SVGs
            cols_target = max(40, int(width / 40))  # More rows: 1 per 40px
            rows_target = max(40, int(height / 40))  # More rows: 1 per 40px
            watermark_count = cols_target * rows_target
        
        # Ensure minimum watermark count based on area as fallback
        area_based_count = max(80, int(area / 600))  # Many more: 1 per 600 area units
        watermark_count = max(watermark_count, area_based_count)
        
        # Cap maximum watermarks
        watermark_count = min(watermark_count, 1500)  # Maximum 1500 watermarks
        
        # Calculate appropriate font size based on SVG dimensions
        # Scale font size to be proportional to SVG size
        avg_dimension = (width + height) / 2
        font_size = max(12, min(60, int(avg_dimension / 15)))  # Font size between 12-60px
        
        # Estimate text width: "FAKE DOCUMENT" is ~13 characters
        # Approximate width: font_size * 0.65 * character_count
        text_width = font_size * 0.65 * 13  # Approximately 8.45 * font_size
        text_height = font_size * 1.2  # Approximate text height (with line height)
        
        # Diagonal angle in degrees (negative for top-left to bottom-right)
        angle_degrees = -45
        angle_radians = abs(angle_degrees) * 3.14159265359 / 180  # Convert to radians
        
        # Step 1: Calculate the bounding box of rotated text to prevent overlap
        # When text is rotated, we need to calculate the space it occupies
        # For a rectangle rotated by angle θ:
        # bounding_width = width * |cos(θ)| + height * |sin(θ)|
        # bounding_height = width * |sin(θ)| + height * |cos(θ)|
        cos_angle = abs(0.70710678118)  # cos(45°) = √2/2
        sin_angle = abs(0.70710678118)  # sin(45°) = √2/2
        
        # Calculate bounding box dimensions of rotated text
        watermark_bbox_width = (text_width * cos_angle) + (text_height * sin_angle)
        watermark_bbox_height = (text_width * sin_angle) + (text_height * cos_angle)
        
        # Calculate spacing based on SVG size - adaptive spacing
        # Use much tighter spacing to allow 2x more rows and columns
        avg_dimension = (width + height) / 2
        
        # Adaptive padding factor based on SVG size - reduced for 2x more watermarks
        if avg_dimension < 200:  # Very small SVGs
            padding_factor = 1.02  # Very tight: 2% spacing (reduced from 5%)
            pixel_buffer = max(2, font_size * 0.05)  # Smaller buffer
        elif avg_dimension < 400:  # Small SVGs
            padding_factor = 1.03  # Very tight: 3% spacing (reduced from 8%)
            pixel_buffer = max(2, font_size * 0.06)  # Smaller buffer
        elif avg_dimension < 700:  # Medium SVGs
            padding_factor = 1.04  # Tight: 4% spacing (reduced from 10%)
            pixel_buffer = max(3, font_size * 0.07)  # Smaller buffer
        elif avg_dimension < 1000:  # Large SVGs
            padding_factor = 1.05  # Tight: 5% spacing (reduced from 12%)
            pixel_buffer = max(3, font_size * 0.08)  # Smaller buffer
        else:  # Very large SVGs
            padding_factor = 1.06  # Moderate: 6% spacing (reduced from 15%)
            pixel_buffer = max(4, font_size * 0.09)  # Smaller buffer
        
        # Calculate minimum spacing based on size-adaptive factors
        min_spacing_x = watermark_bbox_width * padding_factor
        min_spacing_y = watermark_bbox_height * padding_factor
        
        # Ensure minimum safe spacing with pixel buffer (but keep it minimal)
        min_spacing_x = max(min_spacing_x, watermark_bbox_width + pixel_buffer)
        min_spacing_y = max(min_spacing_y, watermark_bbox_height + pixel_buffer)
        
        # Step 2: Calculate available space for watermarks
        # Smaller left margin to start closer to left border
        left_margin_percent = 0.01  # 1% margin on left (very close to border)
        right_margin_percent = 0.05  # 5% margin on right
        top_margin_percent = 0.05  # 5% margin on top
        bottom_margin_percent = 0.05  # 5% margin on bottom
        
        available_width = width * (1 - left_margin_percent - right_margin_percent)
        available_height = height * (1 - top_margin_percent - bottom_margin_percent)
        
        # Step 3: Calculate watermarks using square area approach
        # One watermark per square area - simpler and more predictable
        # Use a square area size of 320x320 pixels per watermark
        square_area_size = 320  # pixels - size of each square area
        
        # Calculate how many squares fit horizontally and vertically
        squares_horizontal = max(1, int(available_width / square_area_size))
        squares_vertical = max(1, int(available_height / square_area_size))
        
        # Calculate spacing between square centers
        spacing_x = available_width / squares_horizontal if squares_horizontal > 0 else 0
        spacing_y = available_height / squares_vertical if squares_vertical > 0 else 0
        
        # Calculate total number of watermarks
        actual_watermark_count = squares_horizontal * squares_vertical
        
        # Step 4: Generate watermarks at the center of each square area
        watermarks = []
        watermark_index = 0
        
        # Start position (center of first square)
        start_x = (width - available_width) / 2 + (spacing_x / 2)
        start_y = (height - available_height) / 2 + (spacing_y / 2)
        
        for row in range(squares_vertical):
            for col in range(squares_horizontal):
                if watermark_index >= actual_watermark_count:
                    break
                
                # Calculate position at the center of this square area
                x = start_x + (col * spacing_x)
                y = start_y + (row * spacing_y)
                
                # Apply diagonal offset for slanted pattern
                if squares_horizontal > 1 and squares_vertical > 1:
                    diagonal_shift = spacing_x * 0.25  # 25% shift for diagonal effect
                    x = x + (diagonal_shift * row / max(1, squares_vertical - 1))
                
                # Ensure watermarks stay within bounds
                margin_x = watermark_bbox_width / 2
                margin_y = watermark_bbox_height / 2
                
                if x >= margin_x and x <= width - margin_x and y >= margin_y and y <= height - margin_y:
                    watermark = (
                        f'<g transform="rotate({angle_degrees}, {x}, {y})" pointer-events="none">'
                        f'<text x="{x}" y="{y}" fill="black" font-size="{font_size}" font-weight="900" font-family="Arial, sans-serif" text-anchor="middle" pointer-events="none">'
                        f'FAKE DOCUMENT</text></g>'
                    )
                    watermarks.append(watermark)
                    watermark_index += 1
            
            if watermark_index >= actual_watermark_count:
                break
        
        # Optimize watermark insertion for large SVGs
        # Use string building instead of replace() for better performance
        if not watermarks:
            return svg_content
        
        watermark_text = '\n'.join(watermarks)
        # Find the position of </svg> tag
        svg_end_pos = svg_content.rfind('</svg>')
        if svg_end_pos == -1:
            return svg_content
        
        # Build new SVG string efficiently
        result = svg_content[:svg_end_pos] + f'\n{watermark_text}\n' + svg_content[svg_end_pos:]
        
        # Cache the result for 24 hours (86400 seconds)
        # Only cache if SVG is reasonably sized (< 10MB) to avoid memory issues
        if len(svg_content) < 10 * 1024 * 1024:  # 10MB limit
            cache.set(cache_key, result, 86400)
        
        return result

    def remove_watermark(self, svg_content):
        """
        Remove all watermark elements added by add_watermark.
        Specifically removes <g> elements containing <text>TEST DOCUMENT</text> with the expected attributes.
        """
        if not svg_content or '</svg>' not in svg_content:
            return svg_content

        # Use pre-compiled regex pattern for better performance
        cleaned_svg = WATERMARK_PATTERN.sub('', svg_content)
        return cleaned_svg

    def get_svg_size(self, svg_content):
        """Get SVG width and height"""
        # Default size 
        width, height = 400, 300

        # Try viewBox first (using pre-compiled pattern)
        viewbox = VIEWBOX_PATTERN.search(svg_content)
        if viewbox:
            values = viewbox.group(1).split()
            if len(values) >= 4:
                width = float(values[2])
                height = float(values[3])
                return width, height
        
        # Try width/height attributes (using pre-compiled patterns)
        width_match = WIDTH_PATTERN.search(svg_content)
        height_match = HEIGHT_PATTERN.search(svg_content)
        
        if width_match:
            width = float(width_match.group(1))
        if height_match:
            height = float(height_match.group(1))
        
        return width, height

