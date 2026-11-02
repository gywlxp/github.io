#!/usr/bin/env python3
import argparse
import re
import sys
from collections import defaultdict
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def normalize_name(name):
    name = re.sub(r'\s+', ' ', name.strip())
    name = re.sub(r'[（(].*?[）)]', '', name)
    name = re.sub(r'(高清|HD|FHD|4K|1080P|720P|频道|电视台)$', '', name, flags=re.I)
    return name.strip() or '未知频道'

def parse_txt(content):
    channels = []
    genre = '未分类'
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            if line.lower().startswith('#genre#'):
                continue
            if line.endswith(',#genre#') or line.endswith(',#genre'):
                genre = line.split(',')[0].strip()
            continue
        if ',' in line and re.match(r'^https?://', line.split(',')[-1].strip()):
            name, url = line.rsplit(',', 1)
            channels.append({'name': normalize_name(name), 'url': url.strip(), 'group': genre})
    return channels

def parse_m3u(content):
    channels = []
    current = {}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF:'):
            name = re.search(r',(.+)$', line)
            group = re.search(r'group-title="([^"]+)"', line)
            current = {
                'name': normalize_name(name.group(1) if name else '未知频道'),
                'group': group.group(1) if group else '未分类'
            }
        elif line.startswith('http') and current:
            current['url'] = line
            channels.append(current)
            current = {}
    return channels

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--meta', required=True)
    parser.add_argument('--max-per-channel', type=int, default=3)
    args = parser.parse_args()

    all_channels = []
    source_stats = []
    with open(args.input, 'r', encoding='utf-8') as f:
        sources = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    for url in sources:
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=15) as resp:
                content = resp.read().decode('utf-8', errors='ignore')
            if '#EXTM3U' in content or '#EXTINF' in content:
                chans = parse_m3u(content)
            else:
                chans = parse_txt(content)
            source_stats.append({'url': url, 'count': len(chans), 'ok': True})
            all_channels.extend(chans)
            print(f'[+] 抓取成功 {url}，共 {len(chans)} 个频道')
        except Exception as e:
            source_stats.append({'url': url, 'count': 0, 'ok': False, 'error': str(e)})
            print(f'[-] 抓取失败 {url}: {e}')

    # 去重并分组
    grouped = defaultdict(list)
    seen_urls = set()
    for chan in all_channels:
        if chan['url'] in seen_urls:
            continue
        seen_urls.add(chan['url'])
        key = (chan['group'], chan['name'])
        if len(grouped[key]) < args.max_per_channel:
            grouped[key].append(chan['url'])

    # 写出m3u
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for (group, name), urls in sorted(grouped.items()):
            for idx, url in enumerate(urls):
                suffix = f' 线路{idx+1}' if len(urls) > 1 else ''
                f.write(f'#EXTINF:-1 group-title="{group}",{name}{suffix}\n{url}\n')
    
    total = sum(len(v) for v in grouped.values())
    groups = defaultdict(int)
    for (g, n), urls in grouped.items():
        groups[g] += len(urls)
    
    import json
    meta = {
        'total_channels': len(grouped),
        'total_streams': total,
        'groups': dict(sorted(groups.items(), key=lambda x: -x[1])),
        'sources': source_stats,
        'updated_at': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    with open(args.meta, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    
    print(f'\n=== 聚合完成 ===')
    print(f'去重后频道数：{len(grouped)}')
    print(f'总线路数：{total}')
    print(f'成功源：{sum(1 for s in source_stats if s["ok"])}/{len(sources)}')

if __name__ == '__main__':
    main()
