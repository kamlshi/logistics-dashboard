/**
 * 物流看板 - Cloudflare Worker 中转 API
 * 前端调用 /api/sync → Worker 实时拉取腾讯文档最新数据 → 返回 JSON
 *
 * 环境变量（在 Cloudflare Worker Settings → Variables 中配置）:
 *   TENCENT_COOKIE    - 腾讯文档登录 Cookie（TOK=xxx; DOC_SID=yyy）
 *   TENCENT_SHEET_ID  - 文档 ID，如 DWk1ESWh0VFJKUGlI
 *   SHEET_TAB_NAME    - 工作表名称（可选）
 *   ALLOW_ORIGIN      - 允许的前端域名（CORS），如 * 或 https://logistics-zhanzhu.surge.sh
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

// 数值型字段（自动转数字）
const NUMERIC_FIELDS = new Set([
  'containerCount', 'packages', 'netWeight', 'grossWeight',
  'quantity', 'unitPrice', 'totalPrice'
]);

const EXPORT_API = 'https://docs.qq.com/v1/export/async_export';
const QUERY_API = 'https://docs.qq.com/v1/export/query_progress';

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // CORS 预检
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: corsHeaders(env),
      });
    }

    // 健康检查
    if (url.pathname === '/' || url.pathname === '/health') {
      return json({ status: 'ok', time: new Date().toISOString() }, env);
    }

    // 同步接口
    if (url.pathname === '/api/sync' && request.method === 'GET') {
      try {
        const data = await fetchLatestData(env);
        return json({
          orders: data,
          updatedAt: new Date().toISOString(),
          count: data.length,
        }, env);
      } catch (e) {
        console.error('Sync error:', e.message);
        return json({ error: e.message }, env, 500);
      }
    }

    return json({ error: 'Not Found' }, env, 404);
  },
};

// ===== 核心：拉取腾讯文档数据 =====
async function fetchLatestData(env) {
  const sheetId = env.TENCENT_SHEET_ID;
  const cookie = env.TENCENT_COOKIE;
  const tabName = env.SHEET_TAB_NAME || '';

  if (!sheetId || !cookie) {
    throw new Error('缺少环境变量 TENCENT_SHEET_ID 或 TENCENT_COOKIE');
  }

  const headers = {
    'Cookie': cookie,
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': `https://docs.qq.com/sheet/D${sheetId}`,
    'Accept': 'application/json, text/plain, */*',
    'Content-Type': 'application/x-www-form-urlencoded',
  };

  // 1. 创建导出任务
  const exportBody = new URLSearchParams({
    docId: sheetId,
    exportType: 'csv',
    sheetId: tabName,
  });

  const exportResp = await fetch(EXPORT_API, {
    method: 'POST',
    headers,
    body: exportBody.toString(),
  });

  const exportResult = await exportResp.json();
  if (exportResult.ret !== 0) {
    throw new Error(`导出失败: ${exportResult.msg || JSON.stringify(exportResult)}`);
  }

  const operationId = exportResult.operationID;

  // 2. 轮询导出进度（最多 30 秒）
  let downloadUrl = null;
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 1000));

    const queryResp = await fetch(
      `${QUERY_API}?operationID=${encodeURIComponent(operationId)}`,
      { headers }
    );
    const q = await queryResp.json();

    if (q.status === 'success' || q.download_url) {
      downloadUrl = q.download_url || (q.result && q.result.download_url);
      break;
    }
    if (q.status === 'failed') {
      throw new Error(`导出任务失败: ${q.msg}`);
    }
  }

  if (!downloadUrl) {
    throw new Error('导出超时，未获取到下载链接');
  }

  // 3. 下载 CSV
  const csvResp = await fetch(downloadUrl);
  const csvText = await csvResp.text();

  // 4. 解析 CSV + 字段映射
  return parseAndMapCSV(csvText);
}

// ===== CSV 解析 + 字段映射 =====
function parseAndMapCSV(csvText) {
  const rows = parseCSV(csvText);
  if (rows.length < 2) return [];

  const headers = rows[0];
  const dataRows = rows.slice(1);

  return dataRows.map((row, idx) => {
    const obj = { id: `LOG-${String(idx + 1).padStart(3, '0')}` };
    headers.forEach((header, colIdx) => {
      const field = HEADER_MAP[header.trim()] || header.trim();
      let value = row[colIdx] || '';

      // 数值型字段转数字
      if (NUMERIC_FIELDS.has(field)) {
        const num = parseFloat(value);
        value = isNaN(num) ? 0 : num;
      }

      obj[field] = value;
    });
    return obj;
  }).filter(obj => {
    // 过滤掉完全空的行
    return Object.values(obj).some(v => v !== '' && v !== 0);
  });
}

// ===== 简易 CSV 解析（支持引号包裹、逗号转义） =====
function parseCSV(text) {
  const rows = [];
  let curRow = [];
  let curVal = '';
  let inQuotes = false;

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];

    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          curVal += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        curVal += ch;
      }
    } else {
      if (ch === '"') {
        inQuotes = true;
      } else if (ch === ',') {
        curRow.push(curVal);
        curVal = '';
      } else if (ch === '\n') {
        curRow.push(curVal);
        rows.push(curRow);
        curRow = [];
        curVal = '';
      } else if (ch === '\r') {
        // skip
      } else {
        curVal += ch;
      }
    }
  }

  // 最后一行
  if (curVal || curRow.length) {
    curRow.push(curVal);
    rows.push(curRow);
  }

  return rows;
}

// ===== 工具函数 =====
function json(data, env, status = 200) {
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
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
  };
}
