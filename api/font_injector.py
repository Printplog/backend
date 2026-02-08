"""
Utility to inject @font-face declarations into SVG content
"""
import base64
import os
import re
import tempfile
import hashlib
from typing import List, Optional, Tuple
from django.conf import settings
from django.core.cache import cache
from .models import Font

# Pre-compile regex patterns for better performance
DEFS_PATTERN = re.compile(r'(<defs[^>]*>)(.*?)(</defs>)', re.IGNORECASE | re.DOTALL)
FONT_FAMILY_CSS_PATTERN = re.compile(r'font-family\s*:\s*([^;,\n]+)', re.IGNORECASE)
STYLE_ATTR_PATTERN = re.compile(r'style\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
FONT_FAMILY_ATTR_PATTERN = re.compile(r'font-family\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
STYLE_BLOCK_PATTERN = re.compile(r'<style[^>]*>(.*?)</style>', re.DOTALL | re.IGNORECASE)
SVG_PATTERN = re.compile(r'(<svg[^>]*>)', re.IGNORECASE)
FONT_FACE_BLOCK_PATTERN = re.compile(r'@font-face\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', re.IGNORECASE | re.DOTALL)
FONT_FAMILY_IN_FONTFACE_PATTERN = re.compile(r'font-family\s*:\s*["\']([^"\']+)["\']', re.IGNORECASE)
URL_SRC_PATTERN = re.compile(r'src\s*:\s*url\s*\(\s*["\']?(https?://[^)"\'\s]+)', re.IGNORECASE)
STYLE_PATTERN_IN_DEFS = re.compile(r'(<style[^>]*>)(<!\[CDATA\[)?(.*?)(\]\]>)?(</style>)', re.IGNORECASE | re.DOTALL)


def _build_font_face(font_family: str, font_url: str, font_format: str, weight: str = "normal", style: str = "normal") -> str:
    return f'''@font-face {{
  font-family: "{font_family}";
  src: url("{font_url}") format("{font_format}");
  font-weight: {weight};
  font-style: {style};
}}'''


def _normalize_font_key(name: Optional[str], weight: str = "normal", style: str = "normal") -> str:
    if not name:
        return ""
    base_key = re.sub(r'[^a-z0-9]', '', name.lower())
    # Create unique key for family + weight + style combination
    return f"{base_key}_{weight}_{style}"


def _extract_font_aliases(svg_content: str) -> dict:
    """
    Extract all font-family names actually used in the SVG.
    Returns a map: normalized_key -> exact_font_family_name_as_used_in_svg
    """
    alias_map = {}
    
    def add_alias(value: str):
        """Add font family to alias map with exact name preservation"""
        # Take first font-family (before comma, which indicates fallbacks)
        first_family = value.split(',')[0].strip().strip('\'"')
        if not first_family:
            return
        
        # Normalize for matching (name only)
        key = re.sub(r'[^a-z0-9]', '', first_family.lower())
        if key:
            # Store the exact name as it appears in SVG (preserves quotes, spacing, case)
            alias_map[key] = first_family
    
    # Extract from <style> blocks (using pre-compiled pattern)
    for style_block in STYLE_BLOCK_PATTERN.findall(svg_content):
        for match in FONT_FAMILY_CSS_PATTERN.findall(style_block):
            add_alias(match)
    
    # Extract from style attributes (inline styles) - using pre-compiled pattern
    for style_attr in STYLE_ATTR_PATTERN.findall(svg_content):
        for match in FONT_FAMILY_CSS_PATTERN.findall(style_attr):
            add_alias(match)
    
    # Extract from font-family XML attributes - using pre-compiled pattern
    for match in FONT_FAMILY_ATTR_PATTERN.findall(svg_content):
        add_alias(match)
    
    return alias_map

 
def _get_font_candidates(font: Font) -> List[str]:
    candidates = []
    if getattr(font, "name", None):
        candidates.append(font.name)
    if font.font_file:
        filename = os.path.basename(font.font_file.name)
        stem, _ = os.path.splitext(filename)
        if stem:
            candidates.append(stem)
    return candidates


def inject_fonts_into_svg(svg_content: str, fonts: List[Font], base_url: Optional[str] = None, embed_base64: bool = False) -> str:
    """
    Inject @font-face declarations into SVG content with caching for performance
    
    Args:
        svg_content: The SVG content as a string
        fonts: List of Font objects to inject
        base_url: Base URL for font files (for frontend use)
        embed_base64: If True, embed fonts as base64 (for backend PDF/PNG generation)
    
    Returns:
        SVG content with @font-face declarations injected
    """
    if not fonts:
        return svg_content
    
    # Create cache key from SVG content hash and font IDs
    # This allows us to cache font-injected SVGs to avoid reprocessing
    svg_hash = hashlib.md5(svg_content.encode('utf-8')).hexdigest()
    font_ids = sorted([str(font.id) for font in fonts])
    font_ids_str = '_'.join(font_ids)
    cache_key = f"svg_fonts_{svg_hash}_{hashlib.md5(font_ids_str.encode('utf-8')).hexdigest()}_{embed_base64}"
    
    # Try to get from cache (cache for 1 hour)
    cached_result = cache.get(cache_key)
    if cached_result:
        return cached_result
    
    # Find or create <defs> section (using pre-compiled pattern)
    defs_match = DEFS_PATTERN.search(svg_content)
    
    alias_map = _extract_font_aliases(svg_content)
    
    # Generate @font-face declarations
    font_faces: List[Tuple[str, str]] = []
    for font in fonts:
        font_family = font.name
        font_format = font.get_font_format()
        font_url: Optional[str] = None
        
        if embed_base64:
            # Embed font as base64 for backend rendering
            try:
                if not font.font_file:
                    continue
                font.font_file.open("rb")
                try:
                    font_data = font.font_file.read()
                finally:
                    font.font_file.close()
                font_base64 = base64.b64encode(font_data).decode('utf-8')
                # Use proper MIME types for different font formats
                mime_type_map = {
                    'truetype': 'application/font-truetype',
                    'opentype': 'application/font-opentype',
                    'woff': 'application/font-woff',
                    'woff2': 'application/font-woff2',
                }
                mime_type = mime_type_map.get(font_format, 'application/font-truetype')
                font_url = f"data:{mime_type};base64,{font_base64}"
            except Exception as e:
                print(f"Error reading font file {font.name}: {e}")
                continue
        else:
            # Use URL for frontend rendering
            if not font.font_file:
                continue
            font_url = font.font_file.url
            if base_url and font_url and not font_url.startswith("http"):
                font_url = f"{base_url}{font_url}"
        
        if not font_url:
            continue
        
        # Use explicit family if present, otherwise font.name
        # Important: Group variants under the same family name
        effective_family = font.family if font.family else font.name
        
        # If we have a clean family name, use it. 
        # Otherwise fallback to matching logic which might grab the full name "Roboto Bold" as family
        if font.family:
             css_family = font.family
        else:
            # Try to find what SVG uses
            candidates = _get_font_candidates(font)
            # ... existing matching logic ...
            # For back-compat with existing behavior if family not set
            for candidate in candidates:
                key = re.sub(r'[^a-z0-9]', '', candidate.lower()) # simple key for matching name only
                if key and key in alias_map:
                    css_family = alias_map[key]
                    break
            
            if not css_family:
                # If no family set and no match, default to name
                 css_family = font.name

        weight = getattr(font, 'weight', 'normal')
        style = getattr(font, 'style', 'normal')
        
        # Generate unique key for this specific variant
        variant_key = _normalize_font_key(css_family, weight, style)
        
        font_faces.append((variant_key, css_family, _build_font_face(css_family, font_url, font_format, weight, style)))
    
    # Deduplicate font-faces by unique key (family + weight + style)
    # Map: normalized_variant_key -> (css_family, font_face_css)
    unique_font_map = {}
    for variant_key, css_family, font_face in font_faces:
        if variant_key not in unique_font_map:
            unique_font_map[variant_key] = (css_family, font_face)
    
    if not unique_font_map:
        return svg_content
    
    # Extract unique font-face CSS strings
    unique_font_faces = [font_face for _, font_face in unique_font_map.values()]
    
    # Combine all font-face declarations
    font_faces_css = '\n'.join(unique_font_faces)
    
    # Create style block with font-face declarations
    style_block = f'<style type="text/css"><![CDATA[\n{font_faces_css}\n]]></style>'
    
    if defs_match:
        defs_start, defs_content, defs_end = defs_match.groups()
        defs_full = defs_match.group(0)
        
        # Use pre-compiled pattern for style matching
        style_match = STYLE_PATTERN_IN_DEFS.search(defs_content)
        
        if style_match:
            style_full = style_match.group(0)
            style_open, cdata_open, existing_style, cdata_close, style_close = style_match.groups()
            
            # Extract existing font-families from @font-face declarations ONLY (not from regular CSS)
            # This prevents skipping fonts that are used in CSS but don't have @font-face yet
            existing_families = set()
            url_based_families = set()  # Track which fonts use URLs (need replacement when embed_base64=True)
            
            # Match @font-face blocks first, then extract font-family from within them
            # Use pre-compiled patterns for better performance
            for font_face_block in FONT_FACE_BLOCK_PATTERN.findall(existing_style):
                for match in FONT_FAMILY_IN_FONTFACE_PATTERN.findall(font_face_block):
                    family_key = _normalize_font_key(match)
                    existing_families.add(family_key)
                    
                    # Check if this @font-face uses a URL (not base64)
                    if URL_SRC_PATTERN.search(font_face_block):
                        url_based_families.add(family_key)
            
            # When embed_base64=True, replace URL-based @font-face declarations
            # Otherwise, only add font-faces that don't already exist
            if embed_base64 and url_based_families:
                # Remove all @font-face blocks that use URLs and need to be replaced
                modified_style = existing_style
                for font_face_block in FONT_FACE_BLOCK_PATTERN.findall(existing_style):
                    family_matches = FONT_FAMILY_IN_FONTFACE_PATTERN.findall(font_face_block)
                    if family_matches:
                        family_key = _normalize_font_key(family_matches[0])
                        if family_key in url_based_families and family_key in unique_font_map:
                            # This URL-based font will be replaced with base64
                            modified_style = modified_style.replace(font_face_block, '', 1)
                
                # Now add all our fonts (new ones + replacements for removed URL-based ones)
                missing_font_faces = []
                for variant_key, (css_family, font_face) in unique_font_map.items():
                    # If using base64, we just append all our unique fonts. 
                    # We might duplicate if same font is already there in base64, but better than missing it.
                    missing_font_faces.append(font_face)
                
                if missing_font_faces:
                    new_style_content = modified_style + '\n' + '\n'.join(missing_font_faces)
                else:
                    new_style_content = modified_style
            else:
                # Original behavior: only add font-faces that don't already exist
                missing_font_faces = []
                for variant_key, (css_family, font_face) in unique_font_map.items():
                    # Always inject our fonts, trusting the unique_font_map to keep them unique among themselves.
                    # We skip the complex 'existing_families' check to avoid false negatives on variants.
                    missing_font_faces.append(font_face)
                
                new_style_content = existing_style + ('\n' + '\n'.join(missing_font_faces) if missing_font_faces else '')
            
            # Update the style block if content changed
            if new_style_content != existing_style:
                cdata_open = cdata_open or ''
                cdata_close = cdata_close or ''
                new_style_block = f'{style_open}{cdata_open}{new_style_content}{cdata_close}{style_close}'
                new_defs_content = defs_content.replace(style_full, new_style_block, 1)
                new_defs_full = defs_full.replace(defs_content, new_defs_content, 1)
                svg_content = svg_content.replace(defs_full, new_defs_full, 1)
        else:
            # No style block yet, prepend a new one while preserving defs wrapper
            new_defs_content = style_block + '\n' + defs_content
            new_defs_full = defs_full.replace(defs_content, new_defs_content, 1)
            svg_content = svg_content.replace(defs_full, new_defs_full, 1)
    else:
        # Create new <defs> section
        # Find the opening <svg> tag (using pre-compiled pattern)
        svg_match = SVG_PATTERN.search(svg_content)
        if svg_match:
            svg_content = svg_content.replace(svg_match.group(0), svg_match.group(0) + f'\n<defs>\n{style_block}\n</defs>')
    
    # Cache the result for 1 hour (3600 seconds)
    # Only cache if SVG is reasonably sized (< 10MB) to avoid memory issues
    if len(svg_content) < 10 * 1024 * 1024:  # 10MB limit
        cache.set(cache_key, svg_content, 3600)
    
    return svg_content

