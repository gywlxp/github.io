#!/usr/bin/env python3
import argparse
import json
import re
import datetime
from collections import defaultdict
from urllib.request import urlopen, Request

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

CN_KEYS = ["cctv", "cgtn", "tvb", "vhk", "phoenix", "凤凰", "翡翠", "明珠", "香港", "澳门", "台湾",
           "中天", "东森", "三立", "民视", "台视", "中视", "华视", "公视", "tvbs",
           "卫视", "湖南", "浙江", "江苏", "东方", "北京", "上海", "广东", "深圳", "南方",
           "安徽", "山东", "河南", "湖北", "四川", "重庆", "天津", "河北", "山西", "辽宁",
           "吉林", "黑龙江", "内蒙古", "甘肃", "宁夏", "青海", "新疆", "西藏", "云南", "贵州",
           "广西", "福建", "江西", "海南", "陕西", "cetv", "chc", "cgtn"]

def normalize_name(name):
    name = re.sub(r"\s+", " ", name.strip())
    name = re.sub(r"[（(].*?[）)]", "", name)
    return name.strip() or "未知频道"

def is_chinese(name):
    if re.search(r"[\u4e00-\u9fff]", name):
        return True
    low = name.lower()
    return any(k in low for k in CN_KEYS)

def parse_txt(content):
    channels, genre = [], "未分类"
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if "#genre#" in line.lower():
                genre = line.split(",")[0].replace("#genre#", "").strip()
            continue
        if "," in line:
            name, url = line.rsplit(",", 1)
            if url.strip().startswith("http"):
                channels.append({"name": normalize_name(name), "url": url.strip(), "group": genre})
    return channels

def parse_m3u(content):
    channels, current = [], {}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            m = re.search(r",(.+)$", line)
            g = re.search(r'group-title="([^"]+)"', line)
            current = {"name": normalize_name(m.group(1) if m else "未知频道"),
                       "group": g.group(1) if g else "未分类"}
        elif line.startswith("http") and current:
            current["url"] = line
            channels.append(current)
            current = {}
    return channels

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--meta", required=True)
    p.add_argument("--max-per-channel", type=int, default=3)
    a = p.parse_args()

    with open(a.input, "r", encoding="utf-8") as f:
        sources = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    all_ch, stats = [], []
    for url in sources:
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=20) as r:
                content = r.read().decode("utf-8", errors="ignore")
            chans = parse_m3u(content) if ("#EXTM3U" in content or "#EXTINF" in content) else parse_txt(content)
            chans = [c for c in chans if is_chinese(c["name"])]
            stats.append({"url": url, "count": len(chans), "ok": True})
            all_ch.extend(chans)
            print(f"[+] {url} -> {len(chans)} 个中国频道")
        except Exception as e:
            stats.append({"url": url, "count": 0, "ok": False, "error": str(e)})
            print(f"[-] 失败 {url}: {e}")

    grouped = defaultdict(list)
    seen = set()
    for c in all_ch:
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        key = (c["group"], c["name"])
        if len(grouped[key]) < a.max_per_channel:
            grouped[key].append(c["url"])

    with open(a.output, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for (g, n), urls in sorted(grouped.items()):
            for i, u in enumerate(urls):
                suf = f" 线路{i+1}" if len(urls) > 1 else ""
                f.write(f'#EXTINF:-1 group-title="{g}",{n}{suf}\n{u}\n')

    groups = defaultdict(int)
    for (g, n), urls in grouped.items():
        groups[g] += len(urls)
    meta = {
        "total_channels": len(grouped),
        "total_streams": sum(len(v) for v in grouped.values()),
        "groups": dict(sorted(groups.items(), key=lambda x: -x[1])),
        "sources": stats,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(a.meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"=== 完成 频道 {len(grouped)} / 线路 {meta['total_streams']} ===")

if __name__ == "__main__":
    main()
