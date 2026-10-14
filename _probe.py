import re

data = open(r'.venv/Lib/site-packages/wcferry/sdk.dll', 'rb').read()

# ASCII
ascii_strs = re.findall(rb'[\x20-\x7e]{4,}', data)
# UTF-16LE (wide)
wide_strs = re.findall(rb'(?:[\x20-\x7e]\x00){4,}', data)

def show(strs, label):
    print(f"===== {label} =====")
    seen = set()
    for s in strs:
        try:
            t = s.decode('utf-16-le', errors='replace') if label == 'UTF-16' else s.decode('ascii', errors='replace')
        except Exception:
            continue
        low = t.lower()
        if any(k in low for k in ['driver', '.sys', 'service', 'registry', 'inject', 'loaddriver', 'wcferry', 'spy', 'wcf', 'wcd', 'dump']):
            if t not in seen:
                seen.add(t)
                print(repr(t))

show(ascii_strs, 'ASCII')
show(wide_strs, 'UTF-16')
