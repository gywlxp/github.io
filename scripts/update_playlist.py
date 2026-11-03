#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 播放列表处理脚本（零第三方依赖，纯标准库实现）

两种工作模式
------------
模式 A  聚合模式（不传 --baseline）
    从 sources.txt 抓取所有源 -> 归一化去重 -> 按分组聚合，生成 all.m3u。
    频道清单完全由源决定。

模式 B  清单模式（传 --baseline，推荐；默认工作流用的就是这个）
    以你自己维护的播放列表文件（baseline）为准，通常就指向 playlist/all.m3u：
      · 清单决定有哪些频道、频道名、分组、排列顺序，以及 EXTINF 原始行；
      · 脚本只负责刷新每个频道的播放线路地址；
      · 原有线路仍然可用的，原样保留、绝不替换（即"源有效就不更新"）；
      · 原有线路失效的，才用源里最新的可用线路补位；
      · 若某频道原有线路全部失效、源里也找不到可用线路，则保留它原有的线路，
        保证频道不会从列表里消失（只会在 meta.json 里记入 stale_channels）；
      · 清单里没有的频道不会出现，清单顺序不会被打乱；
      · 清单文件本身只读不写。

用法示例
--------
  # 聚合模式
  python3 scripts/update_playlist.py --sources playlist/sources.txt --out-dir playlist

  # 清单模式（推荐）
  python3 scripts/update_playlist.py \
      --sources playlist/sources.txt \
      --baseline playlist/all.m3u \
      --probe --out-dir playlist --max-per-channel 3
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 频道名里用于归一化去重时需要剥离的画质 / 修饰后缀。
# 注意：这里刻意不剥离 4K / 8K —— 像 CCTV-4K、CCTV-8K 这类名字里，4K/8K 是
# 频道名的一部分而非画质标注；一旦剥掉，就会和 CCTV-4 / CCTV-8 混淆，导致串台。
QUALITY_RE = re.compile(
    r"(超高清|高清|标清|蓝光|原画|HD|FHD|UHD|SD|1080[Pp]?|720[Pp]?|576[Pp]?|480[Pp]?)",
    re.IGNORECASE,
)
NOISE_RE = re.compile(r"[\s\-_·•|（）()\[\]【】]+")

# 按频道名推断分组，用于 m3u 的 group-title（仅聚合模式 / 清单未给分组时使用）
GROUP_RULES = OrderedDict(
    [
        ("央视", ("CCTV", "CGTN", "中央电视台", "中央电视")),
        ("卫视", ("卫视", "凤凰", "翡翠", "明珠")),
        ("港澳台", ("港", "澳", "台", "TVB", "RTHK", "HOY", "ViuTV")),
        ("体育", ("体育", "足球", "篮球", "SPORT", "NBA", "英超")),
        ("影音", ("电影", "影视", "剧场", "音乐", "MV", "少儿", "动画", "动漫")),
        ("新闻", ("新闻", "NEWS", "财经", "资讯")),
    ]
)

# 仅保留中国（华语）电视台：频道名含中文，或命中下列关键词（英文名的华语台）
CN_KEYWORDS = (
    "CCTV", "CGTN", "CHC", "SMG", "BTV",
    "HUNAN", "ZHEJIANG", "JIANGSU", "DRAGON", "GUANGDONG", "SHENZHEN",
    "BEIJING", "SHANGHAI", "TIANJIN", "CHONGQING", "SICHUAN", "ANHUI",
    "SHANDONG", "HENAN", "HUBEI", "FUJIAN", "LIAONING", "HEILONGJIANG",
    "JILIN", "YUNNAN", "GUIZHOU", "GANSU", "NINGXIA", "XINJIANG", "TIBET",
    "HAINAN", "JIANGXI", "SHANXI", "SHAANXI", "GUANGXI", "HEBEI",
    "TVB", "RTHK", "HOY", "VIUTV", "PHOENIX", "TTV", "CTS", "FTV", "PTS", "TVBS",
    "凤凰", "翡翠", "明珠",
)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

