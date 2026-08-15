"""
SVG optimization utilities to reduce storage size and improve loading performance.
"""
import re
from defusedxml import ElementTree as SafeET
from xml.etree import ElementTree as ET
from typing import Optional


def minify_svg(svg_text: str) -> str:
    """
    Minify SVG by removing unnecessary whitespace and comments ONLY.
    
    SAFE minification that preserves:
    - All attributes (including empty ones)
    - All text content and whitespace inside text elements
    - All IDs, classes, data-* attributes
    - All structure and functionality
    
    This function ONLY removes:
    - XML/HTML comments (<!-- ... -->)
    - Extra whitespace BETWEEN tags (not inside content)
    - Leading/trailing whitespace
    
    Args:
        svg_text: Original SVG content as string
        
    Returns:
        Minified SVG string (functionally identical, just smaller)
    """
    if not svg_text:
        return svg_text
    
    try:
        # Parse SVG to validate structure
        root = SafeET.fromstring(svg_text)
        
        # Serialize back - ET.tostring preserves all attributes and content
        # We're NOT modifying the tree, just re-serializing it
        minified = ET.tostring(root, encoding='unicode', method='xml')
        
        # SAFE cleanup: only remove whitespace BETWEEN tags, not inside content
        # Remove comments (safe to remove)
        minified = re.sub(r'<!--.*?-->', '', minified, flags=re.DOTALL)
        
        # Remove extra whitespace/newlines between tags only
        # This regex targets whitespace that's between > and < (between tags)
        minified = re.sub(r'>\s+<', '><', minified)
        
        # Remove leading/trailing whitespace from the entire string
        minified = minified.strip()
        
        return minified
        
    except ET.ParseError:
        # If parsing fails, do VERY SAFE text-based minification
        # Only remove comments and whitespace between tags
        # Remove XML comments
        svg_text = re.sub(r'<!--.*?-->', '', svg_text, flags=re.DOTALL)
        # Remove whitespace between tags ONLY (>...< becomes ><)
        svg_text = re.sub(r'>\s+<', '><', svg_text)
        return svg_text.strip()


def get_svg_size_kb(svg_text: str) -> float:
    """
    Get the size of SVG in kilobytes.
    
    Args:
        svg_text: SVG content as string
        
    Returns:
        Size in KB (float)
    """
    if not svg_text:
        return 0.0
    return len(svg_text.encode('utf-8')) / 1024.0
