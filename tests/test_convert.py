"""合并逻辑测试：覆盖「本地配置为准」的模型（除代理节点外只使用本地配置、
表达式 `*` 展开、顶层 key 与顺序完全取自本地）、空 sub_url、fetch_remote 真实路径、
多订阅地址等场景。"""
import copy
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import merge
import main


FAKE_REMOTE = {
    # 与本地同名的 key：必须用本地的值
    'port': 8888,
    # 机场独有的 key：一律不得进入输出
    'dns': {'enable': True, 'nameserver': ['1.1.1.1']},
    'mixed-port': 7893,
    'unified-delay': False,
    'cfw-bypass': ['localhost'],
    'proxies': [
        {'name': 'Airport-HK', 'type': 'ss', 'server': '1.2.3.4', 'port': 100},
        {'name': 'Airport-JP', 'type': 'ss', 'server': '5.6.7.8', 'port': 200},
    ],
    # 以下代理组与规则应当被整体丢弃（只使用本地配置）
    'proxy-groups': [
        {'name': '🚀 节点选择', 'type': 'select',
         'proxies': ['Airport-HK', 'Airport-JP', 'DIRECT']},
        {'name': '🛑 全球拦截', 'type': 'select', 'proxies': ['REJECT', 'DIRECT']},
    ],
    'rules': [
        'DOMAIN-SUFFIX,google.com,🚀 节点选择',
        'MATCH,🐟 漏网之鱼',
    ],
}

# 一个不依赖真实 config.yaml 的最小本地模板，用于精确断言表达式展开行为
LOCAL_TEMPLATE = {
    'port': 7890,
    'proxies': [{'name': 'Home', 'type': 'ss', 'server': '10.0.0.1', 'port': 1}],
    'proxy-groups': [
        {'name': '🏠 回家', 'type': 'select', 'proxies': ['Home', 'DIRECT']},
        {'name': '🚀 节点选择', 'type': 'select',
         'proxies': ['♻️ 自动选择', '*', 'DIRECT']},
        {'name': '♻️ 自动选择', 'type': 'url-test', 'url': 'http://t', 'interval': 300,
         'proxies': ['*']},
        {'name': '🐟 漏网之鱼', 'type': 'select', 'proxies': ['🚀 节点选择', 'DIRECT']},
    ],
    'rules': ['DOMAIN-SUFFIX,github.com,🚀 节点选择', 'MATCH,🐟 漏网之鱼'],
}


def _tpl():
    return copy.deepcopy(LOCAL_TEMPLATE)


def _load_template():
    _, template = main.load_local_config()
    return template


def test_remote_top_level_keys_never_enter_output():
    """机场除 proxies 外的顶层 key 一律不进输出；输出 key 顺序与内容完全按本地配置。"""
    out = merge.merge_configs(_tpl(), dict(FAKE_REMOTE))
    local_keys = set(LOCAL_TEMPLATE)
    airport_only = [k for k in FAKE_REMOTE if k not in local_keys]
    assert airport_only, 'FAKE_REMOTE 应包含机场独有 key'
    for k in airport_only:
        assert k not in out, '机场独有 key %r 不应进入输出' % k
    # 同名 key 用本地的值
    assert out['port'] == 7890, out.get('port')
    # 顶层 key 列表（含顺序）与本地模板完全一致
    assert list(out) == list(LOCAL_TEMPLATE), list(out)
    print('[OK] 顶层 key：机场内容（节点除外）不进输出，顺序按本地配置')


def test_remote_content_groups_and_rules_discarded():
    """机场的代理组与规则整体丢弃（即使与本地组同名）。"""
    out = merge.merge_configs(_tpl(), dict(FAKE_REMOTE))
    groups = merge.names_of(out['proxy-groups'])
    assert groups == ['🏠 回家', '🚀 节点选择', '♻️ 自动选择', '🐟 漏网之鱼'], groups
    assert out['rules'] == LOCAL_TEMPLATE['rules'], out['rules']
    assert not any('google.com' in r for r in out['rules'])
    print('[OK] proxy-groups / rules：完全来自本地配置')


