import os
import sys
import django

sys.path.append('/var/www/backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'serverConfig.settings')
django.setup()

from api.models import Template, PurchasedTemplate, Font

def check_files(model, field_name):
    print(f"\nChecking {model.__name__}.{field_name}...")
    missing = 0
    total = 0
    for obj in model.objects.all():
        file_field = getattr(obj, field_name)
        if file_field:
            total += 1
            if not os.path.exists(file_field.path):
                print(f"MISSING: {file_field.name} (ID: {obj.id})")
                
                # Fuzzy matching: find files with same base name but different suffix
                dir_path = os.path.dirname(file_field.path)
                base_name = os.path.basename(file_field.name)
                # Remove Django's random suffix (usually _7chars)
                import re
                clean_name = re.sub(r'_[a-zA-Z0-9]{7}\.', '.', base_name)
                clean_base = clean_name.split('.')[0]
                
                if os.path.exists(dir_path):
                    matches = [f for f in os.listdir(dir_path) if clean_base in f]
                    if matches:
                        print(f"  FOUND POTENTIAL MATCHES: {matches}")
                        for m in matches:
                             source = os.path.join(dir_path, m)
                             dest = file_field.path
                             print(f"  SUGGESTION: cp \"{source}\" \"{dest}\"")
                
                missing += 1
    print(f"Total {model.__name__}.{field_name}: {total}, Missing: {missing}")

check_files(Template, 'svg_file')
check_files(Template, 'banner')
check_files(PurchasedTemplate, 'svg_file')
check_files(Font, 'font_file')
