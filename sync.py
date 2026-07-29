"""物流看板同步脚本（本机常驻·最终版）

数据来源：腾讯文档在线表格，通过已登录的浏览器（复用 qclaw 登录态 cookie）打开文档，
直接从页面内存模型 SpreadsheetApp.workbook.activeSheet.getCellDataAtPosition() 读取整张表。
无需导出 API、无需解析 protobuf、无需抓 WebSocket。

流程：
1) 解密 qclaw xbrowser profile 的腾讯文档登录 cookie（DPAPI + AESGCM，含 32 字节 header 剥离）
2) Playwright 启动 Chromium，注入 cookie 并打开文档（cookie 由 Chrome 自动续期）
3) page.evaluate 读取整张表二维数组（公式单元格取 formulaResult.value）
4) 表头→看板字段映射，生成 data.json
5) 推送到 GitHub Pages（kamlshi.github.io/logistics-dashboard/data.json）

运行：python sync.py
依赖：playwright, cryptography, requests
"""
import os, sys, json, time, shutil, sqlite3, base64, ctypes
from ctypes import wintypes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import requests
from playwright.sync_api import sync_playwright

# ---------- 本地密钥（不从仓库读取，避免泄露）----------
def _load_env_file(path):
    """从本地 .env_sync 读取 GITHUB_PAT 等（该文件不入库）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env_sync"))

# ---------- 配置 ----------
CHROME = r"C:\Users\pc\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
PROFILE_SRC = r"C:\Users\pc\.qclaw\tools\xbrowser\profiles\edge\Default"
DOC_URL = "https://docs.qq.com/sheet/DWk1ESWh0VFJKUGlI"
TMP = r"C:\Users\pc\WorkBuddy\2026-07-29-09-35-09\tmp_cookies"
WS_DIR = r"C:\Users\pc\WorkBuddy\2026-07-29-09-35-09\logistics-dashboard"
OUT_JSON = os.path.join(WS_DIR, "data.json")
os.makedirs(TMP, exist_ok=True)

GITHUB_REPO = os.environ.get("GITHUB_REPO", "kamlshi/logistics-dashboard")
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

# 表头关键词 → 看板字段（按顺序优先匹配，确保稳定）
HEADER_MAP = [
    ("物流状态", "status"),
    ("合同号", "contractNo"),
    ("提单", "blNo"),
    ("启运港", "shipper"),
    ("目的港", "destination"),
    ("中转港", "transitPort"),
    ("承运", "carrier"),
    ("船公司", "shipCompany"),
    ("电放", "type"),
    ("运输方式", "transportMode"),
    ("ETA", "eta"),          # ETA（中转港）/ ETA（目的港）都先映射到 eta/etaDest，下面再细分
    ("柜量", "containerCount"),
    ("柜型", "containerType"),
    ("件数", "packages"),
    ("净重", "netWeight"),
    ("毛重", "grossWeight"),
    ("货名", "goodsName"),
    ("数量", "quantity"),
    ("单位", "unit"),
    ("单价", "unitPrice"),
    ("总价", "totalPrice"),
    ("报告编号", "reportNo"),
    ("船名航次", "vessel"),
    ("预计放行", "estRelease"),
    ("实际放行", "actualRelease"),
    ("水单到账", "paymentDate"),
    ("放单", "releaseTime"),
    ("客户名", "customerName"),
    ("注意事项", "notes"),
    ("备注", "notes"),
]
STATUS_EN = {
    "运输中": "InTransit", "在途": "InTransit", "已装船": "Shipped", "已发船": "Shipped",
    "已交付": "Delivered", "已完成": "Delivered", "待发货": "Pending", "已收货": "Received",
    "延期": "Delayed", "延误": "Delayed",
}

# ---------- 1. 解密 cookie ----------
def decrypt_cookies():
    COOKIE_DB = os.path.join(PROFILE_SRC, "Network", "Cookies")
    USER_DATA = os.path.dirname(PROFILE_SRC)
    shutil.copy2(COOKIE_DB, os.path.join(TMP, "edge_cookies.db"))
    shutil.copy2(os.path.join(USER_DATA, "Local State"), os.path.join(TMP, "edge_localstate.json"))
    crypt32 = ctypes.windll.crypt32; kernel32 = ctypes.windll.kernel32
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
    def dpapi(data):
        inp = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_byte)))
        out = DATA_BLOB()
        if crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)) == 0:
            raise ctypes.WinError()
        buf = ctypes.string_at(out.pbData, out.cbData); kernel32.LocalFree(out.pbData); return buf
    key = dpapi(base64.b64decode(json.load(open(os.path.join(TMP, "edge_localstate.json"), encoding="utf-8"))["os_crypt"]["encrypted_key"])[5:])
    def dec(enc):
        if enc and enc[:3] in (b"v10", b"v11"):
            pt = AESGCM(key).decrypt(enc[3:15], enc[15:], None)
            if len(pt) > 32: pt = pt[32:]
            return pt.decode("utf-8", "replace")
        if enc:
            try: return dpapi(enc).decode("utf-8", "replace")
            except Exception: return ""
        return ""
    conn = sqlite3.connect(os.path.join(TMP, "edge_cookies.db")); cur = conn.cursor()
    cur.execute("SELECT name, value, host_key, encrypted_value, path, expires_utc, is_secure, is_httponly FROM cookies WHERE host_key LIKE '%qq.com%'")
    out = []
    for name, value, host, enc, path, expires, is_secure, is_httponly in cur.fetchall():
        val = dec(enc) if (enc and len(enc) > 0) else (value or "")
        if not val: continue
        c = {"name": name, "value": val, "domain": host, "path": path or "/", "secure": bool(is_secure), "httpOnly": bool(is_httponly)}
        if expires and expires > 0: c["expires"] = expires / 1_000_000 - 11644473600
        out.append(c)
    conn.close()
    return out

# ---------- 2+3. Playwright 读取表格 ----------
def extract_grid(pw_cookies):
    grid_js = r"""
    () => {
      function cellVal(cd) {
        if (cd == null) return '';
        if (typeof cd !== 'object') return cd;
        if (cd.value !== undefined && cd.value !== '' && cd.value !== null) return cd.value;
        if (cd.formulaResult && cd.formulaResult.value !== undefined && cd.formulaResult.value !== '' && cd.formulaResult.value !== null) return cd.formulaResult.value;
        if (cd.displayValue !== undefined && cd.displayValue !== '') return cd.displayValue;
        if (cd.text !== undefined) return cd.text;
        return '';
      }
      const sh = window.SpreadsheetApp.workbook.activeSheet;
      const rows = sh.getRowCount();
      const cols = sh.getColCount();
      const out = [];
      for (let r = 0; r < rows; r++) {
        const row = [];
        for (let c = 0; c < cols; c++) {
          try { row.push(cellVal(sh.getCellDataAtPosition(r, c))); }
          catch(e) { row.push(''); }
        }
        out.push(row);
      }
      return out;
    }
    """
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=os.path.join(TMP, "pw_profile"), executable_path=CHROME, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-software-rasterizer"])
        ctx.add_cookies(pw_cookies)
        page = ctx.new_page()
        page.goto(DOC_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(12)  # 等待 SpreadsheetApp 初始化并载入数据
        grid = page.evaluate(grid_js)
        ctx.close()
    return grid

# ---------- 4. 映射到看板字段 ----------
def build_rows(grid):
    if not grid:
        return [], []
    header = [str(h).strip() if h is not None else "" for h in grid[0]]
    # 建立 列索引 → 看板字段
    col2field = {}
    for i, h in enumerate(header):
        if not h:
            continue
        for kw, field in HEADER_MAP:
            if kw in h:
                # ETA 细分
                if field == "eta":
                    field = "etaDest" if "目的" in h else "eta"
                col2field[i] = field
                break
    rows = []
    for r in grid[1:]:
        if not any(str(c).strip() for c in r):
            continue  # 跳过空行
        d = {k: "" for k in
             ["id","status","contractNo","blNo","shipper","destination","transitPort","carrier",
              "shipCompany","type","transportMode","eta","etaDest","containerCount","containerType",
              "packages","netWeight","grossWeight","goodsName","quantity","unit","unitPrice","totalPrice",
              "customerName","reportNo","vessel","estRelease","actualRelease","paymentDate","releaseTime","notes"]}
        for i, field in col2field.items():
            if i < len(r):
                v = r[i]
                if v is None or v == "":
                    v = ""
                if field in ("containerCount","containerType","packages","quantity","netWeight","grossWeight","unitPrice","totalPrice"):
                    try: v = float(v) if v not in ("", None) else 0
                    except Exception: v = str(v)
                else:
                    v = str(v)
                d[field] = v
        # id + 状态英文映射
        d["id"] = d.get("contractNo") or d.get("blNo") or f"ROW-{len(rows)+1}"
        raw = str(d.get("status", ""))
        d["status"] = STATUS_EN.get(raw, raw)
        rows.append(d)
    return header, rows

# ---------- 5. 推送 GitHub ----------
def push_github(data_str):
    if not GITHUB_PAT:
        print("[github] 未配置 PAT，跳过推送")
        return False
    api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/data.json?ref={GITHUB_BRANCH}"
    headers = {"Authorization": f"token {GITHUB_PAT}", "Content-Type": "application/json", "User-Agent": "sync-bot"}
    resp = requests.get(api, headers=headers, timeout=15)
    sha = resp.json().get("sha") if resp.status_code == 200 else ""
    payload = {"message": f"auto sync {time.strftime('%Y-%m-%d %H:%M:%S')}",
               "content": base64.b64encode(data_str.encode("utf-8")).decode("utf-8"),
               "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha
    r = requests.put(api, headers=headers, json=payload, timeout=30)
    if r.status_code in (200, 201):
        print("[github] ✅ 推送成功")
        return True
    print(f"[github] ❌ 推送失败 {r.status_code}: {r.text[:200]}")
    return False

class _Tee:
    """同时输出到控制台和本地日志文件，便于定时任务排查。"""
    def __init__(self, *streams):
        self.streams = streams
    def write(self, s):
        for st in self.streams:
            try: st.write(s)
            except Exception: pass
    def flush(self):
        for st in self.streams:
            try: st.flush()
            except Exception: pass

def main():
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync.log")
    try:
        _lf = open(log_path, "a", encoding="utf-8")
    except Exception:
        _lf = None
    if _lf:
        _lf.write("\n===== RUN %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        sys.stdout = _Tee(sys.stdout, _lf)
        sys.stderr = _Tee(sys.stderr, _lf)
    try:
        _real_main()
    except Exception as e:
        print("FATAL:", repr(e))
        raise
    finally:
        if _lf:
            _lf.close()

def _real_main():
    print("=== 1) 解密 qclaw cookie ===")
    pw = decrypt_cookies()
    print(f"  {len(pw)} 个 qq.com cookie")
    print("=== 2) Playwright 读取表格 ===")
    grid = extract_grid(pw)
    print(f"  网格: {len(grid)} 行 × {len(grid[0]) if grid else 0} 列")
    header, rows = build_rows(grid)
    print(f"  表头: {header}")
    print(f"  数据行: {len(rows)}")
    data = {
        "lastUpdated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "tencent-docs-live",
        "rowCount": len(rows),
        "columns": header,
        "rows": rows,
    }
    data_str = json.dumps(data, ensure_ascii=False, indent=2)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        f.write(data_str)
    print(f"=== ✅ 已生成 {OUT_JSON} ({len(rows)} 行) ===")
    if rows:
        print("示例首行:", json.dumps(rows[0], ensure_ascii=False)[:400])
    push_github(data_str)

if __name__ == "__main__":
    main()
