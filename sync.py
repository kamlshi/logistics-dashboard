#!/usr/bin/env python3
"""
物流进度看板 - 自动同步脚本
从腾讯文档拉取最新数据 → 嵌入 HTML → 提交到 GitHub（由 Actions 调用）

环境变量配置（在 GitHub Secrets 中设置）:
  TENCENT_COOKIE    - 腾讯文档登录 Cookie（包含 TOK 和 DOC_SID）
  TENCENT_SHEET_ID  - 腾讯文档表格 ID，如 DWk1ESWh0VFJKUGlI
  SHEET_TAB_NAME    - 要导出的工作表名称（可选，默认第一个）
"""

import urllib.request
import urllib.error
import urllib.parse
import json
import csv
import io
import os
import re
import time
import sys
from datetime import datetime

# ===== 配置 =====
SHEET_ID = os.environ.get('TENCENT_SHEET_ID', 'DWk1ESWh0VFJKUGlI')
COOKIE = os.environ.get('TENCENT_COOKIE', '')
TAB_NAME = os.environ.get('SHEET_TAB_NAME', '')
HTML_FILE = 'index.html'

# ===== 字段映射：中文表头 → 英文字段名 =====
HEADER_MAP = {
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
}

NUMERIC_FIELDS = {
    'containerCount', 'packages', 'netWeight', 'grossWeight',
    'quantity', 'unitPrice', 'totalPrice'
}

# 腾讯文档 API 端点
EXPORT_API = 'https://docs.qq.com/v1/export/async_export'
QUERY_API = 'https://docs.qq.com/v1/export/query_progress'


def make_headers():
    """构造请求头"""
    return {
        'Cookie': COOKIE,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': f'https://docs.qq.com/sheet/D{SHEET_ID}',
        'Accept': 'application/json, text/plain, */*',
    }


