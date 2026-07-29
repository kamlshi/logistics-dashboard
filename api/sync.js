/**
 * Vercel Edge Function - 物流看板实时同步接口
 * 部署后访问路径：/api/sync
 * 使用 Edge Runtime，无 10 秒超时限制
 *
 * 支持两种模式:
 *   公开模式：文档设置为「任何人可查看」，无需 Cookie
 *   私密模式：使用 Cookie 鉴权（TENCENT_COOKIE 环境变量）
 */

export const config = {
  runtime: 'edge',
};

const HEADER_MAP = {
  '物流状态': 'status', '合同号': 'contractNo', '提单号(BL#)': 'blNo',
  '启运港': 'shipper', '目的港': 'destination', '中转港': 'transitPort',
  '承运商': 'carrier', '船公司': 'shipCompany', '电放/正本类型': 'type',
  '运输方式': 'transportMode', 'ETA（中转港）': 'eta', 'ETA（目的港）': 'etaDest',
  '柜量': 'containerCount', '柜型': 'containerType', '件数': 'packages',
  '净重': 'netWeight', '毛重': 'grossWeight', '货名': 'goodsName',
  '数量': 'quantity', '单位': 'unit', '单价': 'unitPrice', '总价': 'totalPrice',
  '客户名称': 'customerName', '检测报告编号': 'reportNo',
  '实时轨迹（船名航次）': 'vessel', '预计放行时间': 'estRelease',
  '实际放行时间': 'actualRelease', '客户水单到账日期': 'paymentDate',
  '放单/电放执行时间': 'releaseTime',
};

const NUMERIC_FIELDS = new Set(['containerCount','packages','netWeight','grossWeight','quantity','unitPrice','totalPrice']);
const EXPORT_API = 'https://docs.qq.com/v1/export/async_export';
const QUERY_API = 'https://docs.qq.com/v1/export/query_progress';
const OPENDOC_API = 'https://docs.qq.com/dop-api/opendoc';

