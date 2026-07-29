#!/usr/bin/env python3
"""
物流进度看板 - 自动同步脚本（支持公开/私密两种模式）

公开模式：文档设置为「任何人可查看」后，无需 Cookie 即可拉取数据（dop-api/opendoc）
私密模式：使用 Cookie 鉴权拉取数据（async_export API）

优先尝试公开模式 → 失败后回退到 Cookie 模式

环境变量配置（在 GitHub Secrets 中设置）:
  TENCENT_SHEET_ID  - 腾讯文档表格 ID，如 DWk1ESWh0VFJKUGlI（必需）
  TENCENT_COOKIE    - 腾讯文档登录 Cookie（可选，仅私密模式需要）
  SHEET_TAB_NAME    - 工作表名称或 tab ID（可选，默认第一个）
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
OPENDOC_API = 'https://docs.qq.com/dop-api/opendoc'
DOC_PAGE_URL = f'https://docs.qq.com/sheet/D{SHEET_ID}'


# =====================================================================
# HTTP 工具函数
# =====================================================================

def make_headers(cookie=None):
    """构造请求头"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': DOC_PAGE_URL,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    if cookie:
        headers['Cookie'] = cookie
    return headers


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
    h = headers or make_headers(COOKIE)
    h['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(url, data=body, headers=h, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        print(f'  HTTP {e.code}: {body[:200]}', file=sys.stderr)
        raise


# =====================================================================
# 公开模式：dop-api/opendoc（无需 Cookie）
# =====================================================================

def discover_tab_id():
    """从文档页面 HTML 中发现 tab ID"""
    print('  发现 tab ID...')
    try:
        html = http_get(DOC_PAGE_URL, headers=make_headers())
    except Exception as e:
        print(f'  页面访问失败: {e}', file=sys.stderr)
        return ''

    # 多种模式匹配 tab ID
    patterns = [
        r'\?tab=([A-Za-z0-9]+)',           # URL 参数
        r'["\']tab["\']:\s*["\']([A-Za-z0-9]+)["\']',  # JSON 键值
        r'tabId["\']?\s*[=:]\s*["\']([A-Za-z0-9]+)["\']',  # 变量赋值
        r'activeTabId["\']?\s*[=:]\s*["\']([A-Za-z0-9]+)["\']',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, html)
        if matches:
            tab_id = matches[0]
            print(f'  发现 tab ID: {tab_id}')
            return tab_id

    # 尝试从 JSON 嵌入数据中提取所有 tab
    json_pattern = r'window\.(?:g_initialData|initialSheetData|sheetData)\s*=\s*(\{.+?\});'
    for jm in re.findall(json_pattern, html, re.DOTALL):
        try:
            jdata = json.loads(jm)
            # 深度搜索 tab 列表
            tabs = _find_tabs_in_json(jdata)
            if tabs:
                print(f'  从嵌入数据发现 tab: {tabs[0]}')
                return tabs[0]
        except:
            continue

    print('  未发现 tab ID，将使用默认值')
    return ''


def _find_tabs_in_json(obj, depth=0):
    """递归搜索 JSON 中的 tab 列表"""
    if depth > 8:
        return []
    if isinstance(obj, dict):
        # 直接查找 tabs 相关字段
        for key in ('tabs', 'tabList', 'sheets', 'sheetList'):
            val = obj.get(key)
            if isinstance(val, list) and val:
                ids = []
                for item in val:
                    if isinstance(item, dict):
                        for id_key in ('id', 'tabId', 'sheetId'):
                            if id_key in item:
                                ids.append(item[id_key])
                    elif isinstance(item, str):
                        ids.append(item)
                return ids
        # 递归搜索
        for v in obj.values():
            result = _find_tabs_in_json(v, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for v in obj:
            result = _find_tabs_in_json(v, depth + 1)
            if result:
                return result
    return []


def fetch_opendoc_data(tab_id=''):
    """从公开文档获取数据（dop-api/opendoc，无需 Cookie）"""
    params = {
        'id': SHEET_ID,
        'outformat': '1',
        'normal': '1',
    }
    if tab_id:
        params['tab'] = tab_id

    url = f'{OPENDOC_API}?{urllib.parse.urlencode(params)}'
    print(f'  请求: {url}')
    response_text = http_get(url, headers=make_headers())

    # 解析 JSON
    data = json.loads(response_text)
    return parse_opendoc_response(data)


def parse_opendoc_response(data):
    """解析 dop-api/opendoc 返回的 JSON，提取表格数据为二维数组"""
    print('  解析 opendoc JSON...')

    # 导航 JSON 树：clientVars → collab_client_vars → initialAttributedText → text
    cv = data.get('clientVars', data.get('client_vars', {}))
    ccv = cv.get('collab_client_vars', cv.get('collabClientVars', {}))
    iat = ccv.get('initialAttributedText', ccv.get('initialAttributedTextStr', {}))

    # text 可能是字符串（需 JSON 解析）或已是对象
    text_raw = iat.get('text', iat.get('textStr', ''))
    if isinstance(text_raw, str) and text_raw:
        try:
            text_data = json.loads(text_raw)
        except json.JSONDecodeError:
            print('  text 字段 JSON 解析失败', file=sys.stderr)
            text_data = []
    else:
        text_data = text_raw if isinstance(text_raw, list) else []

    if not text_data:
        raise Exception('opendoc 返回数据中未找到表格内容 (text 为空)')

    # ===== 多种已知结构模式 =====
    # 模式 A: text[0] = [info, rows, cols, ...], text[1] = {cell_data}
    # 模式 B: text[0][2] = [meta], text[0][3] = [meta], text[1] = {cell_data}
    # 模式 C: text 是嵌套多层数组

    # 尝试提取维度和单元格数据
    rows_count = 0
    cols_count = 0
    cell_data = {}

    # --- 提取维度 ---
    # 在 text_data 的第一层寻找维度信息
    first_elem = text_data[0] if text_data else None
    if isinstance(first_elem, list):
        # 遍历第一层找维度信息
        for item in first_elem:
            if isinstance(item, list):
                # 可能是 [padding, type, rows, padding2, cols] 格式
                for sub in item:
                    if isinstance(sub, list) and len(sub) >= 5:
                        r_candidate = sub[2] if isinstance(sub[2], int) else 0
                        c_candidate = sub[4] if isinstance(sub[4], int) else 0
                        if r_candidate > 0 and c_candidate > 0:
                            rows_count = r_candidate
                            cols_count = c_candidate

    # 如果没找到维度，尝试其他模式
    if rows_count == 0 or cols_count == 0:
        # 模式: text_data[0] 是 [meta_obj, dim_info, ...]
        for i, elem in enumerate(text_data[:3]):
            if isinstance(elem, (list, dict)):
                # 递归搜索维度
                dims = _find_dimensions(elem)
                if dims:
                    rows_count, cols_count = dims
                    break

    # --- 提取单元格数据 ---
    # 第二个元素通常是单元格数据对象
    second_elem = text_data[1] if len(text_data) > 1 else None

    # 尝试多种位置
    candidates = []
    if second_elem is not None:
        candidates.append(second_elem)
    # 也可能在更深层
    if len(text_data) > 2:
        candidates.append(text_data[2])

    for candidate in candidates:
        if isinstance(candidate, dict):
            cell_data = candidate
            break
        elif isinstance(candidate, list):
            # 可能是 [header, cell_dict] 格式
            for item in candidate:
                if isinstance(item, dict):
                    cell_data = item
                    break

    # 如果还是空的，深度搜索
    if not cell_data:
        cell_data = _find_cell_data(text_data)

    if not cell_data:
        raise Exception('opendoc 数据中未找到单元格数据')

    # ===== 构建表格 =====
    if rows_count == 0 or cols_count == 0:
        # 从 cell_data 推算维度
        max_pos = 0
        for key in cell_data:
            try:
                pos = int(key)
                max_pos = max(max_pos, pos)
            except (ValueError, TypeError):
                continue
        if max_pos > 0:
            # 推算列数：假设第一个行结束后有换行特征
            # 简单策略：用键 0 的位置推算，假设行有某个列数
            # 取前 20 个键推断列数
            sorted_keys = sorted(int(k) for k in cell_data if _is_int(k))
            if sorted_keys:
                # 找到第一个跳跃点（暗示换行）
                for i in range(1, min(len(sorted_keys), 30)):
                    if sorted_keys[i] - sorted_keys[i-1] > 1:
                        cols_count = sorted_keys[i]
                        break
                if cols_count == 0:
                    cols_count = min(sorted_keys[-1] + 1, 50)
                rows_count = (max_pos // cols_count) + 1 if cols_count > 0 else 1

    print(f'  表格维度: {rows_count} 行 x {cols_count} 列')

    # 提取每个单元格的值
    table = []
    for row_idx in range(rows_count):
        row = []
        for col_idx in range(cols_count):
            cell_key = str(row_idx * cols_count + col_idx)
            cell_value = _extract_cell_value(cell_data.get(cell_key, ''))
            row.append(cell_value)
        table.append(row)

    # 清理：去掉完全空的行
    table = [row for row in table if any(cell.strip() for cell in row)]

    # 也去掉完全空的列
    if table:
        non_empty_cols = set()
        for row in table:
            for ci, cell in enumerate(row):
                if cell.strip():
                    non_empty_cols.add(ci)
        if non_empty_cols:
            min_col = min(non_empty_cols)
            max_col = max(non_empty_cols)
            table = [row[min_col:max_col+1] for row in table]

    print(f'  有效数据: {len(table)} 行 x {len(table[0]) if table else 0} 列')
    return table


def _is_int(s):
    """判断字符串是否可以转为整数"""
    try:
        int(s)
        return True
    except (ValueError, TypeError):
        return False


def _extract_cell_value(cell):
    """从单元格数据中提取显示值"""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, list):
        # 多种已知格式:
        # [attribs, type, [meta, value]] → value 在 [2][1]
        # [attribs, value] → value 在 [1]
        # [value] → value 在 [0]
        for depth in [(2, 1), (1,), (0,)]:
            try:
                val = cell
                for idx in depth:
                    val = val[idx]
                if isinstance(val, str):
                    return val
                if isinstance(val, (int, float)):
                    return str(val)
            except (IndexError, TypeError, KeyError):
                continue
        # 尝试搜索字符串值
        return _find_string_in_list(cell)
    if isinstance(cell, dict):
        # 搜索字符串值
        for key in ('v', 'value', 'text', 'content', 's'):
            val = cell.get(key)
            if isinstance(val, str) and val:
                return val
        return _find_string_in_dict(cell)
    return str(cell) if cell else ''


def _find_string_in_list(lst, depth=0):
    """在列表中递归搜索第一个非空字符串"""
    if depth > 4:
        return ''
    for item in lst:
        if isinstance(item, str) and item.strip():
            return item
        if isinstance(item, list):
            result = _find_string_in_list(item, depth + 1)
            if result:
                return result
    return ''


def _find_string_in_dict(d, depth=0):
    """在字典中递归搜索第一个非空字符串"""
    if depth > 4:
        return ''
    for val in d.values():
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, dict):
            result = _find_string_in_dict(val, depth + 1)
            if result:
                return result
        if isinstance(val, list):
            result = _find_string_in_list(val, depth + 1)
            if result:
                return result
    return ''


def _find_dimensions(obj, depth=0):
    """递归搜索 JSON 中的表格维度信息"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for key in ('rowCount', 'rows', 'numRows', 'rowNum'):
            r = obj.get(key)
            if isinstance(r, int) and r > 0:
                for ckey in ('colCount', 'cols', 'numCols', 'colNum', 'columnCount'):
                    c = obj.get(ckey)
                    if isinstance(c, int) and c > 0:
                        return (r, c)
        for v in obj.values():
            result = _find_dimensions(v, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for v in obj:
            result = _find_dimensions(v, depth + 1)
            if result:
                return result
    return None


def _find_cell_data(obj, depth=0):
    """递归搜索 JSON 中的单元格数据对象"""
    if depth > 6:
        return {}
    if isinstance(obj, dict):
        # 检查是否是单元格数据：大量整数键
        int_keys = sum(1 for k in obj if _is_int(k))
        if int_keys > 10:  # 超过10个整数键，很可能是单元格数据
            return obj
        for v in obj.values():
            result = _find_cell_data(v, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for v in obj:
            result = _find_cell_data(v, depth + 1)
            if result:
                return result
    return {}


# =====================================================================
# 私密模式：async_export API（需要 Cookie）
# =====================================================================

def export_sheet_to_csv():
    """使用 Cookie 鉴权导出腾讯文档为 CSV（私密模式）"""
    print(f'  [私密模式] 发起导出任务... (sheet: {SHEET_ID})')

    # 1. 创建导出任务
    export_data = {
        'docId': SHEET_ID,
        'exportType': 'csv',
        'sheetId': TAB_NAME or '',
    }
    resp = http_post(EXPORT_API, export_data, headers=make_headers(COOKIE))
    result = json.loads(resp)

    if result.get('ret') != 0:
        raise Exception(f'导出失败: {result.get("msg", resp)}')

    operation_id = result.get('operationID')
    print(f'  任务ID: {operation_id}')

    # 2. 轮询导出进度
    print('  等待导出完成...')
    download_url = None
    for i in range(30):
        time.sleep(1)
        qresp = http_get(f'{QUERY_API}?operationID={operation_id}', headers=make_headers(COOKIE))
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
    print('  下载 CSV 数据...')
    csv_content = http_get(download_url, headers={})
    print(f'  CSV 大小: {len(csv_content)} 字节')

    return csv_content


# =====================================================================
# 通用：CSV 解析与字段映射
# =====================================================================

def parse_csv(csv_text):
    """解析 CSV 为二维数组"""
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    print(f'  解析到 {len(rows)} 行数据')
    return rows


def rows_to_objects(rows):
    """将二维数组转换为带英文字段名的对象数组"""
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


# =====================================================================
# 嵌入 HTML
# =====================================================================

def embed_data_into_html(rows):
    """将数据嵌入 HTML 的 EMBEDDED_DATA 变量"""
    print('  映射字段并嵌入 HTML...')

    with open(HTML_FILE, 'r', encoding='utf-8') as f:
        html = f.read()

    data_list = rows_to_objects(rows)
    data_json = json.dumps(data_list, ensure_ascii=False, indent=2)

    # 替换 EMBEDDED_DATA 变量
    pattern = r'(const|let|var)\s+EMBEDDED_DATA\s*=\s*\[.*?\];'
    replacement = f'const EMBEDDED_DATA = {data_json};'

    new_html, count = re.subn(pattern, replacement, html, flags=re.DOTALL)

    if count == 0:
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
        f'\\1{timestamp}',
        new_html
    )

    new_html = re.sub(
        r"(let\s+lastFetchTime\s*=\s*)'[^']*'",
        f"\\1'{timestamp}'",
        new_html
    )

    # 更新同步标记
    new_html = re.sub(
        r"(手动同步 @ )[^)]*",
        f"\\1{timestamp}",
        new_html
    )

    with open(HTML_FILE, 'w', encoding='utf-8') as f:
        f.write(new_html)

    print(f'  已更新 {HTML_FILE}，替换 {count} 处')
    print(f'  更新时间: {timestamp}')
    print(f'  有效数据行: {len(data_list)}')


# =====================================================================
# 主流程
# =====================================================================

def main():
    print('=' * 50)
    print('物流看板数据同步')
    print(f'时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'文档ID: {SHEET_ID}')
    print('=' * 50)

    if not os.path.exists(HTML_FILE):
        raise Exception(f'HTML 文件不存在: {HTML_FILE}')

    rows = None
    mode_used = ''

    # ===== 优先尝试公开模式 =====
    print('\n--- 尝试公开模式 (dop-api/opendoc, 无需 Cookie) ---')
    try:
        tab_id = TAB_NAME or discover_tab_id()
        print(f'[1/3] 使用 tab: {tab_id or "(默认)"}')
        table = fetch_opendoc_data(tab_id)
        if table and len(table) >= 2:
            rows = table
            mode_used = '公开模式 (opendoc)'
            print(f'  公开模式成功！获取 {len(table)} 行 x {len(table[0])} 列')
        else:
            print('  公开模式返回数据不足，尝试其他方式...')
    except Exception as e:
        print(f'  公开模式失败: {e}', file=sys.stderr)

    # ===== 回退到 Cookie 模式 =====
    if rows is None and COOKIE:
        print('\n--- 回退到私密模式 (async_export + Cookie) ---')
        try:
            csv_text = export_sheet_to_csv()
            rows = parse_csv(csv_text)
            mode_used = '私密模式 (Cookie)'
        except Exception as e:
            print(f'  私密模式失败: {e}', file=sys.stderr)

    # ===== 最终检查 =====
    if rows is None:
        print('\n所有模式均失败！', file=sys.stderr)
        print('请检查:', file=sys.stderr)
        print('  1. 腾讯文档是否已设置为「任何人可查看」', file=sys.stderr)
        print('  2. TENCENT_COOKIE 是否有效（如果是私密模式）', file=sys.stderr)
        print('  3. TENCENT_SHEET_ID 是否正确', file=sys.stderr)
        sys.exit(1)

    # ===== 嵌入 HTML =====
    print(f'\n--- 使用 {mode_used} 的数据 ---')
    embed_data_into_html(rows)

    print(f'\n同步完成！')
    print(f'  HTML 文件: {HTML_FILE}')
    print(f'  数据行数: {len(rows)}')
    print(f'  使用模式: {mode_used}')


if __name__ == '__main__':
    main()
