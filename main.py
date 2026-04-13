from gevent import monkey

monkey.patch_all()

import random
from collections import OrderedDict

import requests
import yaml
from flask import Flask, request, Response
from urllib3.exceptions import InsecureRequestWarning
import base64
import time
from flask import g

# Suppress only the single InsecureRequestWarning from urllib3
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
app = Flask(__name__)
# logger
import logging


def configure_logging(app):
    fmt = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    formatter = logging.Formatter(fmt)

    gunicorn_error = logging.getLogger('gunicorn.error')
    gunicorn_access = logging.getLogger('gunicorn.access')

    if gunicorn_error.handlers:
        # 复用 gunicorn 的 handlers，但强制统一 formatter & level
        handlers = gunicorn_error.handlers[:]
        root_level = gunicorn_error.level
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
        handlers = logging.root.handlers[:]
        root_level = logging.root.level

    # 强制为所有 handler 设定统一 formatter
    for h in handlers:
        h.setFormatter(formatter)

    logging.root.handlers = handlers
    logging.root.setLevel(root_level)

    # 统一绑定到 app.logger、werkzeug、以及 gunicorn.access（如果存在）
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


# python
@app.before_request
def log_request():
    g.start_time = time.time()
    raw = request.get_data(cache=True)
    try:
        body = raw.decode('utf-8', errors='replace')
    except Exception:
        body = '<binary>'
    if len(body) > 2000:
        body = body[:2000] + '...[truncated]'

    # 使用 flat=False 保留多值参数，然后把密码脱敏并重建查询串
    from urllib.parse import urlencode
    args_multi = request.args.to_dict(flat=False)
    sanitized_args = {}
    for k, v in args_multi.items():
        if k.lower() == 'password':
            sanitized_args[k] = ['***']
        else:
            sanitized_args[k] = v
    # 将多值参数转为更友好的单值或列表表示用于日志
    args_for_log = {k: (v[0] if isinstance(v, list) and len(v) == 1 else v) for k, v in sanitized_args.items()}

    query = urlencode(sanitized_args, doseq=True)
    sanitized_full_path = request.path + ('?' + query if query else '')

    logging.info(
        "INCOMING %s %s %s Headers=%s Args=%s Body=%s",
        request.remote_addr,
        request.method,
        sanitized_full_path,
        dict(request.headers),
        args_for_log,
        body
    )


# @app.after_request
# def log_response(response):
#     duration = time.time() - getattr(g, 'start_time', time.time())
#     logging.info(
#         "OUTGOING %s %s -> %s Duration=%.3fs Headers=%s",
#         request.method,
#         request.full_path,
#         response.status_code,
#         duration,
#         dict(response.headers)
#     )
#     return response


