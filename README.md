# 个人导航页 + 影视源自动更新

一个纯静态、零后端、零费用的个人工作台：**GitHub Pages 托管的导航页** + **GitHub Actions 定时更新的影视源聚合**。两者共用一个仓库，导航页会自动读取最新的聚合结果并展示更新状态。

---

## 一、它能做什么

| 模块 | 说明 |
| --- | --- |
| 🧭 个人导航页 | 分组展示常用站点，支持关键词搜索、分类切换、一键复制订阅地址；纯 HTML/CSS/JS，无后端 |
| 🔄 影视源自动更新 | 每天定时抓取公开直播源，自动去重、分组、重新生成播放列表，并提交回仓库 |
| 📊 状态看板 | 导航页自动读取 `playlist/meta.json`，展示频道数、线路数、源站状态与最近更新时间 |
| 📈 分布图表 | 用 ECharts 展示各分组的频道分布 |

---

## 二、目录结构

```
github-hub/
├── index.html                          # 导航页（GitHub Pages 入口，必须叫 index.html）
├── README.md                           # 本说明文件
├── playlist/
│   ├── sources.txt                     # 影视源列表（在这里增删源地址）
│   ├── all.m3u                         # 自动生成的播放列表（勿手改）
│   └── meta.json                       # 自动生成的元数据（勿手改）
├── scripts/
│   └── update_playlist.py              # 聚合脚本（零第三方依赖）
└── .github/
    └── workflows/
        ├── deploy-pages.yml            # 把仓库发布成 Pages 站点
        └── update-playlist.yml         # 定时更新影视源
```

---

## 三、快速开始（约 5 分钟）

### 1. 创建仓库

两种命名方式，效果不同：

| 仓库名 | 访问地址 | 用途 |
| --- | --- | --- |
| `你的用户名.github.io` | `https://你的用户名.github.io` | 个人主站（一个账号只能有一个） |
| 任意名（如 `homepage`） | `https://你的用户名.github.io/homepage` | 项目子站（可以有多个） |

> 仓库必须是 **Public**（免费账户下私有仓库无法使用 Pages）。

### 2. 上传文件

把本项目所有文件（**含 `.github` 隐藏目录**）推送到仓库。命令行方式：

```bash
git init
git add .
git commit -m "init: 个人导航页 + 影视源自动更新"
git branch -M main
git remote add origin https://github.com/你的用户名/你的用户名.github.io.git
git push -u origin main
```

### 3. 开启 Pages

仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。

推送后 `deploy-pages.yml` 会自动运行，完成后站点即可访问（也可到 **Actions** 页面手动触发）。

> 想更省事？Source 直接选 **Deploy from a branch → main → / (root)** 也能发布，此时可以删掉 `deploy-pages.yml`。

### 4. 允许 Actions 提交

仓库 **Settings → Actions → General → Workflow permissions** 选择 **Read and write permissions**，否则更新任务无法把结果推回仓库。

### 5. 手动跑一次更新

进入 **Actions → Update Playlist → Run workflow**，跑通后仓库里会生成最新的 `playlist/all.m3u` 与 `playlist/meta.json`，导航页随即显示真实数据。

---

## 四、自定义导航页

所有站点都写在 `index.html` 末尾的 `GROUPS` 数组里，格式：

```js
{ id:'dev', name:'开发', items:[
  { name:'GitHub', desc:'代码托管与协作平台', url:'https://github.com', color:'#24292f', letter:'G' },
  // 想加就照抄一行
]},
```

- `name` 站点名；`desc` 一句话描述；`url` 链接；`color` 图标底色；`letter` 图标里的字母。
- 新增分组时，`id` 保持唯一即可，页面上的分类标签会自动多出一个。
- 把 `url` 写成 `#playlist` 这类锚点，可以做页内跳转（示例中的「电视直播」就是）。

---

## 五、自定义影视源

### 增删来源

编辑 `playlist/sources.txt`，每行一个源地址，支持 `m3u` / `m3u8` / `txt` 三种格式：

```txt
https://example.com/my-channels.m3u
```

### 调整更新频率

编辑 `.github/workflows/update-playlist.yml`：

```yaml
on:
  schedule:
    - cron: '0 22 * * *'   # UTC 22:00 = 北京时间次日 06:00
    - cron: '0 10 * * *'   # UTC 10:00 = 北京时间 18:00
```

> ⚠️ **cron 用的是 UTC 时间，北京时间要减 8 小时。** 例如想每天北京时间 07:00 更新，写成 `0 23 * * *`。

### 每个频道保留多少条线路

工作流命令里的 `--max-per-channel 3` 控制每个频道的最大备用线路数，值越小列表越精简。

### 本地手动跑一次

```bash
python3 scripts/update_playlist.py --sources playlist/sources.txt --out-dir playlist --max-per-channel 3
# 加 --probe 可先做连通性检测，剔除失效链接（GitHub 服务器在海外，检测国内源不够准，默认关闭）
```

脚本零第三方依赖，只需 Python 3.8+。

---

## 六、常见问题

**Q：Pages 打开是 404？**
检查三点：仓库名是否正确（`.github.io` 必须全小写）；入口文件是否叫 `index.html`；Settings → Pages 的 Source 是否已配置。

**Q：Actions 更新后没提交成功？**
多半是第 4 步的 Workflow permissions 没开成 **Read and write**，或仓库开了分支保护。到 Actions 页面查看运行日志即可定位。

**Q：定时任务不触发？**
GitHub 的 `schedule` 在仓库长时间无活动时可能被暂停；此外定时任务高峰期可能延迟几分钟。可随时手动 Run workflow。

**Q：想要更多频道怎么办？**
往 `sources.txt` 追加公开源即可，脚本会自动合并去重。也可以直接订阅现成的聚合项目（见下）。

**Q：播放列表怎么用？**
把 `https://你的用户名.github.io/playlist/all.m3u` 填进 PotPlayer、TVBox、Kodi、VLC 等播放器的订阅地址即可。

---

## 七、备选：直接用现成的聚合项目

如果你不想自己维护脚本，GitHub 上有成熟的开源方案，玩法类似、开箱即用：

- **Guovin/TV**（原 IPTV）：支持工作流 / 命令行 / GUI / Docker 四种运行方式，内置智能检测与定时更新，频道类型覆盖央视、卫视、影视、体育等。
- **Playlist-AutoUpdater**：基于 Actions 的 m3u 每日更新器，订阅链接保持不变，适合已经习惯用 Gist 托管播放列表的用户。

本项目保留自建脚本，是为了让你完全掌控源列表、更新频率和输出格式——两条路线可以按需取用。

---

## 八、许可

页面与脚本均为通用模板代码，可自由修改、分发。聚合的频道内容版权归各原始权利方所有，请遵守当地法律法规与来源站点的使用条款。
