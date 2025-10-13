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
# logger
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')



def read_yaml_config(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)


def convert(default_config, sub_url):
    subscription_userinfo = ''
    try:
        try:
            headers = {
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
            params = {
                "__t": str(random.randint(1000000000, 9999999999))
            }
            response = requests.get(sub_url, headers=headers, verify=False, params=params)
            response.encoding = 'utf-8'
            subscription_userinfo = response.headers.get('subscription-userinfo', '')
            content = yaml.safe_load(response.text)
        except Exception as e:
            logging.error("Error loading YAML: %s" % e)
            content = read_yaml_config('template.yaml')
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
        else:
            logging.info(f"Failed to get proxy IP and port. Status code: {response.status_code}")
    except Exception as e:
        logging.error(f"Failed to get proxy IP and port. Error: {e}")
        pass


if __name__ == '__main__':
    app.run(host="0.0.0.0", debug=False, port=5000)
