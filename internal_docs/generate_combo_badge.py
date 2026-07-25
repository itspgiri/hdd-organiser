import urllib.request

def get_svg(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response:
        return response.read().decode('utf-8')

urls = [
    "https://img.shields.io/badge/LinkedIn-0077B5?style=for-the-badge&logo=linkedin&logoColor=white",
    "https://img.shields.io/badge/Instagram-E4405F?style=for-the-badge&logo=instagram&logoColor=white",
    "https://img.shields.io/badge/YouTube-FF0000?style=for-the-badge&logo=youtube&logoColor=white"
]

svgs = [get_svg(url) for url in urls]

import xml.etree.ElementTree as ET

# Parse SVGs and extract width/height
parsed = []
total_width = 0
max_height = 0

for svg in svgs:
    root = ET.fromstring(svg)
    w = int(float(root.attrib.get('width', 0)))
    h = int(float(root.attrib.get('height', 0)))
    parsed.append({'root': root, 'w': w, 'h': h, 'raw': svg})
    total_width += w + 10 # 10px spacing
    max_height = max(max_height, h)

total_width -= 10 # remove last spacing

combo_svg = f'<svg width="{total_width}" height="{max_height}" viewBox="0 0 {total_width} {max_height}" xmlns="http://www.w3.org/2000/svg">\n'
current_x = 0
for p in parsed:
    # We can just embed the raw SVG inside an <svg> tag as a nested SVG
    raw = p['raw']
    # replace <?xml...> if present
    if raw.startswith("<?xml"):
        raw = raw.split("?>", 1)[1]
    
    combo_svg += f'  <svg x="{current_x}" y="0" width="{p["w"]}" height="{p["h"]}">{raw}</svg>\n'
    current_x += p["w"] + 10

combo_svg += '</svg>'

with open("socials.svg", "w") as f:
    f.write(combo_svg)
print("Combo badge generated!")