STREAM_PREFIX = ("http://", "https://", "rtmp://", "rtsp://", "udp://")


def is_cn_channel(name):
    """判断频道名是否属于中国（华语）电视台。"""
    if not name:
        return False
    if CJK_RE.search(name):
        return True
    upper = name.upper()
    return any(kw in upper for kw in CN_KEYWORDS)


def encode_url(url):
    """对URL里含中文/非ASCII字符的路径与查询部分做百分号编码，避免 ascii 报错。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return url
    encoded_path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    encoded_query = urllib.parse.quote(parts.query, safe="=&%:@!$&'()*+,;=-._~?/")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, encoded_path, encoded_query, parts.fragment)
    )


def fetch(url, timeout=20):
    """下载文本内容，失败返回 None 并打印原因。"""
    safe_url = encode_url(url)
    req = urllib.request.Request(safe_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        print(f"  ! 抓取失败 {url} -> {exc}", file=sys.stderr)
        return None


def parse_m3u(text):
    """解析 m3u / m3u8，返回 [(频道名, 播放地址)]。"""
    entries, pending = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            pending = split_extinf_title(line) or None
        elif line.startswith("#"):
            continue
        elif pending is not None:
            if line.startswith(STREAM_PREFIX):
                entries.append((pending, line))
            pending = None
    return entries


def parse_txt(text):
    """解析 "频道名,地址" 形式的 txt 源，支持 #genre# 分组行。"""
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            continue
        name, url = line.split(",", 1)
        name, url = name.strip(), url.strip()
        if url == "#genre#" or not url.startswith(STREAM_PREFIX):
            continue
        entries.append((name or "未命名频道", url))
    return entries


def parse_any(text):
    """按内容特征自动判断是 m3u 还是 txt。"""
    return parse_m3u(text) if "#EXTM3U" in text or "#EXTINF" in text else parse_txt(text)


def split_extinf_title(line):
    """从 #EXTINF 行里取出频道标题（逗号之后的部分）。

    兼容两种写法：
      #EXTINF:-1 tvg-name="A" group-title="央视",CCTV-1   -> 取"CCTV-1"
      #EXTINF:-1,CCTV-1                                    -> 取"CCTV-1"
    做法：先定位最后一个属性用的引号，其后第一个逗号即为标题分隔符，
    这样标题里带逗号、或属性值里带逗号都不会被误切。
    """
    q = line.rfind('"')
    idx = line.find(",", q + 1) if q != -1 else line.find(",")
    if idx == -1:
        return ""
    return line[idx + 1:].strip()


def parse_baseline(text):
    """解析基线清单，返回 [{group, name, extinf, old}]，保持出现顺序。

    自动识别两种格式：
      · 含 #EXTINF -> 按 m3u 解析：group-title 为分组，逗号后为频道名，
        紧随其后的地址行作为该频道的原有线路；EXTINF 原始行会被记录，
        输出时原样写回，从而保证列表（频道名 / 分组 / 属性）完全不变。
      · 否则按简单清单解析（"分组,频道名" 或 "频道名"）。
    """
    items = []
    if "#EXTINF" in text or "#EXTM3U" in text:
        pending = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#EXTINF"):
                if pending is not None:
                    items.append(pending)
                name = split_extinf_title(line)
                m = re.search(r'group-title="([^"]*)"', line)
                group = m.group(1).strip() if m else ""
                pending = {"group": group, "name": name, "extinf": line, "old": []}
            elif line.startswith("#"):
                continue
            elif pending is not None and line.startswith(STREAM_PREFIX):
                pending["old"].append(line)
        if pending is not None:
            items.append(pending)
    else:
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "," in line:
                group, name = line.split(",", 1)
                items.append({"group": group.strip(), "name": name.strip(),
                              "extinf": "", "old": []})
            else:
                items.append({"group": "", "name": line, "extinf": "", "old": []})
    return [it for it in items if it["name"]]


def norm_key(name):
    """归一化频道名，作为分组去重键。"""
    key = QUALITY_RE.sub("", name or "")
    key = NOISE_RE.sub("", key)
    return key.upper() or (name or "").strip().upper()