def http_get(url, headers=None):
    """发送 GET 请求"""
    req = urllib.request.Request(url, headers=headers or make_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        print(f'  HTTP {e.code}: {body[:200]}', file=sys.stderr)
        raise


def http_post(url, data, headers=None):
    """发送 POST 请求（form-data）"""
    body = urllib.parse.urlencode(data).encode()
    h = headers or make_headers()
    h['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(url, data=body, headers=h, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        print(f'  HTTP {e.code}: {body[:200]}', file=sys.stderr)
        raise


def export_sheet_to_csv():
    """
    导出腾讯文档表格为 CSV
    返回: CSV 文本内容
    """
    print(f'[1/4] 发起导出任务... (sheet: {SHEET_ID})')

    # 1. 创建导出任务
    export_data = {
        'docId': SHEET_ID,
        'exportType': 'csv',
        'sheetId': TAB_NAME or '',
    }
    resp = http_post(EXPORT_API, export_data)
    result = json.loads(resp)

    if result.get('ret') != 0:
        raise Exception(f'导出失败: {result.get("msg", resp)}')

    operation_id = result.get('operationID')
    print(f'  任务ID: {operation_id}')

    # 2. 轮询导出进度
    print('[2/4] 等待导出完成...')
    download_url = None
    for i in range(30):  # 最多等 30 秒
        time.sleep(1)
        qresp = http_get(f'{QUERY_API}?operationID={operation_id}')
        qresult = json.loads(qresp)

        progress = qresult.get('progress', 0)
        status = qresult.get('status', '')
        print(f'  进度: {progress}% (status={status})')

        if status == 'success' or qresult.get('download_url'):
            download_url = qresult.get('download_url') or qresult.get('result', {}).get('download_url')
            break
        elif status == 'failed':
            raise Exception(f'导出任务失败: {qresult.get("msg")}')

    if not download_url:
        raise Exception('导出超时，未获取到下载链接')

    # 3. 下载 CSV
    print('[3/4] 下载 CSV 数据...')
    csv_content = http_get(download_url, headers={})
    print(f'  CSV 大小: {len(csv_content)} 字节')

    return csv_content


def parse_csv(csv_text):
    """解析 CSV 为二维数组（列表的列表）"""
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    print(f'  解析到 {len(rows)} 行数据')
    return rows


def rows_to_objects(rows):
    """将 CSV 二维数组转换为带英文字段名的对象数组"""
    if len(rows) < 2:
        return []

    headers = [h.strip() for h in rows[0]]
    data_rows = rows[1:]
    result = []

    for idx, row in enumerate(data_rows):
        obj = {'id': f'LOG-{str(idx + 1).zfill(3)}'}
        for col_idx, header in enumerate(headers):
            field = HEADER_MAP.get(header, header)
            value = row[col_idx] if col_idx < len(row) else ''

            if field in NUMERIC_FIELDS:
                try:
                    value = float(value) if value else 0
                except ValueError:
                    value = 0

            obj[field] = value

        # 跳过全空的行
        has_data = any(v not in ('', 0, None) for k, v in obj.items() if k != 'id')
        if has_data:
            result.append(obj)

    return result


def embed_data_into_html(rows):
    """
    将数据嵌入 HTML 的 EMBEDDED_DATA 变量
    查找替换: const EMBEDDED_DATA = [...];
    """
    print('[4/4] 映射字段并嵌入 HTML...')

    with open(HTML_FILE, 'r', encoding='utf-8') as f:
        html = f.read()

    # 转成对象数组再序列化
    data_list = rows_to_objects(rows)
    data_json = json.dumps(data_list, ensure_ascii=False, indent=2)

    # 替换 EMBEDDED_DATA 变量
    pattern = r'(const|let|var)\s+EMBEDDED_DATA\s*=\s*\[.*?\];'
    replacement = rf'const EMBEDDED_DATA = {data_json};'

    new_html, count = re.subn(pattern, replacement, html, flags=re.DOTALL)

    if count == 0:
        # 如果没找到，尝试另一种格式
        pattern2 = r'EMBEDDED_DATA\s*=\s*\[.*?\];'
        new_html, count = re.subn(pattern2, replacement, html, flags=re.DOTALL)

    if count == 0:
        raise Exception(
            'HTML 中未找到 EMBEDDED_DATA 变量。\n'
            '请在 HTML 的 <script> 中添加一行:\n'
            '  const EMBEDDED_DATA = [];\n'
            '脚本会自动填充数据'
        )

    # 加上更新时间标记
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new_html = re.sub(
        r'(数据更新时间[:：]\s*)[^\n<]*',
        rf'\1{timestamp}',
        new_html
    )

    # 同步更新 lastFetchTime
    new_html = re.sub(
        r"(let\s+lastFetchTime\s*=\s*)'[^']*'",
        rf"\1'{timestamp}'",
        new_html
    )

    with open(HTML_FILE, 'w', encoding='utf-8') as f:
        f.write(new_html)

    print(f'  已更新 {HTML_FILE}，替换 {count} 处')
    print(f'  更新时间: {timestamp}')
    print(f'  有效数据行: {len(data_list)}')


def main():
    print('=' * 50)
    print('物流看板数据同步')
    print(f'时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 50)

    if not COOKIE:
        print('警告: 未设置 TENCENT_COOKIE 环境变量', file=sys.stderr)
        print('尝试公开导出方式...', file=sys.stderr)

    if not os.path.exists(HTML_FILE):
        raise Exception(f'HTML 文件不存在: {HTML_FILE}')

    # 导出 + 解析
    csv_text = export_sheet_to_csv()
    rows = parse_csv(csv_text)

    # 嵌入 HTML
    embed_data_into_html(rows)

    print('\n✅ 同步完成！')
    print(f'   HTML 文件: {HTML_FILE}')
    print(f'   数据行数: {len(rows)}')


if __name__ == '__main__':
    main()
