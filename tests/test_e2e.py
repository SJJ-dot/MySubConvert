"""端到端回归测试：真实 HTTP 假机场 + 真实 Flask app（/api、/ui、/ui/save）。

覆盖缓存优先 + 后台加载 + 3 秒等待 + 热重载的完整链路。用 pytest 运行
（本机 Bash 通道对普通脚本的 stdout 不可靠，pytest 的输出正常）。
"""
import os
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml
import pytest

import main


def _find_node():
    """定位可用的 node 可执行文件（用于内联 JS 语法检查），找不到返回 None。"""
    import glob
    import shutil as _sh
    cand = _sh.which('node')
    if cand:
        return cand
    pats = [
        os.path.expanduser(r'~\.workbuddy\binaries\node\versions\*\node.exe'),
        r'C:\Program Files\nodejs\node.exe',
    ]
    for p in pats:
        hits = glob.glob(p)
        if hits:
            return sorted(hits)[-1]
    return None


@pytest.fixture
def airport():
    """本地假机场：节点名与 userinfo 随 version 变化，可注入延迟 / 故障。"""
    state = {'version': 1, 'delay': 0.0, 'fail': False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if state['fail']:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'busy')
                return
            if state['delay']:
                time.sleep(state['delay'])
            body = yaml.safe_dump({
                'port': 7890,
                'proxies': [
                    {'name': 'AIR%d-A' % state['version'], 'type': 'ss',
                     'server': '1.2.3.4', 'port': 100},
                    {'name': 'AIR%d-B' % state['version'], 'type': 'ss',
                     'server': '5.6.7.8', 'port': 200},
                ],
                # 机场的组、规则与独有 key 都必须被丢弃
                'proxy-groups': [{'name': '机场组', 'type': 'select',
                                  'proxies': ['DIRECT']}],
                'rules': ['MATCH,DIRECT'],
                'mixed-port': 7893,
            }, allow_unicode=True).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('subscription-userinfo',
                             'upload=%d; download=%d' % (state['version'],
                                                         state['version'] * 10))
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state['url'] = 'http://127.0.0.1:%d/sub' % port
    yield state
    # 注意：不要调 srv.shutdown()。gevent patch 后它会永久挂住进程，
    # 导致 pytest 无法退出（实测）。serve_forever 跑在 daemon 线程上，
    # 进程结束时会自然回收。


@pytest.fixture
def e2e_app(airport, tmp_path, monkeypatch):
    """把 main 的所有文件路径指到临时目录，并写好指向假机场的配置。"""
    cfg = tmp_path / 'config.yaml'
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'config.yaml')
    text = open(src, encoding='utf-8').read()
    text = main.assemble_config(text, {
        'sub_url': airport['url'],
        # 只写哈希：**配置里从来不落明文口令**
        'password_hash': main.hash_password('e2e-pass', iterations=1000),
        'cache_ttl': '60',
    })
    cfg.write_text(text, encoding='utf-8')

    monkeypatch.setattr(main, 'CONFIG_FILE', str(cfg))
    monkeypatch.setattr(main.load_local_config, '__defaults__', (str(cfg),))
    monkeypatch.setattr(main, 'SUBCACHE_FILE', str(tmp_path / 'sub_cache.json'))
    monkeypatch.setattr(main, 'HOME_CACHE_FILE', str(tmp_path / 'home_cache.yaml'))
    main.remote_cache.clear()
    main._loading.clear()
    main._load_done.clear()
    main.invalidate_config()

    client = main.app.test_client()
    # 界面已暴露公网并要求登录：这里先登入，免得每个用例各自处理 302
    assert client.post('/ui/login',
                       data={'password': 'e2e-pass'}).status_code == 302
    yield client
    main.remote_cache.clear()
    main.invalidate_config()


def _api(client, sub=None, password='e2e-pass'):
    q = {'password': password}
    if sub is not None:
        q['sub_url'] = sub
    t0 = time.time()
    r = client.get(main.API_PATH, query_string=q)
    return r, time.time() - t0


def _names(out):
    return main.merge.names_of(out['proxies'])


def test_e2e_first_request_loads_then_cache_hits(e2e_app, airport):
    """首请求触发加载；随后的请求走新鲜缓存，不再打机场。"""
    r, dt = _api(e2e_app)
    out = yaml.safe_load(r.get_data(as_text=True))
    assert r.status_code == 200
    assert _names(out) == ['Home', 'AIR1-A', 'AIR1-B']
    assert dt < 3.0, '首请求应在 3 秒内完成，实际 %.2fs' % dt
    assert '机场组' not in main.merge.names_of(out['proxy-groups'])
    assert 'mixed-port' not in out
    assert r.headers.get('subscription-userinfo') == 'upload=1; download=10'

    # 机场已有 v2，但 60 秒内不该去取
    airport['version'] = 2
    r2, dt2 = _api(e2e_app)
    out2 = yaml.safe_load(r2.get_data(as_text=True))
    assert _names(out2) == ['Home', 'AIR1-A', 'AIR1-B'], '新鲜缓存不应重新加载'
    assert dt2 < 0.2, '命中缓存应几乎无耗时，实际 %.3fs' % dt2


