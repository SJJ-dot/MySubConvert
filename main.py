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

# 在 gunicorn 下复用其 error logger 的 handler，保证 logging.info 能输出到 gunicorn 管理的 stderr/stdout
gunicorn_logger = logging.getLogger('gunicorn.error')
if gunicorn_logger.handlers:
    logging.root.handlers = gunicorn_logger.handlers
    logging.root.setLevel(gunicorn_logger.level)
else:
    # 非 gunicorn 运行时回退到基本配置
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


@app.before_request
def log_request():
    g.start_time = time.time()
    # 读取并缓存请求体（不会二次消费）
    raw = request.get_data(cache=True)
    try:
        body = raw.decode('utf-8', errors='replace')
    except Exception:
        body = '<binary>'
    # 限制长度，避免日志过大
    if len(body) > 2000:
        body = body[:2000] + '...[truncated]'
    # 遮蔽敏感参数
    args = request.args.to_dict()
    if 'password' in args:
        args['password'] = '***'
    logging.info(
        "INCOMING %s %s %s Headers=%s Args=%s Body=%s",
        request.remote_addr,
        request.method,
        request.full_path,
        dict(request.headers),
        args,
        body
    )


@app.after_request
def log_response(response):
    duration = time.time() - getattr(g, 'start_time', time.time())
    logging.info(
        "OUTGOING %s %s -> %s Duration=%.3fs Headers=%s",
        request.method,
        request.full_path,
        response.status_code,
        duration,
        dict(response.headers)
    )
    return response


def read_yaml_config(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)


cache_yaml = {}


def convert(default_config, sub_url):
    subscription_userinfo = ''
    try:
        try:
            logging.info("Loading YAML from %s" % sub_url)
            response = requests.get(sub_url, verify=False, timeout=60)
            response.encoding = 'utf-8'
            subscription_userinfo = response.headers.get('subscription-userinfo', '')
            content = yaml.safe_load(response.text)
            cache_yaml[sub_url] = content
            cache_yaml[sub_url + 'subscription_userinfo'] = subscription_userinfo
            logging.info("YAML loaded successfully from %s" % sub_url)
        except Exception as e:
            logging.error("Error loading YAML: %s" % e)
            if sub_url in cache_yaml:
                logging.info("Using cached YAML for %s" % sub_url)
                content = cache_yaml[sub_url]
                subscription_userinfo = cache_yaml.get(sub_url + 'subscription_userinfo', '')
                logging.info("Cached YAML loaded successfully for %s" % sub_url)
            else:
                content = read_yaml_config('template.yaml')
                logging.info("Using local template.yaml as fallback")
        tmp = OrderedDict()
        # 合并并去重 proxies
        for proxy in content['proxies']:
            tmp[proxy['name']] = proxy
        for proxy in default_config['proxies']:
            tmp[proxy['name']] = proxy
        content['proxies'] = list(tmp.values())
        # 合并并去重 proxy-groups
        tmp = OrderedDict()
        for group in content['proxy-groups']:
            tmp[group['name']] = group
        for group in default_config['proxy-groups']:
            tmp[group['name']] = group
        content['proxy-groups'] = list(tmp.values())
        # 合并并去重 rules
        tmp = []
        for rule in default_config['rules']:
            if rule not in tmp:
                tmp.append(rule)
        for rule in content['rules']:
            if rule not in tmp:
                tmp.append(rule)
        content['rules'] = tmp
        default_config = content
    except Exception as e:
        logging.error("Error: %s" % e)
    if "api_path" in default_config:
        del default_config['api_path']
    if 'password' in default_config:
        del default_config['password']
    if 'sub_url' in default_config:
        del default_config['sub_url']
    if "basic_auth" in default_config:
        del default_config['basic_auth']
    if 'server_url' in default_config:
        del default_config['server_url']
    return yaml.dump(default_config, allow_unicode=True, sort_keys=False), subscription_userinfo


@app.route(read_yaml_config('config.yaml')['api_path'])
def api():
    sub_url = request.args.get('sub_url')
    password = request.args.get('password')
    default_config = read_yaml_config('config.yaml')
    if password != str(default_config.get('password', '')):
        return 'Hello World!'
    get_proxy_ip_port(default_config)
    yaml, subscription_userinfo = convert(default_config, sub_url if sub_url else default_config.get('sub_url', ''))
    headers = {}
    if subscription_userinfo != '':
        headers['subscription-userinfo'] = subscription_userinfo
    return Response(yaml, mimetype='text/plain', headers=headers)


def get_proxy_ip_port(default_config=None):
    try:
        logging.info("get_proxy_ip_port")
        if default_config is None:
            default_config = read_yaml_config('config.yaml')
        basic_auth = default_config.get('basic_auth')
        url = default_config.get('server_url')
        if basic_auth is None or url is None:
            logging.info("basic_auth or server_url is None")
            return
        headers = {}
        if basic_auth:
            encoded = base64.b64encode(basic_auth.encode('utf-8')).decode('utf-8')
            headers['Authorization'] = 'Basic ' + encoded
        response = requests.get(url, headers=headers, verify=False)
        response.encoding = 'utf-8'
        if response.status_code == 200:
            data = response.json()  # {'ip': 'xxx.xxx.xxx.xxx', 'port': 12345}
            ip = data.get('ip')
            port = data.get('port')
            if ip is None or port is None:
                logging.info("ip or port is None")
                return
            for proxy in default_config['proxies']:
                proxy['server'] = ip
                proxy['port'] = int(port)
            with open('config.yaml', 'w', encoding='utf-8') as file:
                yaml.dump(default_config, file, allow_unicode=True, sort_keys=False)
            logging.info("get_proxy_ip_port success")
        else:
            logging.info(f"Failed to get proxy IP and port. Status code: {response.status_code}")
    except Exception as e:
        logging.error(f"Failed to get proxy IP and port. Error: {e}")
        pass


if __name__ == '__main__':
    app.run(host="0.0.0.0", debug=False, port=5000)