def test_proxies_local_first_remote_appended():
    template = _load_template()
    out = merge.merge_configs(template, dict(FAKE_REMOTE))
    names = merge.names_of(out['proxies'])
    # 本地节点在前，机场节点补充在后（本地模板未使用表达式）
    assert names == ['Home', 'Airport-HK', 'Airport-JP'], names
    print('[OK] proxies：本地节点在前，机场节点补充在后')


def test_proxy_groups_local_only_with_placeholder():
    out = merge.merge_configs(_tpl(), dict(FAKE_REMOTE))
    groups = {g['name']: g for g in out['proxy-groups']}
    # 表达式就地展开为机场节点名
    assert groups['🚀 节点选择']['proxies'] == \
        ['♻️ 自动选择', 'Airport-HK', 'Airport-JP', 'DIRECT'], groups['🚀 节点选择']
    assert groups['♻️ 自动选择']['proxies'] == ['Airport-HK', 'Airport-JP']
    # 本地自定义组不受影响
    assert groups['🏠 回家']['proxies'] == ['Home', 'DIRECT']
    print('[OK] proxy-groups：只取本地配置，表达式展开为机场节点')


def test_local_node_wins_over_remote_same_name():
    remote = {'proxies': [{'name': 'Home', 'type': 'ss', 'server': '9.9.9.9', 'port': 9}]}
    out = merge.merge_configs(_tpl(), remote)
    home = [p for p in out['proxies'] if p['name'] == 'Home']
    assert len(home) == 1 and home[0]['server'] == '10.0.0.1', home
    # 同名节点不算「机场带来的节点」，不会插进表达式位置
    groups = {g['name']: g for g in out['proxy-groups']}
    assert groups['♻️ 自动选择']['proxies'] == ['DIRECT'], groups['♻️ 自动选择']
    print('[OK] proxies：本地与机场同名节点，本地定义优先且不插入表达式位置')


def test_placeholder_absent_appends_nodes_and_warns():
    tpl = _tpl()
    # 去掉所有表达式
    for g in tpl['proxy-groups']:
        g['proxies'] = [m for m in g['proxies'] if m != '*']
    assert merge.uses_remote_placeholder(tpl) is False
    out = merge.merge_configs(tpl, dict(FAKE_REMOTE))
    names = merge.names_of(out['proxies'])
    assert names == ['Home', 'Airport-HK', 'Airport-JP'], names   # 节点定义仍保留
    for g in out['proxy-groups']:
        assert 'Airport-HK' not in (g.get('proxies') or []), g
    print('[OK] 未使用表达式：机场节点仍保留定义，但不进入任何代理组')


def test_placeholder_in_top_level_proxies():
    tpl = _tpl()
    tpl['proxies'] = ['*', {'name': 'Home', 'type': 'ss', 'server': '10.0.0.1', 'port': 1}]
    assert merge.uses_remote_placeholder(tpl) is True
    out = merge.merge_configs(tpl, dict(FAKE_REMOTE))
    assert merge.names_of(out['proxies']) == ['Airport-HK', 'Airport-JP', 'Home']
    print('[OK] 顶层 proxies 中的表达式：按位置插入机场节点')


def test_nodes_added_even_if_local_has_no_proxies_key():
    """本地没写 proxies 段时，机场节点定义仍须输出，否则组内引用会悬空。"""
    tpl = {'port': 7890,
           'proxy-groups': [{'name': 'G', 'type': 'select', 'proxies': ['*']}]}
    out = merge.merge_configs(tpl, dict(FAKE_REMOTE))
    assert merge.names_of(out['proxies']) == ['Airport-HK', 'Airport-JP']
    assert 'proxies' in list(out) and list(out)[-1] == 'proxies', list(out)
    print('[OK] 本地无 proxies 段：机场节点定义追加到末尾')


def test_empty_remote_expands_to_empty():
    """无机场节点时表达式展开为空：组内保留其余成员，整组为空则兜底 DIRECT。"""
    out = merge.merge_configs(_tpl(), None)
    groups = {g['name']: g for g in out['proxy-groups']}
    assert groups['🚀 节点选择']['proxies'] == ['♻️ 自动选择', 'DIRECT']
    assert groups['♻️ 自动选择']['proxies'] == ['DIRECT']
    assert '*' not in yaml.safe_dump(out, allow_unicode=True)
    print('[OK] 无机场节点：表达式展开为空，整组为空时兜底 DIRECT')


