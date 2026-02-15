import re

pattern = r'(<image[^>]*?\s(?:xlink:href|href)=["\'])(data:(?:image|img)\/[^;]+;base64,[^"\']+)(["\'][^>]*?>)'
text = '  <image id="Background.select_1" width="2048" height="1063" xlink:href="data:img/png;base64,iVBORw0KGgoAAAANSUhEUgAACAAAAAQnCAYAAABvrozKAAAgAElEQVR4nDy815Ok6ZXe90vvfWZlZnnvurraTo/rQQ9mBoMZOC4BxO6SK664uhGpOzH0RygUIYZCN1RIpFbBNcQSELGLBbAzGIy37X2X95WV3nuvOKdX7Jvuru7K+r73Pe85z3me57yG/+1/+tMTh9PjnZ9fJhLwcuvuU/7yr37G7sExU6MjdBoNLl+5zAuvXqBa7nHt2mV8fgfHRyccHCYYGRtjYIBsKsfRUYLfvv8xAZeXUNCFxeZkc3efc7OjpPNZOq06//pf/XfUqmXsDgepVAqr1Yrb7Sabzervs7OztFptGHbpdPqcJQvMTE+SzqcIj45x/8EOjx48plWt4XX76PTbrCxMMzMzxsr" />'

match = re.search(pattern, text, flags=re.IGNORECASE)
if match:
    print("Match found!")
    print(f"Group 1: {match.group(1)}")
    print(f"Group 2: {match.group(2)[:50]}...")
    print(f"Group 3: {match.group(3)}")
else:
    print("No match found.")
