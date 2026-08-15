from gevent import monkey

monkey.patch_all()

import base64
import logging
import os
import time

import requests
import yaml
from flask import Flask, request, Response
from urllib3.exceptions import InsecureRequestWarning

import merge

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.yaml')
HOME_CACHE_FILE = os.path.join(BASE_DIR, 'home_cache.yaml')

# 控制配置项（不会出现在输出的 Clash 配置中，合并前会被剥离）
CONTROL_KEYS = {
    'api_path',                     # 订阅接口路径（修改需重启）
    'password',                     # 接口访问密码
    'sub_url',                      # 机场订阅链接（可空）
    'basic_auth',                   # 动态更新 Home 节点 IP/端口 的基础认证 username:password
    'server_url',                   # 获取 Home 节点最新 IP/端口 的服务地址
    'exclude_groups',               # 合并时排除的代理组 / 规则目标组
    'remove_keys',                  # 从最终配置中移除的顶层 key
    'merge_groups',                 # 指定代理组合并：sources 组节点并入 target 组
    'cache_ttl',                    # 机场配置缓存时长（秒，0 表示不过期）
}

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


# ===================== 机场订阅拉取 + 缓存 =====================
remote_cache = {}  # sub_url -> {'ts': float, 'data': dict, 'userinfo': str}


def fetch_remote(sub_url, ttl=0):
    """拉取机场订阅，返回 (dict|None, userinfo)。

    - 命中未过期缓存直接返回；
    - 拉取失败且有缓存则回退缓存；
    - 拉取失败且无缓存返回 (None, '')，由调用方退化为本地模板。
    """
    now = time.time()
    cached = remote_cache.get(sub_url)
    if cached and (ttl <= 0 or now - cached['ts'] < ttl):
        return deepcopy(cached['data']), cached['userinfo']

    try:
        header = {'Accept': '*/*', 'User-Agent': 'clash-verge/v2.4.7'}
        resp = requests.get(sub_url, headers=header, verify=False, timeout=(5, 50))
        resp.encoding = 'utf-8'
        logging.info("GET %s -> %s", sub_url, resp.status_code)
        if resp.status_code != 200:
            raise ValueError('意外的状态码 %s' % resp.status_code)
        userinfo = resp.headers.get('subscription-userinfo', '')
        data = merge.parse_clash(resp.text)
        remote_cache[sub_url] = {'ts': now, 'data': data, 'userinfo': userinfo}
        logging.info("订阅解析成功: %s", sub_url)
        return data, userinfo
    except Exception as e:
        logging.error("拉取/解析订阅失败 %s: %s", sub_url, e)
        if cached:
            logging.info("使用缓存回退: %s", sub_url)
            return deepcopy(cached['data']), cached['userinfo']
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


# ===================== 合并入口 =====================
def convert(sub_url, control, template):
    exclude = control.get('exclude_groups') or []
    remove_keys = control.get('remove_keys') or []
    merge_groups = control.get('merge_groups') or []

    apply_home_cache(template)

    if not sub_url:
        return yaml.dump(template, allow_unicode=True, sort_keys=False), ''

    remote, userinfo = fetch_remote(sub_url, int(control.get('cache_ttl', 0) or 0))
    if remote is None:
        # 拉取失败且无缓存：退化为本地模板，保证服务可用
        return yaml.dump(template, allow_unicode=True, sort_keys=False), ''

    merged = merge.merge_configs(
        template, remote,
        exclude_groups=exclude,
        remove_keys=remove_keys,
        merge_groups=merge_groups,
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

    sub_url = request.args.get('sub_url') or control.get('sub_url') or ''
    clash_yaml, userinfo = convert(sub_url, control, template)
    if clash_yaml is None:
        return 'Hello World!'

    headers = {}
    if userinfo:
        headers['subscription-userinfo'] = userinfo
    return Response(clash_yaml, mimetype='text/plain', headers=headers)


@app.route('/health')
def health():
    return 'ok'


if __name__ == '__main__':
    from gevent.pywsgi import WSGIServer

    http_server = WSGIServer(('0.0.0.0', 5000), app)
    http_server.serve_forever()