def test_empty_remote_returns_template():
    template = _load_template()
    out = merge.merge_configs(template, None)
    names = [g['name'] for g in out['proxy-groups']]
    assert '🚀 节点选择' in names and '🏠 回家' in names
    print('[OK] 空订阅：返回完整本地模板')


def test_base64_fallback():
    yaml_text = yaml.safe_dump(FAKE_REMOTE, allow_unicode=True)
    b64 = __import__('base64').b64encode(yaml_text.encode()).decode()
    parsed = merge.parse_clash(b64)
    assert parsed['port'] == 8888
    print('[OK] base64 兜底解析')


def test_find_dangling_rules():
    rules = [
        'DOMAIN-SUFFIX,a.com,机场组',      # 组不存在 → 悬空
        'MATCH,🐟 漏网之鱼',                # 本地组 → 正常
        'IP-CIDR,1.1.1.1/32,DIRECT',       # 内置策略 → 正常
        'DOMAIN-SUFFIX,b.com,Home',        # 指向节点名 → 正常
    ]
    dangling = merge.find_dangling_rules(rules, ['🐟 漏网之鱼'], ['Home'])
    assert dangling == ['DOMAIN-SUFFIX,a.com,机场组'], dangling
    print('[OK] find_dangling_rules：识别目标不存在的规则')


def test_find_dangling_members():
    """代理组引用了不存在的成员（含旧版表达式残留）应被识别。"""
    groups = [
        {'name': 'G1', 'type': 'select', 'proxies': ['Home', 'DIRECT']},
        {'name': 'G2', 'type': 'select', 'proxies': ['__REMOTE_PROXIES__', '不存在节点']},
        {'name': 'G3', 'type': 'select', 'proxies': ['G1', 'REJECT']},
    ]
    bad = merge.find_dangling_members(groups, ['G1', 'G2', 'G3'], ['Home'])
    assert bad == [('G2', '__REMOTE_PROXIES__'), ('G2', '不存在节点')], bad
    print('[OK] find_dangling_members：识别无效成员与旧表达式残留')


def test_full_convert_with_mock(monkeypatch):
    control = {
        'exclude_groups': ['🛑 全球拦截'],   # 已废弃，应被忽略且不影响输出
        'cache_ttl': 0,
    }
    template = _load_template()
    monkeypatch.setattr(main, 'fetch_remote',
                        lambda sub_url, ttl=0: (dict(FAKE_REMOTE), 'upload=1; download=2'))
    text, userinfo = main.convert('http://fake', control, template)
    out = yaml.safe_load(text)
    assert out['port'] == 7890                          # 本地有 port，用本地
    # 本地有 dns，机场的 dns 不得覆盖
    assert out['dns']['enhanced-mode'] == 'fake-ip', out['dns']
    assert out['dns']['nameserver'] != ['1.1.1.1']
    assert 'mixed-port' not in out                      # 机场独有 key 不进输出
    assert 'cfw-bypass' not in out
    assert merge.names_of(out['proxies']) == ['Home', 'Airport-HK', 'Airport-JP']
    groups = merge.names_of(out['proxy-groups'])
    assert '🛑 全球拦截' not in groups                    # 机场组一律丢弃
    assert '🏠 回家' in groups and '🚀 节点选择' in groups
    assert not any('google.com' in r for r in out['rules'])
    print('[OK] convert 全链路（mock 机场）：本地配置 + 机场节点')


