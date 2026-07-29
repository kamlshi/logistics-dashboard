#!/usr/bin/env python3
"""
sync.py — 腾讯文档自动同步脚本 (Cookie模式)
使用 Cookie 调用 async_export API 导出 xlsx，解析后生成 data.json

工作流程:
1. 用 Cookie 调用 async_export 创建导出任务
2. 等待导出完成
3. 下载 xlsx 文件
4. 用 openpyxl 解析单元格数据
5. 生成 data.json 推送到 GitHub

环境变量:
  COOKIE       - 腾讯文档 Cookie (从浏览器获取)
  DOC_ID       - 文档ID (默认: DWk1ESWh0VFJKUGlI)
  PAD_ID       - Pad ID (默认: ZMDIhtTRJPiH)
  GITHUB_PAT   - GitHub Personal Access Token
  GITHUB_REPO  - GitHub 仓库 (默认: kamlshi/logistics-dashboard)
"""

import os
import sys
import json
import time
import base64
import tempfile
import requests

# 配置
DOC_ID = os.environ.get('DOC_ID', 'DWk1ESWh0VFJKUGlI')
PAD_ID = os.environ.get('PAD_ID', 'ZMDIhtTRJPiH')
GITHUB_REPO = os.environ.get('GITHUB_REPO', 'kamlshi/logistics-dashboard')
GITHUB_PAT = os.environ.get('GITHUB_PAT', '')
COOKIE = os.environ.get('COOKIE', '')

BASE_URL = 'https://docs.qq.com'

# 状态映射
STATUS_MAP = {
    '运输中': 'InTransit',
    '已装船': 'Shipped',
    '已交付': 'Delivered',
    '待发货': 'Pending',
    '已收货': 'Received',
    '延期': 'Delayed',
}

def get_xsrf_from_cookie(cookie_str):
    """从 Cookie 字符串中提取 xsrf token"""
    for part in cookie_str.split(';'):
        part = part.strip()
        if part.startswith('xsrf='):
            return part.split('=')[1]
    return ''

def create_export_task(cookie_str, format='xlsx'):
    """创建导出任务"""
    xsrf = get_xsrf_from_cookie(cookie_str)
    headers = {
        'Cookie': cookie_str,
        'Content-Type': 'application/json',
        'X-Xsrf': xsrf,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': f'{BASE_URL}/sheet/{DOC_ID}',
    }

    # async_export API
    url = f'{BASE_URL}/api/v2/sheet/{DOC_ID}/async_export'
    payload = {
        'format': format,
        'sheetId': '000001',  # 第一个sheet
    }

    print(f'[1] Creating export task: {url}')
    resp = requests.post(url, headers=headers, json=payload, timeout=30)

    if resp.status_code != 200:
        # 尝试备用URL
        url2 = f'{BASE_URL}/dop-api/offline/export_async'
        payload2 = {
            'padId': PAD_ID,
            'type': 'xlsx',
            'format': 'xlsx',
        }
        resp = requests.post(url2, headers=headers, json=payload2, timeout=30)

    print(f'  Status: {resp.status_code}')
    result = resp.json()
    print(f'  Response: {json.dumps(result, ensure_ascii=False)[:200]}')

    return result

def wait_for_export(cookie_str, task_id):
    """等待导出完成"""
    xsrf = get_xsrf_from_cookie(cookie_str)
    headers = {
        'Cookie': cookie_str,
        'X-Xsrf': xsrf,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }

    max_wait = 30  # 最大等待30秒
    for i in range(max_wait):
        url = f'{BASE_URL}/api/v2/sheet/{DOC_ID}/async_export_progress?taskId={task_id}'
        resp = requests.get(url, headers=headers, timeout=15)
        result = resp.json()

        status = result.get('status', result.get('retcode', -1))
        print(f'  [{i+1}] Export progress: status={status}')

        if status == 2 or result.get('progress') == 100:
            # 导出完成
            download_url = result.get('downloadUrl', result.get('url', ''))
            if download_url:
                return download_url

        # 尝试备用接口
        url2 = f'{BASE_URL}/dop-api/offline/export_progress'
        params = {'taskId': task_id, 'padId': PAD_ID}
        resp2 = requests.get(url2, headers=headers, params=params, timeout=15)
        result2 = resp2.json()
        if result2.get('progress') == 100 or result2.get('downloadUrl'):
            return result2.get('downloadUrl', '')

        time.sleep(1)

    print('  Export timeout!')
    return None

