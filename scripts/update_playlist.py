#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV / 影视源聚合脚本（零第三方依赖，纯标准库实现）

流程：
  1. 从源列表文件读取若干个公开直播源地址（m3u / m3u8 / txt 均支持）
  2. 逐个下载并解析出 (频道名, 播放地址) 条目
  3. 归一化频道名 -> 按播放地址去重 -> 按频道分组
  4. 可选：并发做连通性检测，剔除失效链接
  5. 输出标准 m3u 播放列表 + meta.json 站点元数据

用法示例：
  python3 scripts/update_playlist.py --sources playlist/sources.txt --out-dir playlist
  python3 scripts/update_playlist.py --probe --max-per-channel 2
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 频道名里用于分组归一化时需要剥离的画质 / 修饰后缀
QUALITY_RE = re.compile(
    r"(超高清|高清|标清|蓝光|原画|HD|FHD|UHD|SD|4K|8K|1080[Pp]?|720[Pp]?|576[Pp]?|480[Pp]?)",
    re.IGNORECASE,
)
NOISE_RE = re.compile(r"[\s\-_·•|（）()\[\]【】]+")

# 按频道名推断分组，用于 m3u 的 group-title
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
    # 央视 / 国家级
    "CCTV", "CGTN", "CHC", "SMG", "BTV",
    # 各大省级 / 地方台常出现的英文名
    "HUNAN", "ZHEJIANG", "JIANGSU", "DRAGON", "GUANGDONG", "SHENZHEN",
    "BEIJING", "SHANGHAI", "TIANJIN", "CHONGQING", "SICHUAN", "ANHUI",
    "SHANDONG", "HENAN", "HUBEI", "FUJIAN", "LIAONING", "HEILONGJIANG",
    "JILIN", "YUNNAN", "GUIZHOU", "GANSU", "NINGXIA", "XINJIANG", "TIBET",
    "HAINAN", "JIANGXI", "SHANXI", "SHAANXI", "GUANGXI", "HEBEI",
    # 港澳台
    "TVB", "RTHK", "HOY", "VIUTV", "PHOENIX", "TTV", "CTS", "FTV", "PTS", "TVBS",
    "凤凰", "翡翠", "明珠",
)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def is_cn_channel(name):
    """判断频道名是否属于中国（华语）电视台。"""
    if not name:
        return False
    if CJK_RE.search(name):
        return True
    upper = name.upper()
    return any(kw in upper for kw in CN_KEYWORDS)


def fetch(url, timeout=20):
    """下载文本内容，失败返回 None 并打印原因。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
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
            # 频道名取最后一个逗号后的部分
            pending = line.rsplit(",", 1)[-1].strip() or None
        elif line.startswith("#"):
            continue
        elif pending is not None:
            # 只保留真正的流地址行
            if line.startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://")):
                entries.append((pending, line))
            pending = None
    return entries


def parse_txt(text):
    """解析 ``频道名,地址`` 形式的 txt 源，支持 #genre# 分组行。"""
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            continue
        name, url = line.split(",", 1)
        name, url = name.strip(), url.strip()
        if url == "#genre#" or not url.startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://")):
            continue
        entries.append((name or "未命名频道", url))
    return entries


def parse_any(text):
    """按内容特征自动判断是 m3u 还是 txt。"""
    return parse_m3u(text) if "#EXTM3U" in text or "#EXTINF" in text else parse_txt(text)


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
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
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


def build_m3u(channels):
    """把有序的 {分组: [(频道名, 地址)]} 渲染成 m3u 文本。"""
    out = ["#EXTM3U"]
    for group, items in channels.items():
        for name, url in items:
            out.append(f'#EXTINF:-1 tvg-name="{name}" group-title="{group}",{name}')
            out.append(url)
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description="IPTV 播放列表聚合脚本")
    parser.add_argument("--sources", default="playlist/sources.txt", help="源列表文件")
    parser.add_argument("--out-dir", default="playlist", help="输出目录")
    parser.add_argument("--max-per-channel", type=int, default=3, help="每个频道保留的最大源数量")
    parser.add_argument("--probe", action="store_true", help="开启连通性检测（默认关闭）")
    parser.add_argument("--workers", type=int, default=16, help="并发线程数")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sources = load_sources(args.sources)
    if not sources:
        print("源列表为空，未找到任何可用源。", file=sys.stderr)
    print(f"共 {len(sources)} 个源待抓取")

    seen_urls = set()
    buckets = OrderedDict()          # norm_key -> [(频道名, 地址)]
    source_stats = []
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
            key = norm_key(name)
            bucket = buckets.setdefault(key, [])
            if len(bucket) >= args.max_per_channel:
                continue
            seen_urls.add(url)
            bucket.append((name, url))
            kept += 1
        source_stats.append({"url": src, "ok": True, "entries": kept})
        print(f"  · {src} -> 收到 {len(entries)} 条，采用 {kept} 条")

    # 可选的连通性检测
    if args.probe:
        targets = [(k, n, u) for k, items in buckets.items() for (n, u) in items]
        print(f"开始连通性检测，共 {len(targets)} 条链接 ...")
        alive = {}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(probe, u): (k, n, u) for (k, n, u) in targets}
            for fut in as_completed(futures):
                key, name, url = futures[fut]
                if fut.result():
                    alive.setdefault(key, []).append((name, url))
        buckets = OrderedDict((k, v) for k, v in buckets.items() if alive.get(k))
        buckets = OrderedDict((k, alive[k]) for k in buckets)
        print(f"检测完成，存活频道 {len(buckets)} 个")

    # 按分组整理并排序
    grouped = OrderedDict((g, []) for g in GROUP_RULES)
    grouped["其他"] = []
    for key in sorted(buckets):
        items = buckets[key]
        if not items:
            continue
        group = guess_group(items[0][0])
        grouped.setdefault(group, []).extend(items)
    grouped = OrderedDict((g, items) for g, items in grouped.items() if items)

    stream_count = sum(len(v) for v in grouped.values())
    m3u_text = build_m3u(grouped)
    (out_dir / "all.m3u").write_text(m3u_text, encoding="utf-8")

    tz = timezone(timedelta(hours=8))
    meta = {
        "updated": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
        "updated_iso": datetime.now(tz).isoformat(timespec="seconds"),
        "channel_count": len(buckets),
        "stream_count": stream_count,
        "group_stats": {g: len(items) for g, items in grouped.items()},
        "sources": source_stats,
        "fetched_raw": total_raw,
        "probe_enabled": args.probe,
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"完成：{meta['channel_count']} 个频道 / {stream_count} 条线路 -> {out_dir/'all.m3u'}")


if __name__ == "__main__":
    main()
