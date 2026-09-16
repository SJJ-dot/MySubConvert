"""MySubConvert —— 机场订阅转换服务。

请求处理流程（与 convert() / load_subscription() 中的实现一一对应）：
  1. 根据配置拉取机场订阅（sub_url 取自请求参数或 config.yaml，每次请求都实时拉取）；
  2. 拉取失败时使用本地缓存（cache_ttl 有效期内的那一份）；
  3. 缓存未命中时，根据本地配置生成最小配置文件（仅本地节点，不含机场内容）；
  4. 根据本地配置转换订阅文件：除代理节点外只使用本地配置，机场只贡献代理节点
     （插入位置由本地配置中的 * 表达式指定）。
"""

from gevent import monkey

monkey.patch_all()

import base64
import logging
import os
import time
from copy import deepcopy

import requests
import yaml
from flask import Flask, request, Response
from urllib3.exceptions import InsecureRequestWarning

import merge

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.yaml')
HOME_CACHE_FILE = os.path.join(BASE_DIR, 'home_cache.yaml')

# 订阅地址：config.yaml 可配置 sub_url（默认）与 sub_url1 ~ sub_url5（备用）。
# 请求 ?sub_url=N（1~5）选择第 N 个，未传或回退时使用 sub_url。
SUB_URL_COUNT = 5
SUB_URL_KEYS = ['sub_url'] + ['sub_url%d' % i for i in range(1, SUB_URL_COUNT + 1)]

# 控制配置项（不会出现在输出的 Clash 配置中，合并前会被剥离）
CONTROL_KEYS = {
    'api_path',                     # 订阅接口路径（修改需重启）
    'password',                     # 接口访问密码
    'basic_auth',                   # 动态更新 Home 节点 IP/端口 的基础认证 username:password
    'server_url',                   # 获取 Home 节点最新 IP/端口 的服务地址
    'cache_ttl',                    # 机场配置缓存时长（秒，0 表示不过期）
    # 以下三项已废弃：输出顶层 key 完全取自本地配置，无需再剥离；代理组与规则
    # 也只取本地配置，无需再排除/合并机场的组。
    # 仍列在控制项中，只为把旧配置里的残留剥离掉，避免泄漏进输出的 Clash 配置。
    'remove_keys',
    'exclude_groups',
    'merge_groups',
} | set(SUB_URL_KEYS)               # sub_url / sub_url1 ~ sub_url5

app = Flask(__name__)


# ===================== 日志 =====================
def configure_logging(app):
    fmt = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    formatter = logging.Formatter(fmt)

    gunicorn_error = logging.getLogger('gunicorn.error')
    gunicorn_access = logging.getLogger('gunicorn.access')

    if gunicorn_error.handlers:
        handlers = gunicorn_error.handlers[:]
        root_level = gunicorn_error.level
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
        handlers = logging.root.handlers[:]
        root_level = logging.root.level

    for h in handlers:
        h.setFormatter(formatter)

    logging.root.handlers = handlers
    logging.root.setLevel(root_level)

    app.logger.handlers = handlers[:]
    app.logger.setLevel(root_level)
    app.logger.propagate = False

    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.handlers = handlers[:]
    werkzeug_logger.setLevel(root_level)
    werkzeug_logger.propagate = False

    if gunicorn_access.handlers:
        gunicorn_access.handlers = handlers[:]
        gunicorn_access.setLevel(root_level)
        gunicorn_access.propagate = False


configure_logging(app)


