# -*- coding: utf-8 -*-
import re, json

SRC = r"D:\mazz\فایل ها.txt"
OUT = r"D:\mazz\output.json"

with open(SRC, encoding="utf-8") as f:
    raw_lines = f.read().split("\n")

# strip BOM / whitespace per line, keep list aligned
lines = [ln.replace("﻿", "").rstrip() for ln in raw_lines]

def strip_line(s):
    return s.strip()

PERSIAN = "۰۱۲۳۴۵۶۷۸۹"
LATIN = "0123456789"
TRANS = {ord(p): l for p, l in zip(PERSIAN, LATIN)}

def to_latin(s):
    return s.translate(TRANS)

def next_nonempty(idx):
    j = idx + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    return lines[j].strip() if j < len(lines) else ""

def is_url(s):
    return s.startswith("http")

def extract_subject(s):
    s = s.replace("💎", "").strip()
    s = re.sub(r"^کلاس[‌ ]های\s*", "", s).strip()
    return s

def clean_header(s, marker_chars):
    for ch in marker_chars:
        s = s.replace(ch, "")
    return s.strip()

folders = []
cur_subject = None
cur_teacher = None
cur_folder = None
cur_session = None
prev_order = None

def make_order(name):
    global prev_order
    is_range = re.match(r"^بخش\s+[0-9۰-۹]+\s+تا\s+", name)
    m = re.match(r"^(?:جلسه|بخش)\s+(-?[0-9۰-۹]+)", name)
    if m and not is_range:
        order = int(to_latin(m.group(1)))
    else:
        order = (prev_order + 1) if prev_order is not None else 1
    prev_order = order
    return order

i = 0
while i < len(lines):
    line = lines[i].strip()
    if not line:
        i += 1
        continue

    if line.startswith("Stream Class,"):
        i += 1
        continue
    if line.startswith("💎"):
        cur_subject = extract_subject(line)
        i += 1
        continue
    if line.startswith("✔"):
        cur_teacher = clean_header(line, ["✔", "️"]).strip()
        i += 1
        continue
    if line.startswith("⬅"):
        cur_folder_title = clean_header(line, ["⬅", "️"]).strip()
        cur_folder = {
            "subject": cur_subject,
            "folder_name": f"{cur_teacher} - {cur_folder_title}",
            "files": [],
        }
        folders.append(cur_folder)
        cur_session = None
        prev_order = None
        i += 1
        continue
    if "@Stream_Konkur" in line or "@StreamClass" in line:
        i += 1
        continue
    if is_url(line):
        if cur_session is not None:
            cur_session["links"].append(line.strip())
        else:
            print("WARNING: URL with no session at line", i + 1, line)
        i += 1
        continue

    # candidate title or noise
    nxt = next_nonempty(i)
    if is_url(nxt):
        # it's a title
        name = re.sub(r"^\.+", "", line).strip()
        order = make_order(name)
        cur_session = {"order": order, "name": name, "links": []}
        cur_folder["files"].append(cur_session)
    # else: noise -> skip
    i += 1

# validation
input_urls = sum(1 for ln in lines if ln.strip().startswith("http"))
output_urls = sum(len(fi["links"]) for fo in folders for fi in fo["files"])

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(folders, f, ensure_ascii=False, indent=2)

# reload to ensure valid
with open(OUT, encoding="utf-8") as f:
    reloaded = json.load(f)

print("=== STATS ===")
print("folders:", len(folders))
print("input URLs :", input_urls)
print("output URLs:", output_urls)
print("match:", input_urls == output_urls)
print()
for fo in folders:
    n_files = len(fo["files"])
    n_links = sum(len(fi["links"]) for fi in fo["files"])
    print(f"[{fo['subject']}] {fo['folder_name']}  -> files={n_files}, links={n_links}")