def download_xlsx(download_url, cookie_str):
    """下载导出的 xlsx 文件"""
    headers = {
        'Cookie': cookie_str,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }

    print(f'[3] Downloading xlsx from: {download_url[:80]}...')
    resp = requests.get(download_url, headers=headers, timeout=30)

    if resp.status_code == 200:
        tmp_file = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
        tmp_file.write(resp.content)
        tmp_file.close()
        print(f'  Saved to: {tmp_file.name} ({len(resp.content)} bytes)')
        return tmp_file.name
    else:
        print(f'  Download failed: {resp.status_code}')
        return None

def parse_xlsx(xlsx_path):
    """解析 xlsx 文件并提取表格数据"""
    try:
        import openpyxl
    except ImportError:
        print('  openpyxl not installed, trying to install...')
        import subprocess
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'openpyxl', '--quiet'])
        import openpyxl

    print(f'[4] Parsing xlsx: {xlsx_path}')
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active

    # 读取所有行
    rows = []
    headers = None

    for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
        if row_idx == 0:
            # 第一行是表头
            headers = [str(cell) if cell else '' for cell in row]
            continue

        # 跳过空行
        if not any(row):
            continue

        row_data = {}
        for col_idx, cell in enumerate(row):
            if col_idx < len(headers) and headers[col_idx]:
                key = headers[col_idx]
                value = cell
                if isinstance(value, (int, float)):
                    row_data[key] = value
                elif value:
                    row_data[key] = str(value)
                else:
                    row_data[key] = ''

        if row_data:
            rows.append(row_data)

    print(f'  Found {len(rows)} rows with headers: {headers}')
    wb.close()
    return headers, rows

def format_for_dashboard(headers, rows):
    """将表格数据格式化为 dashboard 需要的格式"""
    # 根据 headers 映射到 dashboard 字段
    # 原始数据格式: 运单号/订单号/客户/目的地/状态/预计到达/实际到达/总金额/备注
    dashboard_rows = []

    for row in rows:
        # 尝试不同的列名映射
        order_id = row.get('运单号', row.get('订单号', row.get('Order ID', row.get('Logistics ID', ''))))
        customer = row.get('客户', row.get('Customer', row.get('客户名', '')))
        destination = row.get('目的地', row.get('Destination', row.get('目的港', '')))
        status_raw = row.get('状态', row.get('Status', row.get('物流状态', '')))
        eta = row.get('预计到达', row.get('ETA', row.get('预计到港', '')))
        actual_arrival = row.get('实际到达', row.get('Actual Arrival', row.get('实际到港', '')))
        total_price = row.get('总金额', row.get('Total Price', row.get('金额', row.get('Total Amount', 0))))
        notes = row.get('备注', row.get('Notes', row.get('Remark', '')))

        # 映射状态到英文
        status = STATUS_MAP.get(status_raw, status_raw)

        # 格式化金额
        try:
            amount = float(total_price) if total_price else 0
        except (ValueError, TypeError):
            amount = 0

        dashboard_rows.append({
            'id': order_id,
            'customer': customer,
            'destination': destination,
            'status': status,
            'statusRaw': status_raw,
            'eta': str(eta) if eta else '',
            'actualArrival': str(actual_arrival) if actual_arrival else '',
            'totalPrice': amount,
            'notes': notes,
        })

    return dashboard_rows

