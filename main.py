import random
from collections import OrderedDict

import requests
import yaml
from flask import Flask, request, Response
from urllib3.exceptions import InsecureRequestWarning
import base64

# Suppress only the single InsecureRequestWarning from urllib3
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
app = Flask(__name__)


def read_yaml_config(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)


def convert(default_config):
    subscription_userinfo = ''
    try:
        headers = {
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        params = {
            "__t": str(random.randint(1000000000, 9999999999))
        }
        response = requests.get(default_config['sub_url'], headers=headers, verify=False, params=params)
        response.encoding = 'utf-8'
        subscription_userinfo = response.headers.get('subscription-userinfo', '')
        content = yaml.safe_load(response.text)
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
        print("Error: %s" % e)
    if 'sub_url' in default_config:
        del default_config['sub_url']
    if 'path' in default_config:
        del default_config['path']
    return yaml.dump(default_config, allow_unicode=True, sort_keys=False), subscription_userinfo


@app.route(read_yaml_config('config.yaml')['api_path'])
def api():
    password = request.args.get('password')
    default_config = read_yaml_config('config.yaml')
    if password != default_config['password']:
        return 'Hello World!'
    get_proxy_ip_port(default_config)
    yaml, subscription_userinfo = convert(default_config)
    headers = {}
    if subscription_userinfo != '':
        headers['subscription-userinfo'] = subscription_userinfo
    return Response(yaml, mimetype='text/plain', headers=headers)


def get_proxy_ip_port(default_config = None):
    if default_config is None:
        default_config = read_yaml_config('config.yaml')
    basic_auth = default_config.get('basic_auth')
    url = default_config.get('server_url')
    headers = {}
    if basic_auth:
        encoded = base64.b64encode(basic_auth.encode('utf-8')).decode('utf-8')
        headers['Authorization'] = 'Basic ' + encoded
    response = requests.get(url, headers=headers, verify=False)
    response.encoding = 'utf-8'
    if response.status_code == 200:
        data = response.json() # {'ip': 'xxx.xxx.xxx.xxx', 'port': 12345}
        ip = data.get('ip')
        port = data.get('port')
        for proxy in default_config['proxies']:
            if proxy['name'] == "Home":
                if ip is not None:
                    proxy['server'] = ip
                if port is not None:
                    proxy['port'] = int(port)
        with open('config.yaml', 'w', encoding='utf-8') as file:
            yaml.dump(default_config, file, allow_unicode=True, sort_keys=False)
    else:
        print(f"Failed to get proxy IP and port. Status code: {response.status_code}")


if __name__ == '__main__':
    app.run(host="0.0.0.0", debug=False, port=5000)