def read_yaml_config(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            return yaml.safe_load(file)
    except Exception as e:
        logging.error("Error reading YAML config: %s" % e)
        return None


cache_yaml = {}


def convert(sub_url):
    subscription_userinfo = ''
    default_config = read_yaml_config('template.yaml')
    try:
        response = requests.get(sub_url, verify=False, timeout=(5, 50))
        response.encoding = 'utf-8'
        subscription_userinfo = response.headers.get('subscription-userinfo', '')
        remote_config = yaml.safe_load(response.text)
        cache_yaml[sub_url] = remote_config
        cache_yaml[sub_url + 'subscription_userinfo'] = subscription_userinfo
        logging.info("YAML loaded successfully from sub_url")
    except Exception as e:
        logging.error("sub_url Error loading YAML: %s" % e)
        if sub_url in cache_yaml:
            remote_config = cache_yaml[sub_url]
            subscription_userinfo = cache_yaml.get(sub_url + 'subscription_userinfo', '')
            logging.info("Cached YAML loaded successfully for %s" % sub_url)
        else:
            remote_config = None
            logging.error("No cached YAML available for %s" % sub_url)

    if remote_config is None and default_config is None:
        return None, subscription_userinfo

    if remote_config is None:
        return yaml.dump(default_config, allow_unicode=True, sort_keys=False), subscription_userinfo

    if default_config is None:
        return yaml.dump(remote_config, allow_unicode=True, sort_keys=False), subscription_userinfo

    tmp = OrderedDict()
    # 合并并去重 proxies
    for proxy in remote_config['proxies']:
        if proxy['name']:
            tmp[proxy['name']] = proxy
    for proxy in default_config['proxies']:
        if proxy['name']:
            tmp[proxy['name']] = proxy
    remote_config['proxies'] = list(tmp.values())

    exclude_groups = read_yaml_config('config.yaml')["exclude_groups"]
    # 合并并去重 proxy-groups
    tmp = OrderedDict()
    for group in remote_config['proxy-groups']:
        if group['name'] not in tmp and group['name'] not in exclude_groups:
            tmp[group['name']] = group
    for group in default_config['proxy-groups']:
        if group['name'] not in tmp and group['name'] not in exclude_groups:
            tmp[group['name']] = group
    remote_config['proxy-groups'] = list(tmp.values())
    # 合并并去重 rules
    tmp = []
    for rule in remote_config['rules']:
        group = rule.split(',')
        if len(group) >= 3:
            group = group[2]
        else:
            group = group[-1]
        if rule not in tmp and group not in exclude_groups:
            tmp.append(rule)
    idx = 0
    for rule in default_config['rules']:
        group = rule.split(',')
        if len(group) >= 3:
            group = group[2]
        else:
            group = group[-1]
        if rule not in tmp and group not in exclude_groups:
            tmp.insert(idx, rule)
            idx += 1
    remote_config['rules'] = tmp
    return yaml.dump(remote_config, allow_unicode=True, sort_keys=False), subscription_userinfo


@app.route(read_yaml_config('config.yaml')['api_path'])
def api():
    sub_url = request.args.get('sub_url')
    password = request.args.get('password')
    default_config = read_yaml_config('config.yaml')
    if password != str(default_config.get('password', '')):
        return 'Hello World!'
    refresh_proxy_ip_port()
    if sub_url is None:
        sub_url = default_config.get('sub_url')
    clash_yaml, subscription_userinfo = convert(sub_url)
    if clash_yaml is None:
        return 'Hello World!'
    headers = {}
    if subscription_userinfo != '':
        headers['subscription-userinfo'] = subscription_userinfo
    return Response(clash_yaml, mimetype='text/plain', headers=headers)


def refresh_proxy_ip_port():
    # 获取最新的代理 IP 和端口，并更新到 config.yaml 中
    try:
        default_config = read_yaml_config('config.yaml')
        basic_auth = default_config.get('basic_auth')
        url = default_config.get('server_url')
        if url is None:
            logging.info("cancel get_proxy_ip_port, basic_auth or url is None")
            return
        headers = {}
        if basic_auth:
            encoded = base64.b64encode(basic_auth.encode('utf-8')).decode('utf-8')
            headers['Authorization'] = 'Basic ' + encoded
        # add a reasonable timeout so this call doesn't block forever
        response = requests.get(url, headers=headers, verify=False, timeout=(5, 15))
        response.encoding = 'utf-8'
        if response.status_code == 200:
            data = response.json()  # {'ip': 'xxx.xxx.xxx.xxx', 'port': 12345}
            ip = data.get('ip')
            port = data.get('port')
            if ip is None or port is None:
                logging.info("ip or port is None")
                return
            for proxy in default_config['proxies']:
                if "Home" == proxy['name']:
                    proxy['server'] = ip
                    proxy['port'] = int(port)
            default_config.pop('api_path', None)
            default_config.pop('password', None)
            default_config.pop('sub_url', None)
            default_config.pop('basic_auth', None)
            default_config.pop('server_url', None)
            default_config.pop('exclude_groups', None)
            with open('template.yaml', 'w', encoding='utf-8') as file:
                yaml.dump(default_config, file, allow_unicode=True, sort_keys=False)
            logging.info("refresh_proxy_ip_port success")
        else:
            logging.info(f"Failed to get proxy IP and port. Status code: {response.status_code}")
    except Exception as e:
        logging.error(f"Failed to get proxy IP and port. Error: {e}")
        pass


# python
if __name__ == '__main__':
    # refresh_proxy_ip_port()
    # 不要和 gevent 一起使用 debug=True，否则会挂起
    from gevent.pywsgi import WSGIServer

    # app.debug = False
    http_server = WSGIServer(('0.0.0.0', 5000), app)
    http_server.serve_forever()