def guess_group(name):
    upper = (name or "").upper()
    for group, keywords in GROUP_RULES.items():
        if any(kw.upper() in upper for kw in keywords):
            return group
    return "其他"


def probe(url, timeout=6):
    """轻量连通性检测：HEAD 优先，必要时回退 GET 读取少量字节。"""
    safe_url = encode_url(url)
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(safe_url, headers={"User-Agent": UA}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                if method == "GET":
                    resp.read(256)
                if status < 400:
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            continue
    return False


def load_sources(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def build_m3u(grouped):
    """把有序的 {分组: [(频道名, EXTINF行, 地址)]} 渲染成 m3u 文本。

    EXTINF 行为空时自动生成；非空时原样保留（清单模式下即"列表不变"）。
    """
    out = ["#EXTM3U"]
    for group, items in grouped.items():
        for name, extinf, url in items:
            out.append(extinf or f'#EXTINF:-1 tvg-name="{name}" group-title="{group}",{name}')
            out.append(url)
    return "\n".join(out) + "\n"


def collect_index(sources, cap, source_stats):
    """抓取所有源，返回两套索引（结构均为 {"name","group","urls"}）：

      · index   ：norm_key -> slot，归一化去重后的索引（聚合模式用）；
      · by_name ：原始频道名 -> slot，精确名索引（清单模式优先用，
                  保证频道名完全相同的清单项各自匹配自己的线路，不被归一化误并）。
    """
    index = OrderedDict()
    by_name = OrderedDict()
    seen_urls = set()
    total_raw = 0
    for src in sources:
        text = fetch(src)
        if not text:
            source_stats.append({"url": src, "ok": False, "entries": 0})
            continue
        entries = parse_any(text)
        total_raw += len(entries)
        kept = 0
        for name, url in entries:
            if url in seen_urls:
                continue
            slot = index.setdefault(
                norm_key(name), {"name": name, "group": guess_group(name), "urls": []}
            )
            exact = by_name.setdefault(
                name.strip(), {"name": name, "group": guess_group(name), "urls": []}
            )
            if len(slot["urls"]) >= cap and len(exact["urls"]) >= cap:
                continue
            seen_urls.add(url)
            if len(slot["urls"]) < cap:
                slot["urls"].append(url)
            if len(exact["urls"]) < cap:
                exact["urls"].append(url)
            kept += 1
        source_stats.append({"url": src, "ok": True, "entries": kept})
        print(f"  · {src} -> 收到 {len(entries)} 条，采用 {kept} 条")
    return index, by_name, total_raw


def write_outputs(out_dir, grouped, meta):
    stream_count = sum(len(v) for v in grouped.values())
    (out_dir / "all.m3u").write_text(build_m3u(grouped), encoding="utf-8")
    meta["channel_count"] = len({n for items in grouped.values() for n, _e, _u in items})
    meta["stream_count"] = stream_count
    meta["group_stats"] = {g: len(items) for g, items in grouped.items()}
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta["channel_count"], stream_count


def main():
    parser = argparse.ArgumentParser(description="IPTV 播放列表处理脚本")
    parser.add_argument("--sources", default="playlist/sources.txt", help="源列表文件")
    parser.add_argument("--baseline", default=None,
                        help="频道清单文件（清单模式）；不传则为聚合模式")
    parser.add_argument("--out-dir", default="playlist", help="输出目录")
    parser.add_argument("--max-per-channel", type=int, default=3,
                        help="每个频道补位时保留的最大线路数")
    parser.add_argument("--probe", action="store_true", help="开启线路连通性检测")
    parser.add_argument("--workers", type=int, default=32, help="并发线程数")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sources = load_sources(args.sources)
    if not sources:
        print("源列表为空，未找到任何可用源。", file=sys.stderr)
    print(f"共 {len(sources)} 个源待抓取")

    source_stats = []
    index, by_name, total_raw = collect_index(sources, args.max_per_channel, source_stats)

    tz = timezone(timedelta(hours=8))
    meta = {
        "updated": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
        "updated_iso": datetime.now(tz).isoformat(timespec="seconds"),
        "sources": source_stats,
        "fetched_raw": total_raw,
        "probe_enabled": args.probe,
    }

    if args.baseline:
        baseline_path = Path(args.baseline)
        if not baseline_path.exists():
            print(f"清单文件不存在：{args.baseline}", file=sys.stderr)
            return 1
        baseline = parse_baseline(baseline_path.read_text(encoding="utf-8"))
        print(f"清单模式：清单共 {len(baseline)} 个频道")

        plan = []
        for it in baseline:
            # 先按原始频道名精确匹配；匹配不到再退回归一化匹配
            slot = by_name.get(it["name"].strip()) or index.get(norm_key(it["name"]))
            plan.append({
                "group": it["group"] or (slot["group"] if slot else guess_group(it["name"])),
                "name": it["name"],
                "extinf": it["extinf"],
                "old": list(it["old"]),
                "new": list(slot["urls"]) if slot else [],
            })

        alive = None
        if args.probe:
            targets = set()
            for p in plan:
                targets.update(p["old"])
                targets.update(p["new"])
            print(f"开始连通性检测，共 {len(targets)} 条线路 ...")
            alive = set()
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(probe, u): u for u in targets}
                for fut in as_completed(futures):
                    if fut.result():
                        alive.add(futures[fut])
            print(f"检测完成，可用线路 {len(alive)} 条")

        def usable(u):
            return alive is None or u in alive

        grouped = OrderedDict()
        unmatched, stale = [], []
        for p in plan:
            picked, dead = [], 0
            # 1) 原有线路里仍然可用的，原样保留，绝不替换
            for u in p["old"]:
                if usable(u):
                    picked.append(u)
                else:
                    dead += 1
            # 2) 失效的原有线路，才用源里的新线路补位（几个失效补几条）
            if dead:
                for u in p["new"]:
                    if len(picked) >= len(p["old"]):
                        break
                    if usable(u) and u not in picked:
                        picked.append(u)
            # 3) 清单里原本没写线路的频道，用源里的可用线路填充
            if not p["old"] and p["new"]:
                for u in p["new"]:
                    if len(picked) >= args.max_per_channel:
                        break
                    if usable(u) and u not in picked:
                        picked.append(u)
            # 4) 兜底：一条可用线路都没有 -> 保留原有线路，保证频道不消失
            if not picked:
                picked = list(p["old"])
                if picked:
                    stale.append(p["name"])

            if not picked:
                unmatched.append(p["name"])
                continue
            for u in picked:
                grouped.setdefault(p["group"], []).append((p["name"], p["extinf"], u))

        meta["mode"] = "baseline"
        meta["baseline"] = str(baseline_path)
        meta["unmatched"] = unmatched
        meta["stale_channels"] = stale
        channel_count, stream_count = write_outputs(out_dir, grouped, meta)

        if unmatched:
            print(f"以下 {len(unmatched)} 个频道在源里没找到、清单里也没写线路，未输出："
                  f"{', '.join(unmatched)}")
        if stale:
            print(f"以下 {len(stale)} 个频道线路全部失效、且源里也无可用替代，已保留原有线路："
                  f"{', '.join(stale)}")
        print(f"完成：{channel_count} 个频道 / {stream_count} 条线路 -> {out_dir / 'all.m3u'}")
        return 0

    # 聚合模式
    grouped = OrderedDict((g, []) for g in GROUP_RULES)
    grouped["其他"] = []
    for key in sorted(index):
        slot = index[key]
        g = guess_group(slot["name"])
        for u in slot["urls"]:
            grouped.setdefault(g, []).append((slot["name"], "", u))
    grouped = OrderedDict((g, items) for g, items in grouped.items() if items)

    meta["mode"] = "aggregate"
    channel_count, stream_count = write_outputs(out_dir, grouped, meta)
    print(f"完成：{channel_count} 个频道 / {stream_count} 条线路 -> {out_dir / 'all.m3u'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
