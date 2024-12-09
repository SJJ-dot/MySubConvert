from collections import OrderedDict

from flask import Flask, request, Response
import yaml
import requests
from urllib3.exceptions import InsecureRequestWarning

# Suppress only the single InsecureRequestWarning from urllib3
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
app = Flask(__name__)


def read_yaml_config(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)


def convert(default_config):
    try:
        response = requests.get(default_config['sub_url'], verify=False)
        response.encoding = 'utf-8'
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
    return yaml.dump(default_config, allow_unicode=True, sort_keys=False)


@app.route('/api')
def api():
    password = request.args.get('password')
    default_config = read_yaml_config('config.yaml')
    if password != default_config['password']:
        return 'error'
    return Response(convert(default_config), mimetype='text/plain')


@app.route('/set_config_ip_port')
def set_config_ip_port():
    password = request.args.get('password')
    ip = request.args.get('ip')
    port = request.args.get('port')
    name = request.args.get('name')
    default_config = read_yaml_config('config.yaml')
    if password != default_config['password']:
        return 'error'
    for proxy in default_config['proxies']:
        if proxy['name'] == name:
            if ip is not None:
                proxy['server'] = ip
            if port is not None:
                proxy['port'] = int(port)
    with open('config.yaml', 'w', encoding='utf-8') as file:
        yaml.dump(default_config, file, allow_unicode=True, sort_keys=False)
    return 'ok'


if __name__ == '__main__':
    app.run(host="0.0.0.0", debug=False, port=5000)
