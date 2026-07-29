/**
 * 物流看板 - Cloudflare Worker 中转 API
 * 前端调用 /api/sync → Worker 实时拉取腾讯文档最新数据 → 返回 JSON
 *
 * 支持两种模式:
 *   公开模式：文档设置为「任何人可查看」，无需 Cookie（优先）
 *   私密模式：使用 Cookie 鉴权（回退）
 *
 * 环境变量（在 Cloudflare Worker Settings → Variables 中配置）:
 *   TENCENT_SHEET_ID  - 文档 ID，如 DWk1ESWh0VFJKUGlI（必需）
 *   TENCENT_COOKIE    - 腾讯文档登录 Cookie（可选，私密模式需要）
 *   SHEET_TAB_NAME    - 工作表名称或 tab ID（可选）
 *   ALLOW_ORIGIN      - 允许的前端域名（CORS），如 * 或 https://kamlshi.github.io
 */

// ===== 中文表头 → 英文字段名映射 =====
const HEADER_MAP = {
  '物流状态': 'status',
  '合同号': 'contractNo',
  '提单号(BL#)': 'blNo',
  '启运港': 'shipper',
  '目的港': 'destination',
  '中转港': 'transitPort',
  '承运商': 'carrier',
  '船公司': 'shipCompany',
  '电放/正本类型': 'type',
  '运输方式': 'transportMode',
  'ETA（中转港）': 'eta',
  'ETA（目的港）': 'etaDest',
  '柜量': 'containerCount',
  '柜型': 'containerType',
  '件数': 'packages',
  '净重': 'netWeight',
  '毛重': 'grossWeight',
  '货名': 'goodsName',
  '数量': 'quantity',
  '单位': 'unit',
  '单价': 'unitPrice',
  '总价': 'totalPrice',
  '客户名称': 'customerName',
  '检测报告编号': 'reportNo',
  '实时轨迹（船名航次）': 'vessel',
  '预计放行时间': 'estRelease',
  '实际放行时间': 'actualRelease',
  '客户水单到账日期': 'paymentDate',
  '放单/电放执行时间': 'releaseTime',
};

const NUMERIC_FIELDS = new Set([
  'containerCount', 'packages', 'netWeight', 'grossWeight',
  'quantity', 'unitPrice', 'totalPrice'
]);

const EXPORT_API = 'https://docs.qq.com/v1/export/async_export';
const QUERY_API = 'https://docs.qq.com/v1/export/query_progress';
const OPENDOC_API = 'https://docs.qq.com/dop-api/opendoc';

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // CORS 预检
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders(env) });
    }

    // 健康检查
    if (url.pathname === '/' || url.pathname === '/health') {
      return jsonResponse({ status: 'ok', time: new Date().toISOString() }, env);
    }

    // 同步接口
    if (url.pathname === '/api/sync' && request.method === 'GET') {
      try {
        const data = await fetchLatestData(env);
        return jsonResponse({
          orders: data,
          updatedAt: new Date().toISOString(),
          count: data.length,
        }, env);
      } catch (e) {
        console.error('Sync error:', e.message);
        return jsonResponse({ error: e.message }, env, 500);
      }
    }

    return jsonResponse({ error: 'Not Found' }, env, 404);
  },
};

async function fetchLatestData(env) {
  const sheetId = env.TENCENT_SHEET_ID;
  const cookie = env.TENCENT_COOKIE || '';
  const tabName = env.SHEET_TAB_NAME || '';

  if (!sheetId) throw new Error('缺少环境变量 TENCENT_SHEET_ID');

  // 优先尝试公开模式
  let rows = null;
  try {
    rows = await fetchPublicData(sheetId, tabName, env);
  } catch (e) {
    console.warn('公开模式失败:', e.message);
  }

  // 回退到 Cookie 模式
  if (!rows && cookie) {
    try {
      rows = await fetchPrivateData(sheetId, cookie, tabName);
    } catch (e) {
      console.error('Cookie模式失败:', e.message);
    }
  }

  if (!rows) throw new Error('公开模式和 Cookie 模式均失败');

  return mapRows(rows);
}