@app.before_request
def log_request():
    g_start = time.time()
    raw = request.get_data(cache=True)
    try:
        body = raw.decode('utf-8', errors='replace')
    except Exception:
        body = '<binary>'
    if len(body) > 2000:
        body = body[:2000] + '...[truncated]'

    from urllib.parse import urlencode
    args_multi = request.args.to_dict(flat=False)
    sanitized_args = {
        k: (['***'] if k.lower() == 'password' else v)
        for k, v in args_multi.items()
    }
    args_for_log = {
        k: (v[0] if isinstance(v, list) and len(v) == 1 else v)
        for k, v in sanitized_args.items()
    }
    query = urlencode(sanitized_args, doseq=True)
    path = request.path + ('?' + query if query else '')

    logging.info(
        "INCOMING %s %s %s Headers=%s Args=%s Body=%s",
        request.remote_addr, request.method, path,
        dict(request.headers), args_for_log, body,
    )


# ===================== 配置加载 =====================
def read_yaml_config(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logging.error("读取 YAML 配置失败 %s: %s", file_path, e)
        return None


def load_local_config(path=CONFIG_FILE):
    """读取 config.yaml，拆分为 (control 控制项, template Clash 模板)。"""
    raw = read_yaml_config(path) or {}
    control = {k: raw.get(k) for k in CONTROL_KEYS if k in raw}
    template = {k: v for k, v in raw.items() if k not in CONTROL_KEYS}
    return control, template


def resolve_sub_url(requested, control):
    """把请求参数 sub_url 解析为真实订阅地址。

    - `?sub_url=N`（1~5）：取 config.yaml 中的 sub_urlN；该项为空时回退 sub_url；
    - 传入完整地址（含非数字）：直接使用，兼容旧用法；
    - 数字超出 1~5 或未传：使用 sub_url。
    """
    requested = str(requested or '').strip()
    if not requested:
        return control.get('sub_url') or ''

    if requested.isdigit():
        idx = int(requested)
        if 1 <= idx <= SUB_URL_COUNT:
            url = control.get('sub_url%d' % idx) or ''
            if url:
                logging.info("按请求选择订阅地址: sub_url=%s -> sub_url%d", requested, idx)
                return url
            logging.info("sub_url%d 未配置，回退默认 sub_url", idx)
            return control.get('sub_url') or ''
        logging.info("请求的 sub_url=%s 超出 1~%d 范围，使用默认 sub_url",
                     requested, SUB_URL_COUNT)
        return control.get('sub_url') or ''

    return requested


# ===================== 订阅获取：第 1 步拉取 → 第 2 步回退缓存 → 第 3 步最小配置 =====================
remote_cache = {}  # sub_url -> {'ts': float, 'data': dict, 'userinfo': str}

# 拉取订阅统一使用的请求头
SUB_HEADER = {'Accept': '*/*', 'User-Agent': 'clash-verge/v2.4.7'}


def fetch_remote(sub_url, ttl=0):
    """流程第 1、2 步：拉取机场订阅，失败时回退本地缓存。返回 (dict|None, userinfo)。

    网络优先：每次请求都实时拉取机场配置，缓存只作为失败时的回退。
    - 第 1 步 拉取成功：写入缓存并返回最新配置与 userinfo；
    - 第 2 步 拉取失败：回退缓存（仅当缓存仍在 cache_ttl 有效期内，ttl<=0 视为永不过期）；
    - 缓存未命中（超出有效期或从未拉取过）：返回 (None, '')，由第 3 步生成最小配置文件。
    """
    now = time.time()
    cached = remote_cache.get(sub_url)
    usable = cached and (ttl <= 0 or now - cached['ts'] < ttl)

    try:
        resp = requests.get(sub_url, headers=SUB_HEADER, verify=False, timeout=(5, 50))
        resp.encoding = 'utf-8'
        logging.info("第 1 步：拉取订阅 %s -> %s", sub_url, resp.status_code)
        if resp.status_code != 200:
            raise ValueError('意外的状态码 %s' % resp.status_code)
        userinfo = resp.headers.get('subscription-userinfo', '')
        data = merge.parse_clash(resp.text)
        remote_cache[sub_url] = {'ts': now, 'data': data, 'userinfo': userinfo}
        logging.info("第 1 步：订阅解析成功，缓存已更新: %s", sub_url)
        return data, userinfo
    except Exception as e:
        logging.error("第 1 步失败：拉取/解析订阅 %s: %s", sub_url, e)
        if usable:
            logging.info("第 2 步：回退缓存（距今 %d 秒）: %s", int(now - cached['ts']), sub_url)
            return deepcopy(cached['data']), cached['userinfo']
        if cached:
            logging.info("第 2 步：缓存已超出 cache_ttl=%s，不作为回退: %s", ttl, sub_url)
        else:
            logging.info("第 2 步：无本地缓存可回退: %s", sub_url)
        return None, ''


# ===================== Home 节点动态 IP =====================
def load_home_cache():
    # home_cache.yaml 初始可能不存在，缺失时静默返回空（不打印 ERROR）
    try:
        with open(HOME_CACHE_FILE, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.error("读取 Home 缓存失败 %s: %s", HOME_CACHE_FILE, e)
        return {}


def save_home_cache(data):
    try:
        with open(HOME_CACHE_FILE, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False)
    except Exception as e:
        logging.error("写入 Home 缓存失败: %s", e)


def apply_home_cache(template):
    """将动态刷新的 Home 节点 IP/端口应用到本地模板（仅内存，不修改 config.yaml）。"""
    home = load_home_cache()
    if not home:
        return
    name = home.get('name')
    for p in template.get('proxies') or []:
        if p.get('name') == name:
            if 'server' in home:
                p['server'] = home['server']
            if 'port' in home:
                p['port'] = int(home['port'])


def refresh_proxy_ip_port(control):
    """从 server_url 获取 Home 节点最新 IP/端口并持久化到 home_cache.yaml。"""
    basic_auth = control.get('basic_auth')
    url = control.get('server_url')
    if not url:
        return
    try:
        headers = {}
        if basic_auth:
            encoded = base64.b64encode(basic_auth.encode('utf-8')).decode('utf-8')
            headers['Authorization'] = 'Basic ' + encoded
        resp = requests.get(url, headers=headers, verify=False, timeout=(5, 15))
        resp.encoding = 'utf-8'
        if resp.status_code != 200:
            logging.info("刷新 Home IP 失败，状态码: %s", resp.status_code)
            return
        data = resp.json()
        ip = data.get('ip')
        port = data.get('port')
        if not ip or not port:
            logging.info("刷新 Home IP 返回为空")
            return
        save_home_cache({'name': 'Home', 'server': ip, 'port': int(port)})
        logging.info("刷新 Home IP 成功 -> %s:%s", ip, port)
    except Exception as e:
        logging.error("刷新 Home IP 异常: %s", e)


# ===================== 流程第 3 步：生成最小配置文件 =====================
def build_minimal_config(template):
    """流程第 3 步：机场订阅与缓存都不可用时，根据本地配置生成最小配置文件。

    本地模板本身就是最小可用配置：只含本地节点（Home）与本地代理组 / 规则，
    不含任何机场内容。本地代理组里的 * 表达式在第 4 步展开为空，
    组内保留其余本地成员（整组为空时兜底 DIRECT），客户端仍能正常分流。
    """
    logging.info("第 3 步：订阅拉取失败且缓存未命中，根据本地配置生成最小配置文件")
    return deepcopy(template)


def load_subscription(sub_url, control, template):
    """流程第 1-3 步：获取待转换的订阅配置，返回 (config, userinfo)。

    :param sub_url: 机场订阅地址（为空则跳过前两步，直接生成最小配置文件）
    :param control: 控制项配置（取 cache_ttl）
    :param template: 本地配置模板（第 3 步据此生成最小配置文件）
    :return: config 为第 1 步的机场订阅、第 2 步的缓存或第 3 步的最小配置；
             userinfo 仅在第 1 步拿到实时流量信息时非空。
    """
    if not sub_url:
        logging.info("未配置 sub_url，跳过订阅拉取")
        return build_minimal_config(template), ''

    ttl = int(control.get('cache_ttl', 0) or 0)
    config, userinfo = fetch_remote(sub_url, ttl)   # 第 1 步 → 失败回退缓存（第 2 步）
    if config is None:
        return build_minimal_config(template), ''   # 第 3 步
    return config, userinfo


# ===================== 流程第 4 步：订阅文件转换（合并入口） =====================
def convert(sub_url, control, template):
    """流程第 4 步：根据本地配置转换订阅文件。

    转换规则（详见 merge.merge_configs）：
    - 输出顶层 key 完全取自本地配置，机场除代理节点外的内容一律丢弃；
    - 代理节点 = 本地节点 + 机场节点（按 name 去重，本地同名优先）；
    - 机场节点的插入位置由本地配置中的 merge.REMOTE_PROXIES 表达式（`*`）指定。
    """
    apply_home_cache(template)

    if any(control.get(k) for k in ('remove_keys', 'exclude_groups', 'merge_groups')):
        logging.info("第 4 步：remove_keys / exclude_groups / merge_groups 已废弃"
                     "（输出只用本地配置），本次忽略")

    remote, userinfo = load_subscription(sub_url, control, template)

    merged = merge.merge_configs(template, remote)

    remote_count = len((remote or {}).get('proxies') or [])
    logging.info(
        "第 4 步：转换完成，节点 %d 个（含机场 %d 个）/ 代理组 %d 个 / 规则 %d 条",
        len(merged.get('proxies') or []), remote_count,
        len(merged.get('proxy-groups') or []),
        len(merged.get('rules') or []),
    )
    if remote_count and not merge.uses_remote_placeholder(template):
        logging.warning(
            "第 4 步：本地配置未使用 %s 表达式，机场 %d 个节点未被任何代理组引用",
            merge.REMOTE_PROXIES, remote_count)

    group_names = merge.names_of(merged.get('proxy-groups') or [])
    node_names = merge.names_of(merged.get('proxies') or [])

    dangling = merge.find_dangling_rules(merged.get('rules') or [], group_names, node_names)
    if dangling:
        logging.warning(
            "第 4 步：%d 条规则的目标不存在（需在本地配置中定义对应的组或节点）: %s",
            len(dangling), dangling[:3],
        )

    bad_members = merge.find_dangling_members(
        merged.get('proxy-groups') or [], group_names, node_names)
    if bad_members:
        logging.warning(
            "第 4 步：%d 个代理组成员不存在（检查拼写；旧版表达式 __REMOTE_PROXIES__ "
            "现已改为 \"%s\"）: %s",
            len(bad_members), merge.REMOTE_PROXIES, bad_members[:3],
        )
    return yaml.dump(merged, allow_unicode=True, sort_keys=False), userinfo


# ===================== 路由 =====================
try:
    _c, _ = load_local_config()
    API_PATH = '/' + str(_c.get('api_path', 'api')).lstrip('/')
except Exception:
    API_PATH = '/api'


@app.route(API_PATH)
def api():
    control, template = load_local_config()
    password = request.args.get('password')
    if password != str(control.get('password', '')):
        return 'Hello World!'

    refresh_proxy_ip_port(control)

    sub_url = resolve_sub_url(request.args.get('sub_url'), control)
    clash_yaml, userinfo = convert(sub_url, control, template)
    if clash_yaml is None:
        return 'Hello World!'

    headers = {}
    if userinfo:
        headers['subscription-userinfo'] = userinfo
    else:
        logging.info("本次响应未携带 subscription-userinfo（客户端将保留上次显示值）")
    return Response(clash_yaml, mimetype='text/plain', headers=headers)


@app.route('/health')
def health():
    return 'ok'


if __name__ == '__main__':
    from gevent.pywsgi import WSGIServer

    http_server = WSGIServer(('0.0.0.0', 5000), app)
    http_server.serve_forever()