def push_to_github(data_json_path):
    """推送 data.json 到 GitHub Pages"""
    if not GITHUB_PAT:
        print('[5] No GITHUB_PAT, skipping push to GitHub')
        return False

    print(f'[5] Pushing data.json to GitHub...')
    api_url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/data.json'

    # 读取当前文件内容
    headers = {
        'Authorization': f'token {GITHUB_PAT}',
        'Content-Type': 'application/json',
        'User-Agent': 'sync-bot',
    }

    # 获取当前文件的 SHA
    resp = requests.get(api_url, headers=headers, timeout=15)
    current_sha = ''
    if resp.status_code == 200:
        current_sha = resp.json().get('sha', '')

    # 读取新文件内容
    with open(data_json_path, 'r', encoding='utf-8') as f:
        content = f.read()

    content_b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')

    # 推送更新
    payload = {
        'message': f'Auto sync: {time.strftime("%Y-%m-%d %H:%M:%S")}',
        'content': content_b64,
        'sha': current_sha,
    }

    resp = requests.put(api_url, headers=headers, json=payload, timeout=30)

    if resp.status_code in [200, 201]:
        print(f'  ✅ Pushed successfully!')
        return True
    else:
        print(f'  ❌ Push failed: {resp.status_code}')
        print(f'  Response: {resp.text[:200]}')
        return False

def main():
    if not COOKIE:
        print('ERROR: COOKIE environment variable not set!')
        print('')
        print('To get your Cookie:')
        print('1. Open https://docs.qq.com/sheet/DWk1ESWh0VFJKUGlI in your browser')
        print('2. F12 → Application → Cookies → docs.qq.com')
        print('3. Copy all cookie values as a single string')
        print('4. Set COOKIE environment variable')
        print('')
        print('Or use the browser console bookmarklet:')
        print('  document.cookie')
        sys.exit(1)

    print(f'=== Tencent Docs Sync Started ===')
    print(f'Doc ID: {DOC_ID}')
    print(f'Cookie length: {len(COOKIE)} chars')
    print(f'XSRF: {get_xsrf_from_cookie(COOKIE)[:20]}...')
    print()

    # Step 1: 创建导出任务
    export_result = create_export_task(COOKIE)

    task_id = export_result.get('taskId', export_result.get('data', {}).get('taskId', ''))

    if not task_id and export_result.get('retcode') == 100002:
        print('❌ Cookie expired or invalid! Please update your Cookie.')
        sys.exit(1)

    if not task_id:
        # 可能直接返回了下载URL
        download_url = export_result.get('downloadUrl', export_result.get('url', export_result.get('data', {}).get('url', '')))
        if not download_url:
            print('❌ Could not create export task!')
            print(f'Full response: {json.dumps(export_result, ensure_ascii=False)[:500]}')
            sys.exit(1)

    # Step 2: 等待导出完成
    download_url = None
    if task_id:
        download_url = wait_for_export(COOKIE, task_id)

    if not download_url:
        print('❌ Export failed - no download URL!')
        sys.exit(1)

    # Step 3: 下载 xlsx
    xlsx_path = download_xlsx(download_url, COOKIE)
    if not xlsx_path:
        sys.exit(1)

    # Step 4: 解析数据
    headers, rows = parse_xlsx(xlsx_path)
    dashboard_rows = format_for_dashboard(headers, rows)

    # Step 5: 生成 data.json
    data = {
        'lastUpdated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'source': 'tencent-docs-auto-sync',
        'rowCount': len(dashboard_rows),
        'rows': dashboard_rows,
    }

    output_path = os.path.join(os.path.dirname(__file__), 'data.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f'[4] Generated data.json: {len(dashboard_rows)} rows')

    # Step 6: 推送到 GitHub
    push_success = push_to_github(output_path)

    # 清理临时文件
    try:
        os.unlink(xlsx_path)
    except:
        pass

    print()
    if push_success:
        print('=== ✅ SYNC COMPLETE ===')
    else:
        print('=== ⚠️ SYNC PARTIAL (data.json generated, push failed) ===')
        print(f'  data.json is at: {output_path}')

    return push_success

if __name__ == '__main__':
    main()