def test_e2e_expired_cache_reloads(e2e_app, airport):
    """缓存超过 60 秒：触发重新加载，返回新内容。"""
    _api(e2e_app)
    airport['version'] = 2
    with main._state_lock:
        main.remote_cache[airport['url']]['ts'] -= 61

    r, dt = _api(e2e_app)
    out = yaml.safe_load(r.get_data(as_text=True))
    assert _names(out) == ['Home', 'AIR2-A', 'AIR2-B']
    assert dt < 3.0, '应在 3 秒内返回，实际 %.2fs' % dt


def test_e2e_slow_airport_times_out_to_old_cache(e2e_app, airport):
    """机场响应超过 3 秒：请求约 3 秒返回旧缓存，不阻塞更久。"""
    _api(e2e_app)                       # 先建立 v1 缓存
    airport['version'] = 3
    airport['delay'] = 6.0
    with main._state_lock:
        main.remote_cache[airport['url']]['ts'] -= 61

    r, dt = _api(e2e_app)
    out = yaml.safe_load(r.get_data(as_text=True))
    assert 2.5 < dt < 4.5, '应在约 3 秒超时返回，实际 %.2fs' % dt
    assert _names(out) == ['Home', 'AIR1-A', 'AIR1-B'], '超时应返回旧缓存'


def test_e2e_airport_down_still_serves_cache(e2e_app, airport):
    """机场故障：缓存永不过期，服务不中断。"""
    _api(e2e_app)
    airport['fail'] = True
    with main._state_lock:
        main.remote_cache[airport['url']]['ts'] -= 61

    r, dt = _api(e2e_app)
    out = yaml.safe_load(r.get_data(as_text=True))
    assert r.status_code == 200
    assert _names(out) == ['Home', 'AIR1-A', 'AIR1-B']
    assert dt < 4.5


def test_e2e_minimal_config_when_sub_url_unconfigured(tmp_path, monkeypatch):
    """sub_url 未配置时：退化为仅本地节点的最小配置。

    注意 `?sub_url=`（显式空串）≠ 未配置 —— 空串会回退到 config.yaml 的 sub_url
    （见 resolve_sub_url 的既定语义），所以这里通过清空配置来构造该分支。
    """
    cfg = tmp_path / 'config.yaml'
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'config.yaml')
    text = main.assemble_config(open(src, encoding='utf-8').read(), {
        'sub_url': '""',
        'password_hash': main.hash_password('e2e-pass', iterations=1000),
    })
    cfg.write_text(text, encoding='utf-8')

    monkeypatch.setattr(main, 'CONFIG_FILE', str(cfg))
    monkeypatch.setattr(main.load_local_config, '__defaults__', (str(cfg),))
    monkeypatch.setattr(main, 'SUBCACHE_FILE', str(tmp_path / 'sub_cache.json'))
    monkeypatch.setattr(main, 'HOME_CACHE_FILE', str(tmp_path / 'home_cache.yaml'))
    main.remote_cache.clear()
    main.invalidate_config()

    client = main.app.test_client()
    r, _ = _api(client, password='e2e-pass')
    out = yaml.safe_load(r.get_data(as_text=True))
    assert _names(out) == ['Home']
    members = [m for g in out['proxy-groups'] for m in (g.get('proxies') or [])]
    assert '*' not in members
    assert r.headers.get('subscription-userinfo') is None

    main.remote_cache.clear()
    main.invalidate_config()


def test_e2e_ui_page_and_hot_reload(e2e_app, airport):
    """/ui 可打开；保存后 /api 立刻生效（无需重启）。"""
    r = e2e_app.get('/ui')
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'MySubConvert 配置' in html
    assert airport['url'] in html

    rules = ('rules:\n'
             '  - DOMAIN-SUFFIX,e2e-test.com,DIRECT\n'
             '  - MATCH,🐟 漏网之鱼\n')
    r2 = e2e_app.post('/ui/save', json={'blocks': {'rules': rules}})
    assert r2.get_json().get('ok') is True, r2.get_json()

    r3, _ = _api(e2e_app)
    out = yaml.safe_load(r3.get_data(as_text=True))
    assert out['rules'][0] == 'DOMAIN-SUFFIX,e2e-test.com,DIRECT'
    assert '服务控制配置' in open(main.CONFIG_FILE, encoding='utf-8').read()
    assert os.path.exists(main.CONFIG_FILE + '.bak')


def test_e2e_password_mismatch_returns_hello(e2e_app):
    """密码错误：返回 Hello World!，不泄露配置。"""
    r, _ = _api(e2e_app, password='wrong')
    assert r.get_data(as_text=True) == 'Hello World!'
