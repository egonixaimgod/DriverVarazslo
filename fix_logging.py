import sys

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. _delete_ghost_devices_sync
old1 = '''        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],'''
new1 = '''        logging.debug(f"[CMD] Popen futtatása: {ps_script[:300]}...")
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],'''
text = text.replace(old1, new1)

# 2. _scan_and_install_wu_sync
old2 = '''            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", install_ps],'''
new2 = '''            logging.debug(f"[CMD] Popen futtatása: {install_ps[:300]}...")
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", install_ps],'''
text = text.replace(old2, new2)

# 3. _install_wu_api
old3 = '''            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],'''
new3 = '''            logging.debug(f"[CMD] Popen futtatása: {ps_script[:300]}...")
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],'''
# Since old3 is the same text as old1, but old1 was already replaced! Actually, wait.
# The text replace for old1 replaced BOTH if they match exactly!
# Let's check if they matched. old1 and old3 match EXACTLY. So both were replaced!

# 4. backup_third_party
old4 = '''            process = subprocess.Popen(
                dism_cmd,'''
new4 = '''            logging.debug(f"[CMD] Popen futtatása: {' '.join(dism_cmd)}")
            process = subprocess.Popen(
                dism_cmd,'''
text = text.replace(old4, new4)


with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)
