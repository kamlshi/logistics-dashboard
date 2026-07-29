# 物流进度看板 · 自动同步版

基于腾讯文档 + GitHub Actions + GitHub Pages 的全自动物流进度看板。

**访问地址：** https://kamlshi.github.io/logistics-dashboard

---

## 架构说明

```
腾讯文档（数据源，公开可查看）
      ↓  dop-api/opendoc（无需 Cookie）
GitHub Actions（每 2 小时自动同步）
      ↓  更新 index.html
GitHub Pages（静态托管）
      ↓  固定域名访问
同事浏览器
```

**你的电脑只负责写代码，同步和部署全自动化，24 小时无人值守。**

> 推荐将腾讯文档设置为「任何人可查看」，这样无需 Cookie，同步永远不会过期。

---

## 项目结构

```
logistics-dashboard/
├── index.html                  # 看板页面（数据嵌入在这里）
├── sync.py                     # 同步脚本：支持公开/私密两种模式
├── worker.js                   # Cloudflare Worker（实时数据 API）
├── wrangler.toml               # Worker 配置
├── api/sync.js                 # Vercel Edge Function（实时数据 API）
├── .github/
│   └── workflows/
│       └── sync.yml            # GitHub Actions 定时任务
├── WORKER_DEPLOY.md            # Worker 部署说明
└── README.md                   # 本文件
```

---

## 同步模式

sync.py 支持两种数据拉取模式，**自动选择最优方式**：

| 模式 | 条件 | 优势 | 缺点 |
|---|---|---|---|
| **公开模式** | 文档设置为「任何人可查看」 | 无需 Cookie，永不过期 | 数据必须公开 |
| **私密模式** | 提供 TENCENT_COOKIE | 数据保密 | Cookie 7~30 天过期 |

**推荐使用公开模式**，无需维护 Cookie，一劳永逸。

---

## 部署步骤

### 第一步：设置腾讯文档权限

**推荐：设置为「任何人可查看」**

1. 打开腾讯文档 → 右上角「分享」
2. 权限设置为「任何人可查看」
3. 这样 sync.py 可以直接通过 opendoc API 拉数据，不需要 Cookie

> 如果数据必须保密，可以用私密模式（需要定期更新 Cookie），见下方说明。

### 第二步：配置 GitHub Secrets

在仓库页面 → Settings → Secrets and variables → Actions → New repository secret

**公开模式**只需配置 1 个 Secret：

| Secret 名称 | 说明 | 示例 |
|---|---|---|
| `TENCENT_SHEET_ID` | 腾讯文档表格 ID | `DWk1ESWh0VFJKUGlI` |

**私密模式**还需额外配置：

| Secret 名称 | 说明 |
|---|---|
| `TENCENT_COOKIE` | 腾讯文档登录 Cookie（含 TOK 和 DOC_SID） |
| `SHEET_TAB_NAME` | 工作表名称或 tab ID（可选） |

> 表格 ID 从文档 URL 中获取：`https://docs.qq.com/sheet/D{这里就是ID}`

### 第三步：开启 GitHub Pages

仓库页面 → Settings → Pages：

- **Source** 选 `Deploy from a branch`
- **Branch** 选 `main` / `/ (root)`
- 保存，等 1-2 分钟部署完成

### 第四步：手动测试一次

仓库页面 → Actions → 左侧「同步腾讯文档数据」→ Run workflow → 点按钮运行

运行成功后，检查：
1. Actions 日志有没有报错
2. index.html 有没有被自动提交更新
3. Pages 页面数据是不是最新的

---

## 同步频率

默认每 **2 小时**同步一次（全天 12 次），保证数据始终新鲜。

想改频率？编辑 `.github/workflows/sync.yml` 里的 `cron` 表达式：

```yaml
schedule:
  - cron: '0 1 * * *'   # UTC 01:00 = 北京 09:00
  - cron: '0 9 * * *'   # UTC 09:00 = 北京 17:00
```

> cron 格式：`分 时 日 月 周`，时间是 UTC，北京时间 = UTC + 8

---

## 实时数据 API（可选）

除了 GitHub Actions 定时嵌入数据，还可以部署实时 API：

- **Cloudflare Worker**：部署 `worker.js`，访问 `https://xxx.workers.dev/api/sync` 获取实时数据
- **Vercel Edge Function**：部署 `api/sync.js`，访问 `/api/sync` 获取实时数据

两种 API 都支持公开/私密模式，与 sync.py 逻辑一致。

> HTML 页面已经内置了实时 API 调用（`SYNC_API_URL`），部署后前端会优先从 API 拉数据，失败时回退到内嵌数据。

---

## 本地测试

想在电脑上先试试脚本能不能跑：

```bash
# 公开模式（无需 Cookie）
export TENCENT_SHEET_ID="DWk1ESWh0VFJKUGlI"
python sync.py

# 私密模式
export TENCENT_COOKIE="你的Cookie"
export TENCENT_SHEET_ID="DWk1ESWh0VFJKUGlI"
python sync.py
```

运行成功后打开 `index.html` 看数据是否更新。

---

## 常见问题

### Q: Actions 报错，说连不上腾讯文档？
跨境网络偶尔抽风，脚本自带 1 次重试。如果经常失败，可以：
- 增加同步频率（多试几次总能成功）
- 或者部署 Cloudflare Worker（国内有节点，更稳定）

### Q: 公开模式下数据安全性？
如果不想让任何人通过链接查看原始数据，可以：
- 使用私密模式（需要定期更新 Cookie）
- 或在 HTML 里加简单的密码保护

### Q: Cookie 过期了怎么办？
重新从浏览器复制 Cookie，到仓库 Secrets 里更新 `TENCENT_COOKIE` 即可，不用改代码。

### Q: 想加多个工作表？
复制 `sync.py` 里的导出逻辑，分别导出后拼成一个大数组，或者分不同变量嵌入。

---

## 维护清单

- [ ] 每周看一眼 Actions 运行状态是否正常
- [ ] 腾讯文档表格结构大改时，同步更新 HTML 渲染逻辑
- [ ] （私密模式）每月检查一次 Cookie 是否过期