// ===== 公开模式 =====
async function fetchPublicData(sheetId, tabName, env) {
  const params = new URLSearchParams({ id: sheetId, outformat: '1', normal: '1' });

  // 发现 tab ID
  let effectiveTab = tabName;
  if (!effectiveTab) {
    try {
      effectiveTab = await discoverTabId(sheetId);
    } catch {}
  }
  if (effectiveTab) params.set('tab', effectiveTab);

  const url = `${OPENDOC_API}?${params.toString()}`;
  const resp = await fetch(url, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      'Referer': `https://docs.qq.com/sheet/D${sheetId}`,
    },
  });

  if (!resp.ok) throw new Error(`opendoc 返回 ${resp.status}`);

  const data = await resp.json();
  return parseOpendocResponse(data);
}

async function discoverTabId(sheetId) {
  const pageUrl = `https://docs.qq.com/sheet/D${sheetId}`;
  const resp = await fetch(pageUrl, {
    headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' },
  });
  if (!resp.ok) return '';
  const html = await resp.text();

  for (const p of [
    /\?tab=([A-Za-z0-9]+)/,
    /["']tab["']:\s*["']([A-Za-z0-9]+)["']/,
    /tabId["']?\s*[=:]\s*["']([A-Za-z0-9]+)["']/,
  ]) {
    const m = html.match(p);
    if (m) return m[1];
  }
  return '';
}

function parseOpendocResponse(data) {
  const cv = data.clientVars || data.client_vars || {};
  const ccv = cv.collab_client_vars || cv.collabClientVars || {};
  const iat = ccv.initialAttributedText || ccv.initialAttributedTextStr || {};

  let textData = iat.text || iat.textStr || [];
  if (typeof textData === 'string') {
    try { textData = JSON.parse(textData); } catch { throw new Error('text JSON 解析失败'); }
  }

  if (!Array.isArray(textData) || !textData.length) throw new Error('opendoc 数据为空');

  // 提取维度
  let rowsCount = 0, colsCount = 0;
  const first = textData[0];
  if (Array.isArray(first)) {
    for (const item of first) {
      if (Array.isArray(item)) {
        for (const sub of item) {
          if (Array.isArray(sub) && sub.length >= 5 && typeof sub[2] === 'number' && typeof sub[4] === 'number') {
            rowsCount = sub[2]; colsCount = sub[4];
          }
        }
      }
    }
  }

  // 提取单元格数据
  let cellData = {};
  for (let i = 1; i < Math.min(textData.length, 4); i++) {
    const elem = textData[i];
    if (elem && typeof elem === 'object' && !Array.isArray(elem)) {
      const intKeys = Object.keys(elem).filter(k => /^\d+$/.test(k));
      if (intKeys.length > 10) { cellData = elem; break; }
    }
  }

  if (!Object.keys(cellData).length) throw new Error('未找到单元格数据');

  if (!rowsCount || !colsCount) {
    const maxPos = Math.max(...Object.keys(cellData).filter(k => /^\d+$/.test(k)).map(Number));
    if (maxPos > 0) {
      const sortedKeys = Object.keys(cellData).filter(k => /^\d+$/.test(k)).map(Number).sort((a,b) => a-b);
      for (let i = 1; i < Math.min(sortedKeys.length, 30); i++) {
        if (sortedKeys[i] - sortedKeys[i-1] > 1) { colsCount = sortedKeys[i]; break; }
      }
      if (!colsCount) colsCount = Math.min(maxPos + 1, 50);
      rowsCount = Math.ceil((maxPos + 1) / colsCount);
    }
  }

  if (!rowsCount || !colsCount) throw new Error('无法确定表格维度');

  const table = [];
  for (let r = 0; r < rowsCount; r++) {
    const row = [];
    for (let c = 0; c < colsCount; c++) {
      const key = String(r * colsCount + c);
      row.push(extractCell(cellData[key] || ''));
    }
    if (row.some(v => String(v).trim())) table.push(row);
  }

  if (table.length < 2) throw new Error('表格数据不足');
  return table;
}

function extractCell(cell) {
  if (typeof cell === 'string') return cell;
  if (Array.isArray(cell)) {
    for (const path of [[2,1],[1],[0]]) {
      try { let v = cell; for (const i of path) v = v[i]; if (typeof v === 'string') return v; if (typeof v === 'number') return String(v); } catch {}
    }
    return findStr(cell);
  }
  if (typeof cell === 'object' && cell !== null) {
    for (const k of ['v','value','text','content','s']) { const v = cell[k]; if (typeof v === 'string' && v.trim()) return v; }
    return findStrObj(cell);
  }
  return String(cell ?? '');
}

function findStr(arr, d=0) {
  if (d > 4) return '';
  for (const i of arr) { if (typeof i === 'string' && i.trim()) return i; if (Array.isArray(i)) { const r = findStr(i, d+1); if (r) return r; } }
  return '';
}

function findStrObj(obj, d=0) {
  if (d > 4) return '';
  for (const v of Object.values(obj)) { if (typeof v === 'string' && v.trim()) return v; if (typeof v === 'object' && v) { const r = Array.isArray(v) ? findStr(v,d+1) : findStrObj(v,d+1); if (r) return r; } }
  return '';
}

// ===== Cookie 模式 =====
async function fetchPrivateData(sheetId, cookie, tabName) {
  const headers = {
    'Cookie': cookie,
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': `https://docs.qq.com/sheet/D${sheetId}`,
    'Content-Type': 'application/x-www-form-urlencoded',
  };

  const exportBody = new URLSearchParams({ docId: sheetId, exportType: 'csv', sheetId: tabName });
  const exportResp = await fetch(EXPORT_API, { method: 'POST', headers, body: exportBody.toString() });
  const exportResult = await exportResp.json();
  if (exportResult.ret !== 0) throw new Error(`导出失败: ${exportResult.msg}`);

  const operationId = exportResult.operationID;

  let downloadUrl = null;
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 1000));
    const queryResp = await fetch(`${QUERY_API}?operationID=${encodeURIComponent(operationId)}`, { headers });
    const q = await queryResp.json();
    if (q.status === 'success' || q.download_url) { downloadUrl = q.download_url || (q.result && q.result.download_url); break; }
    if (q.status === 'failed') throw new Error(`导出任务失败: ${q.msg}`);
  }

  if (!downloadUrl) throw new Error('导出超时');

  const csvResp = await fetch(downloadUrl);
  const csvText = await csvResp.text();
  return parseAndMapCSV(csvText);
}