export default async function handler(req) {
  const origin = process.env.ALLOW_ORIGIN || '*';
  const corsHeaders = {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Headers': 'Content-Type',
    'Cache-Control': 'no-store',
  };

  if (req.method === 'OPTIONS') {
    return new Response(null, { status: 200, headers: corsHeaders });
  }

  if (req.method !== 'GET') {
    return new Response(JSON.stringify({ error: 'Method Not Allowed' }), {
      status: 405,
      headers: { ...corsHeaders, 'Content-Type': 'application/json' },
    });
  }

  try {
    const data = await fetchLatestData();
    return new Response(
      JSON.stringify({ orders: data, updatedAt: new Date().toISOString(), count: data.length }),
      { status: 200, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
    );
  } catch (e) {
    return new Response(
      JSON.stringify({ error: e.message }),
      { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
    );
  }
}

async function fetchLatestData() {
  const sheetId = process.env.TENCENT_SHEET_ID;
  const cookie = process.env.TENCENT_COOKIE || '';
  const tabName = process.env.SHEET_TAB_NAME || '';

  if (!sheetId) throw new Error('缺少环境变量 TENCENT_SHEET_ID');

  // 优先尝试公开模式 (dop-api/opendoc)
  let rows = null;
  try {
    rows = await fetchPublicData(sheetId, tabName);
  } catch (e) {
    console.warn('公开模式失败:', e.message);
  }

  // 回退到 Cookie 模式
  if (!rows && cookie) {
    rows = await fetchPrivateData(sheetId, cookie, tabName);
  }

  if (!rows) throw new Error('公开模式和 Cookie 模式均失败');

  return mapRows(rows);
}

// ===== 公开模式 =====
async function fetchPublicData(sheetId, tabName) {
  const params = new URLSearchParams({
    id: sheetId,
    outformat: '1',
    normal: '1',
  });
  if (tabName) params.set('tab', tabName);

  // 先尝试发现 tab ID（如果没提供）
  if (!tabName) {
    try {
      const discoveredTab = await discoverTabId(sheetId);
      if (discoveredTab) params.set('tab', discoveredTab);
    } catch {}
  }

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

  const patterns = [
    /\?tab=([A-Za-z0-9]+)/,
    /["']tab["']:\s*["']([A-Za-z0-9]+)["']/,
    /tabId["']?\s*[=:]\s*["']([A-Za-z0-9]+)["']/,
  ];
  for (const p of patterns) {
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

  if (!textData || !Array.isArray(textData)) throw new Error('opendoc text 数据为空');

  // 提取维度
  let rowsCount = 0, colsCount = 0;
  const first = textData[0];
  if (Array.isArray(first)) {
    for (const item of first) {
      if (Array.isArray(item)) {
        for (const sub of item) {
          if (Array.isArray(sub) && sub.length >= 5 && typeof sub[2] === 'number' && typeof sub[4] === 'number') {
            rowsCount = sub[2];
            colsCount = sub[4];
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

  // 推算维度（如果没找到）
  if (!rowsCount || !colsCount) {
    const maxPos = Math.max(...Object.keys(cellData).filter(k => /^\d+$/.test(k)).map(Number));
    if (maxPos > 0) {
      const sortedKeys = Object.keys(cellData).filter(k => /^\d+$/.test(k)).map(Number).sort((a,b)=>a-b);
      // 推算列数
      for (let i = 1; i < Math.min(sortedKeys.length, 30); i++) {
        if (sortedKeys[i] - sortedKeys[i-1] > 1) {
          colsCount = sortedKeys[i];
          break;
        }
      }
      if (!colsCount) colsCount = Math.min(maxPos + 1, 50);
      rowsCount = Math.ceil((maxPos + 1) / colsCount);
    }
  }

  if (!rowsCount || !colsCount) throw new Error('无法确定表格维度');

  // 构建表格
  const table = [];
  for (let r = 0; r < rowsCount; r++) {
    const row = [];
    for (let c = 0; c < colsCount; c++) {
      const key = String(r * colsCount + c);
      row.push(extractCellValue(cellData[key] || ''));
    }
    // 去掉全空行
    if (row.some(v => v.trim())) table.push(row);
  }

  if (table.length < 2) throw new Error('表格数据不足');
  return table;
}

function extractCellValue(cell) {
  if (typeof cell === 'string') return cell;
  if (Array.isArray(cell)) {
    // 尝试 [2][1] → [1] → [0]
    for (const path of [[2,1],[1],[0]]) {
      try {
        let val = cell;
        for (const idx of path) val = val[idx];
        if (typeof val === 'string') return val;
        if (typeof val === 'number') return String(val);
      } catch {}
    }
    // 搜索字符串
    return findString(cell);
  }
  if (typeof cell === 'object') {
    for (const key of ['v','value','text','content','s']) {
      const val = cell[key];
      if (typeof val === 'string' && val.trim()) return val;
    }
    return findStringObj(cell);
  }
  return String(cell ?? '');
}

function findString(arr, depth=0) {
  if (depth > 4) return '';
  for (const item of arr) {
    if (typeof item === 'string' && item.trim()) return item;
    if (Array.isArray(item)) { const r = findString(item, depth+1); if (r) return r; }
  }
  return '';
}

function findStringObj(obj, depth=0) {
  if (depth > 4) return '';
  for (const val of Object.values(obj)) {
    if (typeof val === 'string' && val.trim()) return val;
    if (typeof val === 'object' && val !== null) {
      const r = Array.isArray(val) ? findString(val, depth+1) : findStringObj(val, depth+1);
      if (r) return r;
    }
  }
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

  // 1. 创建导出任务
  const exportBody = new URLSearchParams({ docId: sheetId, exportType: 'csv', sheetId: tabName || '' });
  const exportResp = await fetch(EXPORT_API, { method: 'POST', headers, body: exportBody.toString() });
  const exportResult = await exportResp.json();
  if (exportResult.ret !== 0) throw new Error(`导出失败: ${exportResult.msg || JSON.stringify(exportResult)}`);

  const operationId = exportResult.operationID;

  // 2. 轮询（最多约24秒）
  let downloadUrl = null;
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 800));
    const queryResp = await fetch(`${QUERY_API}?operationID=${encodeURIComponent(operationId)}`, { headers });
    const q = await queryResp.json();
    if (q.status === 'success' || q.download_url) {
      downloadUrl = q.download_url || (q.result && q.result.download_url);
      break;
    }
    if (q.status === 'failed') throw new Error(`导出任务失败: ${q.msg}`);
  }
  if (!downloadUrl) throw new Error('导出超时');

  // 3. 下载 CSV
  const csvResp = await fetch(downloadUrl);
  const csvText = await csvResp.text();

  // 4. 解析 CSV
  return parseCSVText(csvText);
}

function parseCSVText(text) {
  const rows = parseCSV(text);
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
    headers.forEach((h, i) => {
      const field = HEADER_MAP[h.trim()] || h.trim();
      let v = row[i] || '';
      if (NUMERIC_FIELDS.has(field)) { const n = parseFloat(v); v = isNaN(n) ? 0 : n; }
      obj[field] = v;
    });
    return obj;
  }).filter(obj => Object.values(obj).some(v => v !== '' && v !== 0));
}
