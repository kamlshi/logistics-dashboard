# 物流进度看板 · 自动同步版

基于腾讯文档 + GitHub Actions + GitHub Pages 的全自动物流进度看板。

**访问地址：** https://kamlshi.github.io/logistics-dashboard

---

## 架构说明

```
腾讯文档（数据源）
      ↓  每天定时拉取
GitHub Actions（自动同步）
      ↓  更新 index.html
GitHub Pages（静态托管）
      ↓  固定域名访问
同事浏览器
```

**你的电脑只负责写代码，同步和部署全自动化，24 小时无人值守。**

---

## 项目结构

```
logistics-dashboard/
├── index.html                  # 看板页面（数据嵌入在这里）
├── sync.py                     # 同步脚本：拉腾讯文档 → 嵌 HTML
├── .github/
│   └── workflows/
│       └── sync.yml            # GitHub Actions 定时任务
└── README.md                   # 本文件
```

---

## 部署步骤

### 第一步：准备 HTML 看板

确保你的 `index.html` 中有这行代码（脚本会自动替换数据）：

```html
<script>
  // 数据占位，sync.py 会自动填充
  const EMBEDDED_DATA = [];

  // 你的渲染逻辑，用 EMBEDDED_DATA 渲染表格/图表
  function renderDashboard() {
    // ...
  }
</script>
```

可选：在页面某处加上更新时间显示（脚本会自动更新）：

```html
<span>数据更新时间：--</span>
```

### 第二步：获取腾讯文档 Cookie

1. 浏览器打开腾讯文档，登录你的账号
2. 按 F12 打开开发者工具 → Network（网络）面板
3. 刷新页面，随便点一个请求
4. 在 Request Headers 里找到 `Cookie:`，复制整段值
5. 确认 Cookie 中包含 `TOK=` 和 `DOC_SID=` 字段

> ⚠️ Cookie 有效期一般 7-30 天，过期后重新获取并更新 Secrets 即可。

### 第三步：配置 GitHub Secrets

在仓库页面 → Settings → Secrets and variables → Actions → New repository secret

添加以下 3 个 Secret：

| Secret 名称 | 说明 | 示例 |
|---|---|---|
| `TENCENT_COOKIE` | 第二步获取的完整 Cookie | `TOK=xxx; DOC_SID=yyy; ...` |
| `TENCENT_SHEET_ID` | 腾讯文档表格 ID | `DWk1ESWh0VFJKUGlI` |
| `SHEET_TAB_NAME` | 工作表名称（可选，默认第一个） | `物流进度` |

> 表格 ID 从文档 URL 中获取：`https://docs.qq.com/sheet/D{这里就是ID}`

### 第四步：开启 GitHub Pages

仓库页面 → Settings → Pages：

- **Source** 选 `Deploy from a branch`
- **Branch** 选 `main` / `/ (root)`
- 保存，等 1-2 分钟部署完成

### 第五步：手动测试一次

仓库页面 → Actions → 左侧「同步腾讯文档数据」→ Run workflow → 点按钮运行

运行成功后，检查：
1. Actions 日志有没有报错
2. index.html 有没有被自动提交更新
3. Pages 页面数据是不是最新的

---

## 同步频率

默认每天同步 **2 次**：
- 早上 **09:00**（上班前刷新）
- 下午 **17:00**（下班前刷新）

想改频率？编辑 `.github/workflows/sync.yml` 里的 `cron` 表达式：

```yaml
schedule:
  - cron: '0 1 * * *'   # UTC 01:00 = 北京 09:00
  - cron: '0 9 * * *'   # UTC 09:00 = 北京 17:00
```

> cron 格式：`分 时 日 月 周`，时间是 UTC，北京时间 = UTC + 8

---

## 本地测试

想在电脑上先试试脚本能不能跑：

```bash
# 设置环境变量（Windows PowerShell）
$env:TENCENT_COOKIE = "你的Cookie"
$env:TENCENT_SHEET_ID = "DWk1ESWh0VFJKUGlI"

# 运行
python sync.py
```

运行成功后打开 `index.html` 看数据是否更新。

---

## 常见问题

### Q: Actions 报错，说连不上腾讯文档？
跨境网络偶尔抽风，脚本自带 1 次重试。如果经常失败，可以：
- 增加同步频率（多试几次总能成功）
- 或者把同步任务搬到国内服务器（比如腾讯云函数）

### Q: Cookie 过期了怎么办？
重新从浏览器复制 Cookie，到仓库 Secrets 里更新 `TENCENT_COOKIE` 即可，不用改代码。

### Q: 想加多个工作表？
复制 `sync.py` 里的导出逻辑，分别导出后拼成一个大数组，或者分不同变量嵌入。

### Q: 数据不想公开怎么办？
仓库设为 Private，GitHub Pages 也可以限制访问（企业版支持）。
或者在 HTML 里加个简单的密码访问。

---

## 维护清单

- [ ] 每月检查一次 Cookie 是否过期
- [ ] 每周看一眼 Actions 运行状态是否正常
- [ ] 腾讯文档表格结构大改时，同步更新 HTML 渲染逻辑
