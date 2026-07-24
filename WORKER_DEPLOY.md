# Cloudflare Worker 部署指南

实时刷新功能的后端中转服务，部署后看板的"刷新数据"按钮就能实时拉取腾讯文档最新数据。

## 方案一：网页控制台部署（推荐，最简单）

### 第 1 步：注册 Cloudflare
访问 https://dash.cloudflare.com/ 注册账号（免费版够用）

### 第 2 步：创建 Worker
1. 登录后，左侧菜单 → **Workers & Pages**
2. 点 **Create application** → **Create Worker**
3. 给 Worker 起个名字（比如 `logistics-sync`），点 **Deploy**

### 第 3 步：粘贴代码
1. 部署成功后，点 **Edit Code**（编辑代码）
2. 把 `worker.js` 的全部内容复制粘贴进去
3. 右上角点 **Deploy** 保存

### 第 4 步：配置环境变量
1. 回到 Worker 详情页 → 点 **Settings** 标签
2. 左侧选 **Variables**
3. 在 **Environment Variables** 下点 **Add variable**，依次添加：

| 变量名 | 值 | 加密 |
|--------|-----|------|
| `TENCENT_COOKIE` | `TOK=xxx; DOC_SID=yyy` | ✅ Encrypt |
| `TENCENT_SHEET_ID` | `DWk1ESWh0VFJKUGlI` | 否 |
| `SHEET_TAB_NAME` | `Logistics Detail` | 否 |
| `ALLOW_ORIGIN` | `*`（或你的看板域名） | 否 |

4. 点 **Deploy** 生效

### 第 5 步：测试
访问 `https://你的-worker名.workers.dev/api/sync`，应该返回 JSON 数据。

### 第 6 步：更新看板
1. 打开 `logistics_dashboard.html`
2. 找到 `SYNC_API_URL` 这一行
3. 改成你的 Worker 地址：
   ```js
   const SYNC_API_URL = 'https://logistics-sync.xxx.workers.dev/api/sync';
   ```
4. 重新部署看板页面

---

## 方案二：命令行部署（适合开发者）

### 前置条件
- 安装 Node.js
- Cloudflare 账号

### 部署命令
```bash
# 安装 wrangler
npm install -g wrangler

# 登录
npx wrangler login

# 部署
npx wrangler deploy
```

### 设置环境变量
```bash
npx wrangler secret put TENCENT_COOKIE
npx wrangler secret put TENCENT_SHEET_ID
npx wrangler secret put SHEET_TAB_NAME
npx wrangler secret put ALLOW_ORIGIN
```

---

## 免费额度说明
Cloudflare Worker 免费版：
- 每天 10 万次请求
- 每次请求最多 10ms CPU 时间
- 完全够用（一次刷新 = 一次请求）

## 注意事项
1. **Cookie 会过期**：腾讯文档 Cookie 一般 7~30 天失效，失效后同步会报错，需要重新抓 Cookie 并更新环境变量
2. **安全建议**：`ALLOW_ORIGIN` 建议设置为你的看板实际域名，不要用 `*`，防止被滥用
3. **性能**：每次刷新需要 3~10 秒（腾讯文档导出需要时间），属于正常现象
