/**
 * Vercel Edge Function - 物流看板实时同步接口
 * 部署后访问路径：/api/sync
 * 使用 Edge Runtime，无 10 秒超时限制
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

export default async function handler(req) {
  const origin = process.env.ALLOW_ORIGIN || '*';
  const corsHeaders = {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
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
  const cookie = process.env.TENCENT_COOKIE;
  const tabName = process.env.SHEET_TAB_NAME || '';

  if (!sheetId || !cookie) throw new Error('缺少环境变量 TENCENT_SHEET_ID 或 TENCENT_COOKIE');

  const headers = {
    'Cookie': cookie,
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': `https://docs.qq.com/sheet/D${sheetId}`,
    'Content-Type': 'application/x-www-form-urlencoded',
  };

  // 1. 创建导出任务
  const exportBody = new URLSearchParams({ docId: sheetId, exportType: 'csv', sheetId: tabName });
  const exportResp = await fetch(EXPORT_API, { method: 'POST', headers, body: exportBody.toString() });
  const exportResult = await exportResp.json();
  if (exportResult.ret !== 0) throw new Error(`导出失败: ${exportResult.msg || JSON.stringify(exportResult)}`);

  const operationId = exportResult.operationID;

  // 2. 轮询导出进度（最多约24秒）
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

  // 4. 解析 + 映射
  return parseAndMapCSV(csvText);
}

function parseAndMapCSV(csvText) {
  const rows = parseCSV(csvText);
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