class _FakeResp:
    """模拟 requests.Response 的最小对象。"""
    def __init__(self, status_code, text, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def test_fetch_remote_success(monkeypatch):
    payload = yaml.safe_dump(
        {'port': 7890, 'proxies': [{'name': 'A', 'type': 'ss', 'server': '1.1.1.1', 'port': 1}]},
        allow_unicode=True)
    monkeypatch.setattr(
        main.requests, 'get',
        lambda *a, **k: _FakeResp(200, payload, {'subscription-userinfo': 'upload=1'}))
    main.remote_cache.clear()
    data, ui = main.fetch_remote('http://fake', ttl=0)
    assert data['port'] == 7890 and data['proxies'][0]['name'] == 'A'
    assert ui == 'upload=1'
    assert 'http://fake' in main.remote_cache          # 已写入缓存
    print('[OK] fetch_remote：拉取成功、解析并缓存')


def test_fetch_remote_always_fetches_even_with_fresh_cache(monkeypatch):
    """网络优先：即使缓存新鲜，也必须重新拉取机场配置。"""
    main.remote_cache['http://fake'] = {
        'ts': time.time(),
        'data': {'proxies': [{'name': 'CACHED'}]},
        'userinfo': 'cached'}
    payload = yaml.safe_dump(
        {'port': 7890, 'proxies': [{'name': 'FRESH', 'type': 'ss', 'server': '1.1.1.1', 'port': 1}]},
        allow_unicode=True)
    calls = []

    def _fake_get(*args, **kwargs):
        calls.append(kwargs)
        return _FakeResp(200, payload, {'subscription-userinfo': 'upload=9'})

    monkeypatch.setattr(main.requests, 'get', _fake_get)
    data, ui = main.fetch_remote('http://fake', ttl=3600)
    assert calls, '有新鲜缓存时仍应发起拉取'
    assert data['proxies'][0]['name'] == 'FRESH'        # 用最新配置而非缓存
    assert ui == 'upload=9'                             # userinfo 来自当次拉取
    assert main.remote_cache['http://fake']['data']['proxies'][0]['name'] == 'FRESH'
    print('[OK] fetch_remote：网络优先，新鲜缓存也重新拉取并更新缓存')


def test_fetch_remote_fallback_fresh_cache(monkeypatch):
    """拉取失败：回退仍在 cache_ttl 有效期内的缓存。"""
    main.remote_cache['http://fake'] = {
        'ts': time.time(),
        'data': {'proxies': [{'name': 'CACHED'}]},
        'userinfo': 'cached'}
    monkeypatch.setattr(main.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(Exception('net down')))
    data, ui = main.fetch_remote('http://fake', ttl=3600)
    assert data == {'proxies': [{'name': 'CACHED'}]} and ui == 'cached'
    print('[OK] fetch_remote：拉取失败回退有效期内的缓存')


def test_fetch_remote_expired_cache_not_used(monkeypatch):
    """拉取失败且缓存已超出 cache_ttl：不作回退，交由调用方退化为本地模板。"""
    main.remote_cache['http://fake'] = {
        'ts': 0, 'data': {'proxies': [{'name': 'STALE'}]}, 'userinfo': 'stale'}
    monkeypatch.setattr(main.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(Exception('network down')))
    data, ui = main.fetch_remote('http://fake', ttl=3600)
    assert data is None and ui == ''
    print('[OK] fetch_remote：缓存超出 cache_ttl 时不回退，返回 (None, "")')


def test_fetch_remote_fail_no_cache(monkeypatch):
    main.remote_cache.clear()
    monkeypatch.setattr(main.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(Exception('network down')))
    data, ui = main.fetch_remote('http://fake', ttl=0)
    assert data is None and ui == ''
    print('[OK] fetch_remote：失败且无缓存返回 (None, "")')


def test_output_key_order_follows_local():
    """输出顶层 key 顺序完全按本地配置（机场顺序不参与）。"""
    template = _load_template()
    remote = {
        'mixed-port': 7893,
        'dns': {'enable': True, 'nameserver': ['8.8.8.8']},
        'port': 8888,
        'proxies': [{'name': 'HK', 'type': 'ss', 'server': '1.1.1.1', 'port': 1}],
        'proxy-groups': [{'name': '🚀 节点选择', 'type': 'select', 'proxies': ['HK', 'DIRECT']}],
        'rules': ['MATCH,🐟 漏网之鱼'],
    }
    out = merge.merge_configs(template, remote)
    assert list(out) == list(template), (list(out), list(template))
    assert 'mixed-port' not in out
    assert out['port'] == 7890 and out['dns']['enhanced-mode'] == 'fake-ip'
    print('[OK] 输出 key 顺序：完全按本地配置')


def test_build_minimal_config_is_local_only():
    """第 3 步：最小配置文件只含本地节点与本地代理组 / 规则，不含机场内容。"""
    template = _load_template()
    minimal = main.build_minimal_config(template)
    proxies = [p['name'] for p in minimal['proxies']]
    groups = [g['name'] for g in minimal['proxy-groups']]
    assert proxies == ['Home'], proxies
    assert '🏠 回家' in groups and '🚀 节点选择' in groups
    assert minimal['port'] == 7890
    # 深拷贝：不得与模板共享可变对象
    minimal['proxies'][0]['server'] = 'x'
    assert template['proxies'][0]['server'] != 'x'
    print('[OK] build_minimal_config：最小配置仅含本地节点')


def test_convert_no_sub_url_returns_minimal_config():
    """第 1 步无订阅地址：跳过拉取，直接由第 3 步生成最小配置并走第 4 步转换。"""
    control = {'cache_ttl': 3600}
    template = _load_template()
    text, userinfo = main.convert('', control, template)
    out = yaml.safe_load(text)
    assert merge.names_of(out['proxies']) == ['Home']
    assert userinfo == ''
    assert '🏠 回家' in merge.names_of(out['proxy-groups'])
    groups = {g['name']: g for g in out['proxy-groups']}
    # 无机场节点时表达式展开为空，整组兜底 DIRECT（不得残留字面量 *）
    assert groups['♻️ 自动选择']['proxies'] == ['DIRECT'], groups['♻️ 自动选择']
    all_members = [m for g in out['proxy-groups'] for m in (g.get('proxies') or [])]
    assert '*' not in all_members, all_members
    print('[OK] convert：sub_url 为空 → 最小配置文件（表达式展开为空）')


def test_convert_fetch_fail_no_cache_returns_minimal_config(monkeypatch):
    """第 1 步失败且第 2 步缓存未命中：退化为第 3 步的最小配置文件。"""
    main.remote_cache.clear()
    monkeypatch.setattr(main.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(Exception('network down')))
    control = {'cache_ttl': 3600}
    template = _load_template()
    text, userinfo = main.convert('http://fake', control, template)
    out = yaml.safe_load(text)
    assert merge.names_of(out['proxies']) == ['Home']
    assert userinfo == ''
    assert out['port'] == 7890
    print('[OK] convert：拉取失败且无缓存 → 最小配置文件')


def test_convert_fetch_fail_falls_back_to_cache(monkeypatch):
    """第 1 步失败但有有效缓存：用缓存内容走第 4 步转换，并透传缓存里的 userinfo。"""
    main.remote_cache.clear()
    main.remote_cache['http://fake'] = {
        'ts': time.time(),
        'data': {'proxies': [{'name': 'CACHED-HK', 'type': 'ss', 'server': '1.1.1.1', 'port': 1}],
                 'rules': ['MATCH,DIRECT'],
                 'dns': {'enable': True, 'nameserver': ['9.9.9.9']}},
        'userinfo': 'upload=8; download=88'}
    monkeypatch.setattr(main.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(Exception('network down')))
    control = {'cache_ttl': 3600}
    template = _load_template()
    text, userinfo = main.convert('http://fake', control, template)
    out = yaml.safe_load(text)
    assert merge.names_of(out['proxies']) == ['Home', 'CACHED-HK']
    assert userinfo == 'upload=8; download=88'
    # 缓存里的机场规则与 dns 同样丢弃
    assert not any(r == 'MATCH,DIRECT' for r in out['rules'])
    assert out['dns']['nameserver'] != ['9.9.9.9']
    print('[OK] convert：拉取失败回退缓存 → 仍走合并')


def test_resolve_sub_url_by_index():
    """多订阅地址：?sub_url=N 选择 sub_urlN，未传/为空/越界回退 sub_url。"""
    control = {'sub_url': 'https://default',
               'sub_url1': 'https://one',
               'sub_url3': 'https://three'}
    assert main.resolve_sub_url(None, control) == 'https://default'
    assert main.resolve_sub_url('', control) == 'https://default'
    assert main.resolve_sub_url('1', control) == 'https://one'
    assert main.resolve_sub_url('3', control) == 'https://three'
    assert main.resolve_sub_url(' 1 ', control) == 'https://one'
    assert main.resolve_sub_url('2', control) == 'https://default'
    assert main.resolve_sub_url('0', control) == 'https://default'
    assert main.resolve_sub_url('9', control) == 'https://default'
    assert main.resolve_sub_url('https://raw.example/sub', control) == 'https://raw.example/sub'
    assert main.resolve_sub_url('3', {}) == ''
    print('[OK] resolve_sub_url：序号选择 / 回退 / 直传地址')


def test_sub_url_keys_are_control_keys():
    """sub_url 与 sub_url1~5 都是控制项，不得泄漏进输出的 Clash 配置。"""
    assert main.SUB_URL_KEYS == ['sub_url', 'sub_url1', 'sub_url2', 'sub_url3',
                                 'sub_url4', 'sub_url5'], main.SUB_URL_KEYS
    for k in main.SUB_URL_KEYS:
        assert k in main.CONTROL_KEYS, k
    _, template = main.load_local_config()
    leaked = [k for k in main.SUB_URL_KEYS if k in template]
    assert not leaked, leaked
    print('[OK] 控制项：sub_url1~5 已从模板中剥离')


def test_deprecated_control_keys_not_leaked(tmp_path):
    """已废弃的 remove_keys / exclude_groups / merge_groups 仍须被剥离，不得泄漏进输出配置。"""
    p = tmp_path / 'config.yaml'
    p.write_text(
        'password: pw\n'
        'remove_keys:\n  - dns\n'
        'exclude_groups:\n  - 🛑 全球拦截\n'
        'merge_groups:\n  - target: A\n'
        'port: 7890\n',
        encoding='utf-8')
    control, template = main.load_local_config(str(p))
    for k in ('remove_keys', 'exclude_groups', 'merge_groups'):
        assert k in control, k
    assert template == {'port': 7890}, template
    print('[OK] 控制项：已废弃的 remove_keys / exclude_groups / merge_groups 已剥离')


def test_api_selects_subscription_by_index(monkeypatch):
    """api() 集成：请求 ?sub_url=N 时把对应地址交给 convert()。"""
    control = {'password': 'pw', 'sub_url': 'http://default',
               'sub_url2': 'http://two', 'cache_ttl': 0}
    template = _load_template()
    monkeypatch.setattr(main, 'load_local_config', lambda *a, **k: (control, template))
    seen = []

    def _fake_convert(url, c, t):
        seen.append(url)
        return 'proxies: []', ''

    monkeypatch.setattr(main, 'convert', _fake_convert)
    client = main.app.test_client()

    assert client.get(main.API_PATH, query_string={'password': 'pw'}).status_code == 200
    client.get(main.API_PATH, query_string={'password': 'pw', 'sub_url': '2'})
    client.get(main.API_PATH, query_string={'password': 'pw', 'sub_url': '5'})
    client.get(main.API_PATH, query_string={'password': 'pw', 'sub_url': 'http://direct'})
    assert seen == ['http://default', 'http://two', 'http://default', 'http://direct'], seen
    # 密码错误时不进入转换
    assert client.get(main.API_PATH, query_string={'password': 'bad'}).get_data(as_text=True) \
        == 'Hello World!'
    assert len(seen) == 4
    print('[OK] api：按 ?sub_url=N 选择订阅地址（未配置则回退默认）')


if __name__ == '__main__':
    test_remote_top_level_keys_never_enter_output()
    test_remote_content_groups_and_rules_discarded()
    test_proxies_local_first_remote_appended()
    test_proxy_groups_local_only_with_placeholder()
    test_local_node_wins_over_remote_same_name()
    test_placeholder_absent_appends_nodes_and_warns()
    test_placeholder_in_top_level_proxies()
    test_nodes_added_even_if_local_has_no_proxies_key()
    test_empty_remote_expands_to_empty()
    test_empty_remote_returns_template()
    test_base64_fallback()
    test_find_dangling_rules()
    test_find_dangling_members()
    test_output_key_order_follows_local()
    test_build_minimal_config_is_local_only()
    test_convert_no_sub_url_returns_minimal_config()
    test_resolve_sub_url_by_index()
    test_sub_url_keys_are_control_keys()
    # 以下测试依赖 pytest 的 monkeypatch / requests mock，用 pytest 运行：
    # test_full_convert_with_mock / test_fetch_remote_* / test_convert_fetch_fail_*
    # test_deprecated_control_keys_not_leaked / test_api_selects_subscription_by_index
    print('\n全部基础测试通过 ✅')