function parseAndMapCSV(csvText) {
  const rows = parseCSV(csvText);
  return rows.length >= 2 ? rows : [];
}

function parseCSV(text) {
  const rows = []; let curRow = []; let curVal = ''; let inQ = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQ) {
      if (ch === '"') { if (text[i+1] === '"') { curVal += '"'; i++; } else { inQ = false; } }
      else curVal += ch;
    } else {
      if (ch === '"') inQ = true;
      else if (ch === ',') { curRow.push(curVal); curVal = ''; }
      else if (ch === '\n') { curRow.push(curVal); rows.push(curRow); curRow = []; curVal = ''; }
      else if (ch !== '\r') curVal += ch;
    }
  }
  if (curVal || curRow.length) { curRow.push(curVal); rows.push(curRow); }
  return rows;
}

// ===== 字段映射 =====
function mapRows(rows) {
  if (rows.length < 2) return [];
  const headers = rows[0];
  return rows.slice(1).map((row, idx) => {
    const obj = { id: `LOG-${String(idx + 1).padStart(3, '0')}` };
    headers.forEach((header, colIdx) => {
      const field = HEADER_MAP[header.trim()] || header.trim();
      let value = row[colIdx] || '';
      if (NUMERIC_FIELDS.has(field)) { const num = parseFloat(value); value = isNaN(num) ? 0 : num; }
      obj[field] = value;
    });
    return obj;
  }).filter(obj => Object.values(obj).some(v => v !== '' && v !== 0));
}

// ===== 工具函数 =====
function jsonResponse(data, env, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      ...corsHeaders(env),
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store, no-cache, must-revalidate',
    },
  });
}

function corsHeaders(env) {
  const origin = env.ALLOW_ORIGIN || '*';
  return {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
  };
}
