import sys
import os

# Add parent dir to path
sys.path.append(os.getcwd())

from api.compression import compress_svg_images

svg_path = "test_original.svg"
if not os.path.exists(svg_path):
    print(f"File {svg_path} not found.")
    sys.exit(1)

with open(svg_path, 'r', encoding='utf-8', errors='ignore') as f:
    text = f.read()

print(f"Original size: {len(text)} bytes")

# Test with quality=40 for aggressive PNG quantization (if implemented) or just optimization
optimized = compress_svg_images(text, quality=40)

print(f"Optimized size: {len(optimized)} bytes")

if len(optimized) < len(text):
    with open("test_safe_optimized.svg", "w", encoding='utf-8') as f:
        f.write(optimized)
    print("Optimization successful! Saved to test_safe_optimized.svg")
else:
    print("Optimization did not reduce size (or increased it due to PNG overhead).")
