"""合并逻辑测试：覆盖「本地配置为准」的模型（除代理节点外只使用本地配置、
表达式 `*` 展开、顶层 key 与顺序完全取自本地）、缓存优先 + 后台加载、
空 sub_url、多订阅地址等场景。"""
import copy
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import merge
import main


# 测试期间把缓存文件指到临时目录，避免污染仓库里的 sub_cache.json
_TMP_CACHE = os.path.join(tempfile.gettempdir(), '_mysubconvert_test_cache.json')
main.SUBCACHE_FILE = _TMP_CACHE
main.remote_cache.clear()


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
        'cache_ttl': 60,
    }
    template = _load_template()
    monkeypatch.setattr(main, 'refresh_subscription',
                        lambda sub_url, c: (dict(FAKE_REMOTE), 'upload=1; download=2'))
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
    monkeypatch.setattr(main, 'SUBCACHE_FILE', _TMP_CACHE)
    monkeypatch.setattr(
        main.requests, 'get',
        lambda *a, **k: _FakeResp(200, payload, {'subscription-userinfo': 'upload=1'}))
    main.remote_cache.clear()
    data, ui = main.fetch_remote('http://fake')
    assert data['port'] == 7890 and data['proxies'][0]['name'] == 'A'
    assert ui == 'upload=1'
    assert 'http://fake' in main.remote_cache          # 已写入缓存
    print('[OK] fetch_remote：拉取成功、解析并缓存')


def test_cache_written_is_permanent():
    """缓存一经写入即永久有效：age 再大也不会被丢弃。"""
    main.remote_cache.clear()
    main.remote_cache['http://fake'] = {
        'ts': time.time() - 86400,      # 一天前
        'data': {'proxies': [{'name': 'OLD'}]},
        'userinfo': 'old'}
    data, ui, age = main.get_cached('http://fake')
    assert data is not None and data['proxies'][0]['name'] == 'OLD'
    assert age > 86000
    print('[OK] 缓存永不失效：超长 age 依然可读')


def test_refresh_uses_fresh_cache_without_loading(monkeypatch):
    """缓存新鲜（age < cache_ttl）时直接返回，不触发加载。"""
    main.remote_cache.clear()
    main.remote_cache['http://fake'] = {
        'ts': time.time(), 'data': {'proxies': [{'name': 'CACHED'}]},
        'userinfo': 'cached'}
    calls = []
    monkeypatch.setattr(main, 'trigger_load', lambda url: calls.append(url))
    data, ui = main.refresh_subscription('http://fake', {'cache_ttl': 60})
    assert data['proxies'][0]['name'] == 'CACHED' and ui == 'cached'
    assert calls == [], '新鲜缓存不应触发加载'
    print('[OK] refresh_subscription：缓存新鲜直接返回，不触发加载')


def test_refresh_expired_cache_triggers_load(monkeypatch):
    """缓存超过 cache_ttl：触发加载并返回新内容。"""
    main.remote_cache.clear()
    main.remote_cache['http://fake'] = {
        'ts': time.time() - 120, 'data': {'proxies': [{'name': 'OLD'}]},
        'userinfo': 'old'}
    calls = []

    def fake_load(sub_url):
        # 模拟加载线程：把新内容写进缓存（不改变内存里 dict 的其它引用）
        calls.append(sub_url)
        main.remote_cache[sub_url] = {
            'ts': time.time(), 'data': {'proxies': [{'name': 'NEW'}]},
            'userinfo': 'new'}
        return True

    monkeypatch.setattr(main, 'trigger_load', fake_load)
    monkeypatch.setattr(main, 'wait_load', lambda url, t=main.LOAD_WAIT_TIMEOUT: True)
    data, ui = main.refresh_subscription('http://fake', {'cache_ttl': 60})
    assert calls == ['http://fake'], '过期缓存应触发加载'
    assert data['proxies'][0]['name'] == 'NEW' and ui == 'new'
    print('[OK] refresh_subscription：缓存过期触发加载，返回新内容')


def test_refresh_timeout_falls_back_to_old_cache(monkeypatch):
    """加载超时（3 秒内未完成）：返回原缓存，不报错。"""
    main.remote_cache.clear()
    main.remote_cache['http://fake'] = {
        'ts': time.time() - 120, 'data': {'proxies': [{'name': 'OLD'}]},
        'userinfo': 'old'}
    monkeypatch.setattr(main, 'trigger_load', lambda url: True)
    monkeypatch.setattr(main, 'wait_load', lambda url, t=main.LOAD_WAIT_TIMEOUT: False)
    data, ui = main.refresh_subscription('http://fake', {'cache_ttl': 60})
    assert data['proxies'][0]['name'] == 'OLD' and ui == 'old'
    print('[OK] refresh_subscription：加载超时回退旧缓存')


def test_refresh_no_cache_triggers_load(monkeypatch):
    """无任何缓存：触发加载并等待，拿到内容即返回。"""
    main.remote_cache.clear()
    monkeypatch.setattr(main, 'trigger_load', lambda url: True)
    monkeypatch.setattr(main, 'wait_load', lambda url, t=main.LOAD_WAIT_TIMEOUT: True)
    main.remote_cache['http://fake'] = {
        'ts': time.time(), 'data': {'proxies': [{'name': 'LOADED'}]}, 'userinfo': 'u'}

    def fake_load(url):
        return True

    monkeypatch.setattr(main, 'trigger_load', fake_load)
    data, ui = main.refresh_subscription('http://fake', {'cache_ttl': 60})
    assert data['proxies'][0]['name'] == 'LOADED'
    print('[OK] refresh_subscription：无缓存触发加载并返回')


def test_refresh_no_cache_no_subscription_returns_none(monkeypatch):
    """无缓存且加载失败：返回 (None, '')，交由调用方生成最小配置。"""
    main.remote_cache.clear()
    monkeypatch.setattr(main, 'trigger_load', lambda url: True)
    monkeypatch.setattr(main, 'wait_load',
                        lambda url, t=main.LOAD_WAIT_TIMEOUT: True)
    data, ui = main.refresh_subscription('http://fake', {'cache_ttl': 60})
    assert data is None and ui == ''
    print('[OK] refresh_subscription：无缓存且加载无果返回 (None, "")')


def test_trigger_load_dedup(monkeypatch):
    """同一订阅同时只允许一个加载线程：_loading 已有记录时不重复触发。"""
    main.remote_cache.clear()
    started = []

    def fake_fetch(sub_url):
        started.append(sub_url)
        time.sleep(0.3)

    monkeypatch.setattr(main, '_fetch_and_cache', fake_fetch)
    main._loading.clear()
    main._load_done.clear()
    assert main.trigger_load('http://dup') is True
    assert main.trigger_load('http://dup') is False, '加载中不应重复触发'
    main.wait_load('http://dup', timeout=2)
    assert started == ['http://dup'], started
    print('[OK] trigger_load：同一订阅加载中不重复触发')


def test_wait_load_returns_true_when_already_finished():
    """加载已结束（事件已摘除）时 wait_load 必须按「已完成」处理，不能误报超时。"""
    main._loading.clear()
    main._load_done.clear()
    assert main.wait_load('http://none', timeout=0.01) is True
    print('[OK] wait_load：无待等事件视为已完成（避免误报 3 秒超时）')


def test_cache_file_roundtrip(monkeypatch, tmp_path):
    """缓存落盘后可完整恢复（重启不丢）。"""
    path = str(tmp_path / 'sub_cache.json')
    monkeypatch.setattr(main, 'SUBCACHE_FILE', path)
    main.remote_cache.clear()
    main.remote_cache['http://fake'] = {
        'ts': 123.0, 'data': {'proxies': [{'name': 'PERSIST'}]}, 'userinfo': 'ui'}
    main._save_cache_file()
    main.remote_cache.clear()
    main._load_cache_file()
    assert main.remote_cache['http://fake']['data']['proxies'][0]['name'] == 'PERSIST'
    assert main.remote_cache['http://fake']['userinfo'] == 'ui'
    print('[OK] 缓存文件：写入后可完整恢复')


def test_control_int_fallback():
    """cache_ttl 非法 / 缺失 / 非正数时回退默认值。"""
    assert main.control_int({'cache_ttl': 90}, 'cache_ttl', 60) == 90
    assert main.control_int({'cache_ttl': '90'}, 'cache_ttl', 60) == 90
    assert main.control_int({}, 'cache_ttl', 60) == 60
    assert main.control_int({'cache_ttl': 'abc'}, 'cache_ttl', 60) == 60
    assert main.control_int({'cache_ttl': 0}, 'cache_ttl', 60) == 60
    assert main.control_int({'cache_ttl': -5}, 'cache_ttl', 60) == 60
    print('[OK] control_int：非法值回退默认')


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
    """加载失败且无任何缓存：退化为最小配置文件。"""
    main.remote_cache.clear()
    monkeypatch.setattr(main, 'SUBCACHE_FILE', _TMP_CACHE)
    monkeypatch.setattr(main.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(Exception('network down')))
    control = {'cache_ttl': 60}
    template = _load_template()
    text, userinfo = main.convert('http://fake', control, template)
    out = yaml.safe_load(text)
    assert merge.names_of(out['proxies']) == ['Home']
    assert userinfo == ''
    assert out['port'] == 7890
    print('[OK] convert：加载失败且无缓存 → 最小配置文件')


def test_convert_fetch_fail_falls_back_to_cache(monkeypatch):
    """加载失败但缓存永久有效：依然用缓存内容走合并，并透传缓存里的 userinfo。"""
    main.remote_cache.clear()
    main.remote_cache['http://fake'] = {
        'ts': time.time() - 86400,          # 一天前的缓存：仍然有效
        'data': {'proxies': [{'name': 'CACHED-HK', 'type': 'ss', 'server': '1.1.1.1', 'port': 1}],
                 'rules': ['MATCH,DIRECT'],
                 'dns': {'enable': True, 'nameserver': ['9.9.9.9']}},
        'userinfo': 'upload=8; download=88'}
    monkeypatch.setattr(main, 'SUBCACHE_FILE', _TMP_CACHE)
    monkeypatch.setattr(main.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(Exception('network down')))
    control = {'cache_ttl': 60}
    template = _load_template()
    text, userinfo = main.convert('http://fake', control, template)
    out = yaml.safe_load(text)
    assert merge.names_of(out['proxies']) == ['Home', 'CACHED-HK']
    assert userinfo == 'upload=8; download=88'
    # 缓存里的机场规则与 dns 同样丢弃
    assert not any(r == 'MATCH,DIRECT' for r in out['rules'])
    assert out['dns']['nameserver'] != ['9.9.9.9']
    print('[OK] convert：加载失败回退永久缓存 → 仍走合并')


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
               'sub_url2': 'http://two', 'cache_ttl': 60}
    template = _load_template()
    monkeypatch.setattr(main, 'load_local_config', lambda *a, **k: (control, template))
    seen = []

    def _fake_convert(url, c, t):
        seen.append(url)
        return 'proxies: []', ''

    monkeypatch.setattr(main, 'convert', _fake_convert)
    monkeypatch.setattr(main, 'refresh_proxy_ip_port', lambda c: None)
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


# ===================== 网页配置界面：文本块读写 =====================
_UI_SAMPLE = (
    '# 文件头注释\n'
    '\n'
    'password: old-pw          # 访问密码\n'
    'cache_ttl: 60\n'
    'port: 7890\n'
    'sub_url: https://example.com/sub   # 默认订阅\n'
    'sub_url1: \"\"\n'
    'proxies:\n'
    '  - name: A\n'
    '    type: ss\n'
    'proxy-groups:\n'
    '  - name: G1\n'
    '    type: select\n'
    '    proxies:\n'
    '      - \"Home\"\n'
    'rules:\n'
    '  - DOMAIN-SUFFIX,example.com,G1   # 走代理\n'
    '  - MATCH,DIRECT\n'
)
# _UI_SAMPLE 里的界面密码，测试登录用
_UI_SAMPLE_PW = 'old-pw'


def ui_login(client, password=None):
    """登录网页界面，返回已带 cookie 的 client（登录失败则抛错）。

    界面已暴露公网，/ui 与 /ui/save 都要求登录；测试里必须先过这一关，
    否则拿到的是 302 跳登录页，断言会以各种奇怪的方式失败。
    """
    r = client.post('/ui/login', data={'password': password or _UI_SAMPLE_PW})
    assert r.status_code == 302, '登录失败: %s' % r.status_code
    return client


def ui_client(cfg_path, monkeypatch, password=None):
    """按测试配置起一个已登录的 test_client。"""
    monkeypatch.setattr(main, 'CONFIG_FILE', str(cfg_path))
    monkeypatch.setattr(main.load_local_config, '__defaults__', (str(cfg_path),))
    main.invalidate_config()
    client = main.app.test_client()
    return ui_login(client, password)


def ui_page_html():
    """取一份已登录的 /ui 页面 HTML（供纯前端逻辑测试用，不关心配置内容）。

    这里依赖当前生效的配置（可能是 monkeypatch 后的临时配置），
    密码从该配置里现读，避免写死某个样例口令。
    """
    client = main.app.test_client()
    pod = main._ui_password()
    assert pod, '当前配置没有 password，无法登录'
    ui_login(client, pod)
    return client.get('/ui').get_data(as_text=True)


def test_ui_requires_login(tmp_path, monkeypatch):
    """未登录时 /ui 跳登录页、/ui/save 返回 401；密码错不给 cookie。"""
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_UI_SAMPLE, encoding='utf-8')
    monkeypatch.setattr(main, 'CONFIG_FILE', str(cfg))
    monkeypatch.setattr(main.load_local_config, '__defaults__', (str(cfg),))
    main.invalidate_config()
    client = main.app.test_client()          # 刻意**不**登录

    r = client.get('/ui')
    assert r.status_code == 302 and '/ui/login' in r.headers.get('Location', '')
    r2 = client.post('/ui/save', json={'blocks': {'rules': 'rules:\n'}})
    assert r2.status_code == 401, r2.status_code

    # 密码错：403 且不发 cookie
    r3 = client.post('/ui/login', data={'password': 'wrong'})
    assert r3.status_code == 403
    assert main.UI_SESSION_COOKIE not in r3.headers.get('Set-Cookie', '')
    # 仍然进不去
    assert client.get('/ui').status_code == 302

    # 密码对：发 cookie，页面可访问
    ui_login(client)
    html = client.get('/ui').get_data(as_text=True)
    assert 'MySubConvert 配置' in html

    # 伪造 cookie 无效
    client.set_cookie(main.UI_SESSION_COOKIE, '9999999999.deadbeef')
    assert client.get('/ui').status_code == 302
    print('[OK] 界面登录：未登录跳转、错密码 403、伪造 cookie 无效')


def test_ui_login_redirects_and_logout(tmp_path, monkeypatch):
    """登录后跳回原页面（限站内）；登出后立刻失效。"""
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_UI_SAMPLE, encoding='utf-8')
    client = ui_client(cfg, monkeypatch)

    r = client.post('/ui/login', data={'password': _UI_SAMPLE_PW, 'next': '/ui'})
    assert r.status_code == 302 and r.headers['Location'] == '/ui'
    # 站外跳转必须被拒（防开放重定向）
    r2 = client.post('/ui/login', data={'password': _UI_SAMPLE_PW,
                                        'next': 'https://evil.example/x'})
    assert r2.headers['Location'] == '/ui', r2.headers['Location']
    client.get('/ui/logout')
    assert client.get('/ui').status_code == 302
    print('[OK] 登录跳转限站内；登出后失效')


def test_ui_password_change_takes_effect(tmp_path, monkeypatch):
    """改掉 config.yaml 的 password 后，旧密码立即失效、新密码可用（热重载）。"""
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_UI_SAMPLE, encoding='utf-8')
    client = ui_client(cfg, monkeypatch)
    assert client.get('/ui').status_code == 200

    cfg.write_text(main.assemble_config(
        cfg.read_text(encoding='utf-8'), {'password': 'new-pw'}),
        encoding='utf-8')
    main.invalidate_config()

    fresh = main.app.test_client()
    assert fresh.post('/ui/login', data={'password': _UI_SAMPLE_PW}).status_code == 403
    assert fresh.post('/ui/login', data={'password': 'new-pw'}).status_code == 302
    print('[OK] 改密码后旧密码失效、新密码生效（无需重启）')


def test_split_config_blocks():
    """按顶层 key 切块：注释与缩进子项跟随所属段。"""
    blocks = main.split_config_blocks(_UI_SAMPLE)
    assert blocks['__head__'].startswith('# 文件头注释')
    assert set(blocks) == {'__head__', 'password', 'cache_ttl', 'port',
                           'sub_url', 'sub_url1', 'proxies', 'proxy-groups',
                           'rules'}
    assert blocks['password'].startswith('password: old-pw')
    assert '# 访问密码' in blocks['password']
    assert blocks['proxies'].startswith('proxies:\n  - name: A')
    assert blocks['rules'].startswith('rules:\n')
    print('[OK] split_config_blocks：按顶层 key 切块，注释随段')


def test_assemble_is_idempotent():
    """无改动时逐字还原（不重排、不丢注释）。"""
    assert main.assemble_config(_UI_SAMPLE, {}) == _UI_SAMPLE
    print('[OK] assemble_config：无改动时逐字还原')


def test_assemble_line_keeps_comment():
    """单行段只换值，保留行尾注释。"""
    out = main.assemble_config(_UI_SAMPLE, {'sub_url': 'https://h/new'})
    line = [l for l in out.splitlines() if l.startswith('sub_url:')][0]
    assert line.startswith('sub_url: https://h/new')
    assert line.endswith('# 默认订阅'), repr(line)
    assert '# 访问密码' in out              # 其他行的注释不受影响
    print('[OK] assemble_config：单行换值保留注释')


_QUOTED_SAMPLE = (
    'password: "12345678"          # 访问密码\n'
    'sub_url: "https://a.c/subscribe1"   # 默认订阅\n'
    'sub_url1: "https://a.c/subscribe2"\n'
    'sub_url2: ""\n'
    'server_url: http://127.0.0.1:8080\n'
    'cache_ttl: 60\n'
)


def test_ui_line_value_hides_quotes():
    """界面读到的单行值不该带 YAML 引号。

    真实反馈：「订阅地址输入框现在会显示字符串的双引号」。根因是
    `_line_value` 只剥了行尾注释、没剥引号，界面上就显示成
    `"https://a.c/subscribe1"`——用户会以为引号是地址的一部分。
    """
    payload = main.ui_blocks_payload(_QUOTED_SAMPLE)
    assert payload['sub_url'] == 'https://a.c/subscribe1', payload['sub_url']
    assert payload['sub_url1'] == 'https://a.c/subscribe2'
    assert payload['sub_url2'] == ''                 # `""` 在界面上就是空
    for k in ('sub_url', 'sub_url1', 'sub_url2'):
        assert '"' not in payload[k], '%s 仍带引号: %r' % (k, payload[k])
    print('[OK] 界面读取：单行值已剥引号，不显示双引号')


def test_assemble_single_line_quotes_only_when_needed():
    """单行段写回：该加引号时才加，不该加时保持干净。"""
    out = main.assemble_config(_QUOTED_SAMPLE, {
        'sub_url': 'https://a.c/subscribe1',      # 普通 URL：裸写
        'sub_url1': 'https://a.c/subscribe2',
        'sub_url2': '',                            # 空值：必须写成 ""
    })
    lines = dict(l.split(': ', 1) for l in out.splitlines() if ': ' in l)
    # 普通 URL 不带引号——写出来的配置比原文还干净
    assert lines['sub_url'].startswith('https://a.c/subscribe1'), lines['sub_url']
    assert not lines['sub_url'].startswith('"'), lines['sub_url']
    assert lines['sub_url'].endswith('# 默认订阅')     # 注释仍在
    # 空值要显式写成 ""，不能留个空尾巴
    assert lines['sub_url2'] == '""', repr(lines['sub_url2'])

    parsed = yaml.safe_load(out)
    assert parsed['sub_url'] == 'https://a.c/subscribe1'
    assert parsed['sub_url1'] == 'https://a.c/subscribe2'
    assert parsed['sub_url2'] == ''
    print('[OK] 单行写回：按需加引号，空值写 ""')


def test_assemble_single_line_quotes_dangerous_values():
    """会改变语义的值必须加引号，否则配置被静默改坏。"""
    cases = [
        # (值, 说明)
        ('https://x.c/s?a=1#frag', '`#` 会被当注释起点 → 值被截断'),
        ('a,b', '普通串，本身安全（不该被引，反向断言在下面）'),
        ('true', '裸写会变成布尔 True'),
        ('no', '裸写会变成布尔 False'),
        ('null', '裸写会变成 None'),
        ('a: b', '裸写直接解析失败'),
        (' tab', '首尾空白会被吃掉'),
    ]
    for val, why in cases:
        out = main.assemble_config(_QUOTED_SAMPLE, {'sub_url': val})
        line = [l for l in out.splitlines() if l.startswith('sub_url:')][0]
        try:
            parsed = yaml.safe_load(out)
        except Exception as exc:                    # noqa: BLE001
            raise AssertionError('%r 写出的配置无法解析（%s）:\n%s'
                                 % (val, why, out)) from exc
        assert parsed['sub_url'] == val, \
            '%r 语义被改变（%s）→ %r\n%s' % (val, why, parsed['sub_url'], line)

    # 反向确认：逗号/撇号这类不需要引号，别把它引起来（引号噪音）
    out = main.assemble_config(_QUOTED_SAMPLE, {'sub_url': 'a,b'})
    line = [l for l in out.splitlines() if l.startswith('sub_url:')][0]
    assert line.startswith('sub_url: a,b'), line
    print('[OK] 单行写回：危险值加引号、安全值不引')


def test_yaml_scalar_roundtrip_matrix():
    """`_yaml_scalar` 对一批边界值都必须能原样读回（且类型不变）。

    唯一例外是**看起来像数字的串**：它们裸写、读回成 int/float——这是刻意的，
    `port: 8388` 必须是数字，给数字加引号反而是错（Clash 会拿到字符串）。
    """
    values = [
        'https://a.c/subscribe1', 'https://a.c/sub?token=x#y',
        'https://a.c/sub?a=1&b=2', 'has,comma', "quo'te", 'dq"uote',
        'My Node', '中文名字', 'emoji 🐟', '🤖 Github', '*',
        'DIRECT', 'MATCH,DIRECT', 'a.b', 'a/b', 'ss',
        'true', 'false', 'yes', 'no', 'on', 'off', 'null', '~', '',
        '  lead', 'trail  ', 'a # c', 'a: b',
        'tab\there', 'multi\nline', '#开头', '&anchor',
    ]
    for v in values:
        out = main._yaml_scalar(v)
        try:
            got = yaml.safe_load('k: ' + out)['k']
        except Exception as exc:                    # noqa: BLE001
            raise AssertionError('值 %r 渲染成 %r 后无法解析: %s'
                                 % (v, out, exc)) from exc
        assert got == v and isinstance(got, str), \
            '值 %r 渲染成 %r 后读回成 %r（类型 %s）' % (
                v, out, got, type(got).__name__)

    # 数字串裸写是**有意为之**：读回成数字才是对的（port 不能是字符串）
    for v, want in (('8388', 8388), ('1.5', 1.5), ('0x1f', 31)):
        out = main._yaml_scalar(v)
        assert '"' not in out, '数字 %r 不该被加引号: %r' % (v, out)
        assert yaml.safe_load('k: ' + out)['k'] == want
    # 真·bool 值（不是字符串）照样裸写
    assert main._yaml_scalar(True) == 'true'
    assert main._yaml_scalar(8080) == '8080'
    print('[OK] _yaml_scalar：%d 个边界值原样读回，数字按原生类型输出'
          % len(values))


def test_assemble_multiline_block():
    """多行段整体替换，其他段不受影响。"""
    new_rules = ('rules:\n'
                 '  - DOMAIN-SUFFIX,foo.com,G1\n'
                 '  - MATCH,DIRECT\n')
    out = main.assemble_config(_UI_SAMPLE, {'rules': new_rules})
    parsed = yaml.safe_load(out)
    assert parsed['rules'] == ['DOMAIN-SUFFIX,foo.com,G1', 'MATCH,DIRECT']
    assert parsed['port'] == 7890 and parsed['password'] == 'old-pw'
    assert out.startswith('# 文件头注释')
    print('[OK] assemble_config：多行段整体替换，其他段完好')


def test_assemble_appends_new_block():
    """原文没有的段追加到末尾。"""
    out = main.assemble_config(_UI_SAMPLE, {'tun': 'tun:\n  enable: true\n'})
    assert yaml.safe_load(out)['tun'] == {'enable': True}
    assert out.index('tun:') > out.index('proxy-groups:')
    print('[OK] assemble_config：新段追加到末尾')


def test_assemble_multi_updates():
    """多字段同时修改互不干扰（订阅槽位为单行段，规则为多行段）。"""
    out = main.assemble_config(_UI_SAMPLE, {
        'sub_url': 'https://example.com/new',
        'sub_url1': 'https://example.com/s1',
        'rules': 'rules:\n  - DOMAIN-SUFFIX,foo.com,G1\n  - MATCH,DIRECT\n'})
    parsed = yaml.safe_load(out)
    assert parsed['sub_url'] == 'https://example.com/new'
    assert parsed['sub_url1'] == 'https://example.com/s1'
    assert parsed['rules'] == ['DOMAIN-SUFFIX,foo.com,G1', 'MATCH,DIRECT']
    assert parsed['port'] == 7890                    # 未改动的段保持原样
    assert '# 访问密码' in out                        # 未改动行的注释保留
    print('[OK] assemble_config：多字段同时修改')


def test_ui_blocks_payload():
    """界面 payload：订阅槽位逐行取值、代理规则取整段正文。"""
    p = main.ui_blocks_payload(_UI_SAMPLE)
    assert p['sub_url'] == 'https://example.com/sub'   # 注释已剥掉
    assert p['sub_url1'] == ''                         # `""` 归一成空
    # rules 段只回传条目正文（保留 `  - ` 与行尾注释），不含 `rules:` 首行；
    # 前端把它塞进文本区编辑，格式原样回写即可。
    assert p['rules'].splitlines()[0].startswith('  - DOMAIN-SUFFIX,')
    assert 'rules:' not in p['rules']
    print('[OK] ui_blocks_payload：槽位取值、规则取正文')


def test_ui_page_and_save(tmp_path, monkeypatch):
    """GET /ui 返回可编辑页面；POST /ui/save 写盘并热重载。"""
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_UI_SAMPLE, encoding='utf-8')
    client = ui_client(cfg, monkeypatch)

    r = client.get('/ui')
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'MySubConvert 配置' in html
    assert 'sub_url1' in html and 'rules' in html
    assert '__FIELDS__' not in html and '__RAW__' not in html

    r2 = client.post('/ui/save', json={'blocks': {
        'sub_url1': 'https://example.com/s1',
        'rules': 'rules:\n  - DOMAIN-SUFFIX,foo.com,G1\n  - MATCH,DIRECT\n',
    }})
    assert r2.get_json()['ok'] is True
    saved = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    assert saved['sub_url1'] == 'https://example.com/s1'
    assert saved['rules'] == ['DOMAIN-SUFFIX,foo.com,G1', 'MATCH,DIRECT']
    text_now = cfg.read_text(encoding='utf-8')
    assert text_now.startswith('# 文件头注释')            # 文件头注释保留
    assert '# 访问密码' in text_now                        # 行尾注释保留

    # 热重载：无需重启即读到新值
    control, _ = main.load_local_config()
    assert control['sub_url1'] == 'https://example.com/s1'
    print('[OK] /ui + /ui/save：页面可编辑、写盘并热重载')


def test_ui_page_rule_logic_roundtrip(tmp_path, monkeypatch):
    """页面的规则解析/拼装逻辑必须闭合（真跑页面里的 JS）。

    这条测的是踩过的真实 bug：`RAW['rules']` 回传的是 rules 段正文，
    每行带 YAML 列表标记 `- ` 和可能的行尾注释。若 `splitRule` 不剥离它们，
    类型列会显示成 `- DOMAIN-SUFFIX`、注释会混进目标，表格看着「有内容」
    但保存回去就是错的。语法检查抓不到，必须真跑函数。
    """
    import os
    import re
    import subprocess

    from tests.test_e2e import _find_node

    node = _find_node()
    if not node:
        print('[SKIP] 未找到 node，跳过规则逻辑检查')
        return

    html = ui_page_html()
    js = '\n;\n'.join(re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>',
                                 html, re.S))

    def extract_fn(name):
        m = re.search(r'^function\s+%s\s*\([^)]*\)\s*\{' % re.escape(name),
                      js, re.M)
        assert m, '未找到函数 %s' % name
        i, depth = m.end() - 1, 0
        for j in range(i, len(js)):
            if js[j] == '{':
                depth += 1
            elif js[j] == '}':
                depth -= 1
                if depth == 0:
                    return js[m.start():j + 1]
        raise AssertionError('花括号不配对: %s' % name)

    decls = []
    for name in ('NO_VALUE', 'RULE_TYPES'):
        decls += re.findall(r'^(?:const|let|var)\s+%s\s*=.*?;\s*$' % name,
                            js, re.M)

    body = ('  - DOMAIN-SUFFIX,github.com,GITHUB   # 走代理\n'
            '  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve\n'
            '  - MATCH,DIRECT\n')
    harness = '\n'.join(decls) + '\n\n' + '\n\n'.join(
        extract_fn(n) for n in ('splitRule', 'joinRule', 'parseRules')) + '''

const parsed = parseRules(%r);
const bad = [];
if (parsed.length !== 3) bad.push('len=' + parsed.length);
if (parsed[0].type !== 'DOMAIN-SUFFIX') bad.push('type0=' + parsed[0].type);
if (parsed[0].value !== 'github.com') bad.push('val0=' + parsed[0].value);
if (parsed[0].target !== 'GITHUB') bad.push('tgt0=' + parsed[0].target);
if (parsed[1].target !== 'DIRECT,no-resolve') bad.push('tgt1=' + parsed[1].target);
if (parsed[2].type !== 'MATCH' || parsed[2].target !== 'DIRECT') {
  bad.push('match=' + JSON.stringify(parsed[2]));
}
if (joinRule(parsed[0]) !== 'DOMAIN-SUFFIX,github.com,GITHUB') {
  bad.push('join0=' + joinRule(parsed[0]));
}
if (joinRule(parsed[2]) !== 'MATCH,DIRECT') bad.push('join2=' + joinRule(parsed[2]));
console.log(bad.length ? 'BAD ' + bad.join(' | ') : 'OK');
''' % body

    p = tmp_path / 'rule_logic.js'
    p.write_text(harness, encoding='utf-8')
    r = subprocess.run([node, str(p)], capture_output=True, text=True,
                       timeout=60)
    out = (r.stdout or '').strip()
    assert r.returncode == 0 and out == 'OK', \
        '规则解析逻辑不闭合: rc=%s out=%s err=%s' % (
            r.returncode, out, (r.stderr or '')[:400])
    print('[OK] /ui：规则解析与拼装逻辑闭合（剥列表标记与注释）')


def test_ui_page_writeback_is_valid_yaml(tmp_path, monkeypatch):
    """页面写回的 rules 段必须能被 YAML 解析成字符串列表。

    真实 bug：`collect()` 曾用 `'  ' + r` 拼行，漏了列表标记 `- `，
    写回 config.yaml 就成了：
        rules:
          DOMAIN-SUFFIX,github.com,GITHUB
    裸缩进的 `a,b,c` 会被 YAML 当成 dict 的键（或直接报错），
    客户端拿到的是坏配置。表格里不显示 `- `（`parseRules` 会剥掉），
    所以只有写回这一刻才补——必须真跑 `collect()` 才测得出。
    """
    import re
    import subprocess

    from tests.test_e2e import _find_node

    node = _find_node()
    if not node:
        print('[SKIP] 未找到 node，跳过写回检查')
        return

    html = ui_page_html()
    js = '\n;\n'.join(re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>',
                                 html, re.S))
    m = re.search(r'^function\s+collect\s*\([^)]*\)\s*\{', js, re.M)
    assert m, '未找到 collect 函数'
    i, depth = m.end() - 1, 0
    for j in range(i, len(js)):
        if js[j] == '{':
            depth += 1
        elif js[j] == '}':
            depth -= 1
            if depth == 0:
                collect_src = js[m.start():j + 1]
                break
    else:
        raise AssertionError('collect 花括号不配对')

    decls = []
    for name in ('NO_VALUE', 'RULE_TYPES', 'BUILTIN_TARGETS_JS',
                 'YAML_QUOTE_RE', 'YAML_AMBIG_RE'):
        decls += re.findall(r'^(?:const|let|var)\s+%s\s*=.*?;\s*$' % name,
                            js, re.M)
    decls.append('let rules = [];')
    decls.append('let groups = [];')
    decls.append('let proxies = [];')
    decls.append('let knownTargets = [];')
    decls.append('const RAW = {};')
    decls.append('const SLOTS = [];')
    decls.append('let curSlot = "";')
    decls.append('let exKey = null;')
    decls.append('const F = { extra_keys: [] };')
    decls.append('const slotUrl = { value: "" };')
    decls.append("function syncRawFromRules() {}")
    decls.append("function markDirty() {}")
    decls.append("function $(s) { return { value: '' }; }")

    body = ('  - DOMAIN-SUFFIX,github.com,GITHUB   # 走代理\n'
            '  - MATCH,DIRECT\n')
    harness = '\n'.join(decls) + '\n\n' + '\n\n'.join(
        re.search(r'^function\s+%s\s*\([^)]*\)\s*\{[\s\S]*?^\}' % n,
                  js, re.M).group(0)
        for n in ('splitRule', 'joinRule', 'parseRules', 'stripComment',
                  'commentOf', 'unquote', 'yamlScalar',
                  'parseListSection', 'renderListSection')) + '\n\n' + \
        collect_src + '''

rules = parseRules(%r);
const blocks = collect();
console.log(JSON.stringify(blocks.rules));
''' % body

    p = tmp_path / 'collect.js'
    p.write_text(harness, encoding='utf-8')
    r = subprocess.run([node, str(p)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, 'harness 跑不起来: %s' % (r.stderr or '')[:400]
    written = (r.stdout or '').splitlines()[-1]
    import json as _json
    text = _json.loads(written)

    parsed = yaml.safe_load(text)
    rules = main._normalize_rules(parsed.get('rules'))
    assert rules == ['DOMAIN-SUFFIX,github.com,GITHUB', 'MATCH,DIRECT'], \
        '写回的 rules 段不是合法 YAML 字符串列表：\n%s\n解析得 %r' % (text, rules)
    print('[OK] /ui：写回的 rules 段是合法 YAML（带 `- ` 列表标记）')


_JS_HELPERS = ('splitRule', 'joinRule', 'parseRules', 'stripComment',
               'commentOf', 'unquote', 'yamlScalar', 'quoteScalar',
               'parseListSection', 'renderListSection',
               'collect', 'applyPxRaw', 'applyPgRaw')


def _load_page_js(extra_decls=()):
    """从页面里抽出内联 JS，并提取公共函数，拼成一个可直接跑的小模块。

    前端这些解析/渲染函数与后端 list_section_items / render_list_section
    是**两份实现**，最容易出现「后端对、前端错」的静默不一致（比如前端
    renderListSection 把不认识的字段丢了）。所以必须真跑一遍前端代码。
    """
    import re

    html = ui_page_html()
    js = '\n;\n'.join(re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>',
                                 html, re.S))
    decls = []
    for name in ('NO_VALUE', 'RULE_TYPES', 'BUILTIN_TARGETS_JS',
                 'YAML_QUOTE_RE', 'YAML_AMBIG_RE'):
        decls += re.findall(r'^(?:const|let|var)\s+%s\s*=.*?;\s*$' % name,
                            js, re.M)
    decls += list(extra_decls)
    fns = [re.search(r'^function\s+%s\s*\([^)]*\)\s*\{[\s\S]*?^\}' % n,
                     js, re.M).group(0) for n in _JS_HELPERS]
    return '\n'.join(decls) + '\n\n' + '\n\n'.join(fns)


def _run_js(tmp_path, name, source):
    """跑一段 JS，返回 stdout 最后一行。"""
    import subprocess

    from tests.test_e2e import _find_node

    node = _find_node()
    if not node:
        return None
    p = tmp_path / name
    p.write_text(source, encoding='utf-8')
    r = subprocess.run([node, str(p)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, 'JS 执行失败: %s' % (r.stderr or '')[:500]
    return (r.stdout or '').strip().splitlines()[-1]


def test_ui_page_list_section_preserves_scalar_types(tmp_path):
    """前端重渲染时，原文裸写的字段必须保持 YAML 原生类型。

    真实 bug：后端 `_yaml_scalar(v, force_str)` 有"原文没加引号就裸写"的机制，
    但**前端 `renderListSection` 漏传了 `_quoted`** → 界面上没动过的字段被当字符串
    重新加引号，`udp: true` 保存后变成 `udp: "true"`（布尔 → 字符串），
    `port: 8388` 也可能被引成 `"8388"`。**配置语义被静默改掉**，页面完全看不出来。

    这是「同一逻辑两份实现」的典型翻车点：只修了后端、忘了同步前端。
    判据必须是**解析后的类型**，不是字符串比对——`'udp: true' in text` 在
    `udp: "true"` 上是 False，但如果你只断言"渲染成功"就测不出来。
    """
    decls = [
        'let rules = [];', 'let groups = [];', 'let proxies = [];',
        'let knownTargets = [];', 'const RAW = {};',
    ]
    src = _load_page_js(decls) + '''

const NODES = [
  'proxies:',
  '  - name: Home',
  '    type: ss',
  '    server: 11.11.11.11',
  '    port: 11',
  '    cipher: aes-256-gcm',
  '    password: 1234',
  '    udp: true',
].join('\\n');

const bad = [];
const items = parseListSection(NODES, ['name', 'type', 'server', 'port']);
const it = items[0];
if (it.udp !== 'true') bad.push('udp parsed as ' + JSON.stringify(it.udp));
if (it._quoted.udp !== false) bad.push('_quoted.udp = ' + JSON.stringify(it._quoted.udp));
if (it._quoted.name !== false) bad.push('_quoted.name wrong');

const out = renderListSection('proxies', ['name', 'type', 'server', 'port'], items);
// 裸写：不能出现引号包裹的 true / 数字
if (out.indexOf('udp: true') < 0) bad.push('udp 未裸写: ' + out);
if (out.indexOf('udp: "true"') >= 0) bad.push('udp 被加引号');
if (out.indexOf('port: "11"') >= 0) bad.push('port 被加引号');
// 原文明确加了引号的字符串字段，仍应保留引号语义（这里 password 未加引号，故裸写）
if (out.indexOf('password: 1234') < 0) bad.push('password 不该被引: ' + out);

console.log(bad.length ? 'BAD ' + bad.join(' | ') : 'OK');
'''
    got = _run_js(tmp_path, 'scalar_types.js', src)
    if got is None:
        print('[SKIP] 未找到 node，跳过前端类型保持检查')
        return
    assert got == 'OK', got

    # 再把渲染结果真正喂给 YAML，确认类型正确（这是要守的最终语义）
    src2 = _load_page_js(decls) + '''

const NODES = [
  'proxies:',
  '  - name: Home',
  '    type: ss',
  '    server: 11.11.11.11',
  '    port: 11',
  '    udp: true',
  '    tls: false',
].join('\\n');
const items = parseListSection(NODES, ['name', 'type', 'server', 'port']);
process.stdout.write(renderListSection('proxies', ['name', 'type', 'server', 'port'], items));
'''
    import pathlib
    import subprocess

    from tests.test_e2e import _find_node

    node = _find_node()
    if not node:
        print('[SKIP] 未找到 node，跳过 YAML 类型校验')
        return
    mod = pathlib.Path(tmp_path) / 'scalar_yaml.js'
    mod.write_text(src2, encoding='utf-8')
    r = subprocess.run([node, str(mod)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'JS 执行失败: %s' % (r.stderr or '')[:400]
    parsed = yaml.safe_load(r.stdout)['proxies'][0]
    assert parsed['udp'] is True, 'udp 变成了 %r（应为布尔 True）' % (parsed['udp'],)
    assert parsed['tls'] is False, 'tls 变成了 %r（应为布尔 False）' % (parsed['tls'],)
    assert parsed['port'] == 11 and isinstance(parsed['port'], int), \
        'port 变成了 %r' % (parsed['port'],)
    print('[OK] 前端列表段重渲染：裸写字段保持 YAML 原生类型（bool/int）')


def test_ui_page_raw_textarea_edit_wins(tmp_path):
    """「高级原文」里改的字段，必须先经过「用文本覆盖表格」才能生效。

    真实 bug：`collect()` 保存 proxy-groups / proxies 段时**只认表格模型**
    （`renderListSection(key, ..., groups/proxies)`），完全不读 `RAW`。
    而这两张卡片原先只有单向同步（表格 → 原文），用户在原文里改
    `udp: false` 后保存，提交上去的还是模型里的 `udp: true` —— 改动被静默
    丢弃，页面还提示「已保存」。用户的反馈就是「改 udp: False / false 都没效果」。

    修法：补齐与规则卡片一致的反向同步（`applyPxRaw` / `applyPgRaw`），
    并把文案里的「保存时原样保留，不会被表格覆盖」改成实话。

    这条测试守的是**端到端契约**：原文改 → apply → collect 出来必须是改后的值，
    并且解析回 YAML 类型正确。只测 apply 函数本身不够，因为丢数据发生在 collect。
    """
    decls = [
        'let rules = [];', 'let groups = [];', 'let proxies = [];',
        'let knownTargets = [];', 'let RAW = {};', 'const SLOTS = [];',
        'let curSlot = "sub_url";', 'const F = {extra_keys: []};',
    ]
    src = _load_page_js(decls) + '''

// DOM 桩：只需要 #px-raw 的 value（applyPxRaw 读它）
const _nodes = {'#px-raw': {value: ''}, '#ex-raw': {value: ''}};
const $ = (sel) => _nodes[sel];
const slotUrl = {value: ''};
function markDirty() {}
function flash() {}
function renderRules() {}
function refreshStat() {}
// 说明：这里不抽 renderProxies/renderGroups 的真实实现——它们依赖整条
// DOM 渲染链（textCell/linesCell/renderListTable...），抽进来只会淹没重点。
// 本测试要守的是「collect 的输入模型」是否更新，所以把重绘打成空壳。
let __redraws = 0;
function renderProxies() { __redraws++; }
function renderGroups() { __redraws++; }

const FIELDS = ['name', 'type', 'server', 'port'];
const NODES = [
  'proxies:',
  '  - name: Home',
  '    type: ss',
  '    server: 10.0.0.1',
  '    port: 8388',
  '    cipher: aes-256-gcm',
  '    password: 1234',
  '    udp: true',
].join('\\n');

// 页面初始状态：表格模型从原文解析，原文回填 RAW
proxies = parseListSection(NODES, FIELDS);
RAW.proxies = renderListSection('proxies', FIELDS, proxies);
SLOTS.length = 0;
curSlot = 'sub_url';

const bad = [];

// 1) 用户只在「高级原文」里把 udp 改成 false，点「用文本覆盖表格」
RAW.proxies = RAW.proxies.replace('udp: true', 'udp: false');
_nodes['#px-raw'].value = RAW.proxies;
applyPxRaw();

const blocks = collect();
if (blocks.proxies.indexOf('udp: false') < 0) {
  bad.push('apply 后仍未写回 udp: false: ' + blocks.proxies);
}
if (blocks.proxies.indexOf('udp: true') >= 0) {
  bad.push('旧值 udp: true 仍在提交内容里（改动被丢弃）');
}

// 2) 未点 apply 时，表格模型的改动仍应生效（反向不能过头）
proxies[0].name = 'Home2';           // 表格里改名（不点 apply）
const blocks2 = collect();
if (blocks2.proxies.indexOf('name: Home2') < 0) {
  bad.push('表格改名未生效: ' + blocks2.proxies);
}

console.log(bad.length ? 'BAD ' + bad.join(' || ') : 'OK');
'''
    got = _run_js(tmp_path, 'raw_apply.js', src)
    if got is None:
        print('[SKIP] 未找到 node，跳过原文覆盖检查')
        return
    assert got == 'OK', got
    print('[OK] /ui：原文编辑经「用文本覆盖表格」后能写回（不再被静默丢弃）')


def test_ui_page_list_section_roundtrip(tmp_path):
    """前端 parseListSection / renderListSection 往返后字段零丢失。

    这条守的是前端独有的风险：表格只编辑 name/type/server/port 与成员的
    proxies，**其余协议字段靠 `_extra` 整行带走**。若前端渲染时丢掉
    `_extra`，用户点一次保存就把 cipher/password/url/interval 全抹了，
    而且页面看着完全正常。
    """
    decls = [
        'let rules = [];', 'let groups = [];', 'let proxies = [];',
        'let knownTargets = [];', 'const RAW = {};',
    ]
    src = _load_page_js(decls) + '''

const NODES = [
  'proxies:',
  '  - name: Home                     # 本地节点',
  '    type: ss',
  '    server: 10.0.0.1',
  '    port: 8388',
  '    cipher: aes-256-gcm',
  '    password: 1234',
  '    udp: true',
].join('\\n');

const GROUPS = [
  'proxy-groups:',
  '  - name: 🚀 节点选择',
  '    type: select',
  '    proxies:',
  '      - ♻️ 自动选择',
  '      - "*"',
  '  - name: ♻️ 自动选择              # 说明注释',
  '    type: url-test',
  '    url: http://www.gstatic.com/generate_204',
  '    interval: 300',
  '    tolerance: 50',
  '    proxies:',
  '      - "*"',
].join('\\n');

const bad = [];
const nodes = parseListSection(NODES, ['name', 'type', 'server', 'port']);
if (nodes.length !== 1) bad.push('nodes len=' + nodes.length);
if (nodes[0].name !== 'Home') bad.push('node name=' + JSON.stringify(nodes[0].name));
if (nodes[0].server !== '10.0.0.1') bad.push('server=' + nodes[0].server);
if (nodes[0].cipher !== 'aes-256-gcm') bad.push('cipher lost');
if (String(nodes[0].password) !== '1234') bad.push('password lost');
if (String(nodes[0].udp) !== 'true') bad.push('udp lost');

const outNodes = renderListSection('proxies', ['name','type','server','port'], nodes);
if (outNodes.indexOf('cipher: aes-256-gcm') < 0) bad.push('render lost cipher');
if (outNodes.indexOf('password: 1234') < 0) bad.push('render lost password');
if (outNodes.indexOf('# 本地节点') < 0) bad.push('render lost comment');

const gs = parseListSection(GROUPS, ['name', 'type', 'proxies']);
if (gs.length !== 2) bad.push('groups len=' + gs.length);
if (gs[0].name !== '🚀 节点选择') bad.push('g0 name=' + gs[0].name);
if (gs[0].proxies.join('|') !== '♻️ 自动选择|*') bad.push('g0 proxies=' + gs[0].proxies);
if (gs[1].name !== '♻️ 自动选择') bad.push('g1 name=' + JSON.stringify(gs[1].name));
if (String(gs[1].interval) !== '300') bad.push('interval lost');
if (String(gs[1].tolerance) !== '50') bad.push('tolerance lost');
if (gs[1].url !== 'http://www.gstatic.com/generate_204') bad.push('url lost');

const outGroups = renderListSection('proxy-groups', ['name','type','proxies'], gs);
if (outGroups.indexOf('interval: 300') < 0) bad.push('render lost interval');
// url 含冒号，yamlScalar 会加双引号（这是对的），所以只查键存在
if (outGroups.indexOf('url:') < 0) bad.push('render lost url');
if (outGroups.indexOf('tolerance: 50') < 0) bad.push('render lost tolerance');
if (outGroups.indexOf('# 说明注释') < 0) bad.push('render lost g1 comment');
if (gs[1].url !== 'http://www.gstatic.com/generate_204') bad.push('url mangled');

// 空列表要写成 `key: []`
if (renderListSection('proxies', ['name'], []).indexOf('proxies: []') !== 0) {
  bad.push('empty not rendered as []');
}
console.log(bad.length ? 'BAD ' + bad.join(' | ') : 'OK ' + JSON.stringify([outNodes, outGroups]));
'''
    out = _run_js(tmp_path, 'list_roundtrip.js', src)
    if out is None:
        print('[SKIP] 未找到 node，跳过前端列表段检查')
        return
    assert out.startswith('OK'), '前端列表段往返有问题: %s' % out[:600]
    # 用 Python 侧解析前端产物，确认是合法 YAML 且字段完整
    import json as _json
    text = _json.loads(out[3:])
    parsed = yaml.safe_load(text[0])
    assert parsed['proxies'][0]['cipher'] == 'aes-256-gcm'
    assert str(parsed['proxies'][0]['password']) == '1234'
    parsed_g = yaml.safe_load(text[1])
    assert parsed_g['proxy-groups'][1]['interval'] == 300
    assert parsed_g['proxy-groups'][1]['url'] == 'http://www.gstatic.com/generate_204'
    assert parsed_g['proxy-groups'][0]['proxies'] == ['♻️ 自动选择', '*']
    print('[OK] /ui：前端列表段往返字段零丢失（含 _extra 协议字段与注释）')


def test_normalize_rules_accepts_all_yaml_shapes():
    """`rules:` 段在 YAML 下会解析成多种类型，界面保存都要能接受。

    曾经踩过：段空着解析成 None 被判「rules 必须是列表」；用户把规则写在
    `rules:` 同一行则解析成 str，同样被拒。两者都是合法写法，必须容错。
    """
    assert main._normalize_rules(None) == []
    assert main._normalize_rules([]) == []
    assert main._normalize_rules(['A,B,C']) == ['A,B,C']
    assert main._normalize_rules('MATCH,DIRECT') == ['MATCH,DIRECT']
    assert main._normalize_rules('') == []
    assert main._normalize_rules('   ') == []
    # 非 str 元素由 ui_save 的元素级校验兜住，_normalize_rules 只管段级类型
    assert main._normalize_rules([1, 2]) == [1, 2]
    for bad in ({'a': 1}, 123):
        try:
            main._normalize_rules(bad)
        except ValueError:
            pass
        else:
            raise AssertionError('非法类型应抛 ValueError: %r' % (bad,))
    print('[OK] _normalize_rules：None / list / str 都能接受，非法类型抛错')


def test_ui_save_accepts_empty_and_inline_rules(tmp_path, monkeypatch):
    """保存空规则、单行内联规则都必须成功（这两种形态曾报「rules 必须是列表」）。"""
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_UI_SAMPLE, encoding='utf-8')
    client = ui_client(cfg, monkeypatch)

    cases = [
        ('空 rules（只 header）', 'rules:\n', None),
        ('单行内联', 'rules: MATCH,DIRECT\n', 'MATCH,DIRECT'),
        ('正常两条', 'rules:\n  - DOMAIN-SUFFIX,a.com,G1\n  - MATCH,DIRECT\n',
         ['DOMAIN-SUFFIX,a.com,G1', 'MATCH,DIRECT']),
    ]
    for name, payload, want in cases:
        r = client.post('/ui/save', json={'blocks': {'rules': payload}})
        body = r.get_json()
        assert body.get('ok') is True, '%s 被拒: %s' % (name, body.get('error'))
        saved = yaml.safe_load(cfg.read_text(encoding='utf-8'))
        assert saved.get('rules') == want, '%s: %r' % (name, saved.get('rules'))
    print('[OK] /ui/save：空规则与单行内联规则均可保存')


def test_assemble_no_double_header(tmp_path):
    """替换体自带 `key:` 首行时不得再套一次，否则写出 `rules: rules:`。

    同时确认单行值里的 `https://...` 不会被误判成 `key:` 头（会被误当整段替换）。
    """
    sample = "password: pw\nport: 7890\nrules:\n  - A,B,C\n"
    # 替换体自带 header → 整段替换，不重复前缀
    out = main.assemble_config(sample, {'rules': 'rules:\n  - D,E,F\n'})
    assert 'rules: rules:' not in out, out
    assert yaml.safe_load(out)['rules'] == ['D,E,F']

    # 段在原文里已是单行形态（rules: X）时，再送自带 header 的正文也不能翻倍
    inline = "password: pw\nrules: MATCH,DIRECT\n"
    out2 = main.assemble_config(inline, {'rules': 'rules:\n  - A,B,C\n'})
    assert 'rules: rules:' not in out2, out2
    assert yaml.safe_load(out2)['rules'] == ['A,B,C']

    # URL 值（含冒号）不被误判成 header：应写成单行 `sub_url: <url>`，
    # 而不是把 URL 当成整段内容原样铺开（那会得到无 key 的裸行）
    out3 = main.assemble_config(sample, {'sub_url': 'https://h/sub?a=1'})
    assert yaml.safe_load(out3)['sub_url'] == 'https://h/sub?a=1'
    assert 'sub_url: https://h/sub?a=1' in out3
    assert 'https: //h' not in out3        # 未被冒号拆成 key
    print('[OK] assemble_config：不产生双重 header，URL 值不被误判')


# ---------- 列表段（proxies / proxy-groups）：结构化编辑 ----------
_LIST_SAMPLE_PW = 'pw'
_LIST_SAMPLE = (
    'password: pw\n'
    'proxies:\n'
    '  - name: Home                     # 本地节点\n'
    '    type: ss\n'
    '    server: 10.0.0.1\n'
    '    port: 8388\n'
    '    cipher: aes-256-gcm\n'
    '    password: 1234\n'
    '    udp: true\n'
    'proxy-groups:\n'
    '  - name: 🚀 节点选择\n'
    '    type: select\n'
    '    proxies:\n'
    '      - ♻️ 自动选择\n'
    '      - "*"\n'
    '  - name: ♻️ 自动选择              # 自动测速\n'
    '    type: url-test\n'
    '    url: http://www.gstatic.com/generate_204\n'
    '    interval: 300\n'
    '    tolerance: 50\n'
    '    proxies:\n'
    '      - "*"\n'
    'rules:\n'
    '  - MATCH,DIRECT\n'
)


def test_list_section_roundtrip_keeps_every_field():
    """proxies / proxy-groups 解析后重渲染，**所有字段一个不少**。

    这条守的是最危险的一类 bug：界面只暴露 name/type/server/port 几个字段，
    其余（cipher/password/udp 以及 url-test 的 url/interval/tolerance）若在
    重渲染时被丢掉，用户点一次保存就会被静默改坏配置——页面上完全看不出来。
    必须做「解析 → 渲染 → YAML 解析」的语义比对，只断言子串抓不到丢字段。
    """
    blocks = main.split_config_blocks(_LIST_SAMPLE)
    for key in ('proxies', 'proxy-groups'):
        before = yaml.safe_load(blocks[key])[key]
        items = main.list_section_items(blocks[key])
        assert len(items) == len(before), '%s 条目数不对: %d' % (key, len(items))
        text = main.render_list_section(key, items)
        after = yaml.safe_load(text)[key]
        assert before == after, \
            '%s 往返后字段有变化:\n  原始 %r\n  重渲染 %r\n\n%s' % (
                key, before, after, text)
    print('[OK] 列表段往返：proxies / proxy-groups 字段零丢失')


def test_list_section_parses_names_and_comments():
    """名称要去掉行尾注释与引号；嵌套 proxies 列表要提成字符串列表。"""
    blocks = main.split_config_blocks(_LIST_SAMPLE)
    nodes = main.list_section_items(blocks['proxies'])
    assert nodes[0]['name'] == 'Home', nodes[0]['name']
    assert nodes[0]['type'] == 'ss'
    assert nodes[0]['server'] == '10.0.0.1'
    assert str(nodes[0]['port']) == '8388'
    # 界面不暴露的字段也必须解析出来（才能原样写回）
    assert nodes[0]['cipher'] == 'aes-256-gcm'
    assert str(nodes[0]['password']) == '1234'

    groups = main.list_section_items(blocks['proxy-groups'])
    assert groups[0]['name'] == '🚀 节点选择'
    assert groups[0]['proxies'] == ['♻️ 自动选择', '*'], groups[0]['proxies']
    # 行尾注释不该混进 name
    assert groups[1]['name'] == '♻️ 自动选择', repr(groups[1]['name'])
    assert groups[1]['type'] == 'url-test'
    print('[OK] 列表段解析：名称去注释与引号，嵌套列表提成字符串列表')


def test_list_section_edit_and_add_remove():
    """改字段、增条目、删条目后写回 YAML 语义正确。"""
    blocks = main.split_config_blocks(_LIST_SAMPLE)
    items = main.list_section_items(blocks['proxies'])
    # 改
    items[0]['server'] = '192.168.1.9'
    items[0]['port'] = '1080'
    # 增（新节点只给关键字段，不该影响已有条目）
    items.append({'name': 'B', 'type': 'trojan', 'server': 'b.example.com',
                  'port': '443', '_order': ['name', 'type', 'server', 'port'],
                  '_nested': [], '_extra': []})
    text = main.render_list_section('proxies', items)
    parsed = yaml.safe_load(text)['proxies']
    assert len(parsed) == 2
    assert parsed[0]['server'] == '192.168.1.9'
    assert str(parsed[0]['port']) == '1080'
    assert parsed[0]['cipher'] == 'aes-256-gcm'      # 未编辑字段仍保留
    assert parsed[1]['name'] == 'B'
    assert parsed[1]['port'] == 443                  # YAML 里 port 应是数字

    # 删到空：应输出 `proxies: []` 而不是留个空段
    empty = main.render_list_section('proxies', [])
    assert yaml.safe_load(empty)['proxies'] == []
    print('[OK] 列表段编辑：改/增/删后写回语义正确，空列表写 []')


def test_list_section_edit_preserves_comments():
    """只改字段值时，同条目上的注释不应被抹掉。"""
    blocks = main.split_config_blocks(_LIST_SAMPLE)
    items = main.list_section_items(blocks['proxies'])
    items[0]['server'] = '10.9.9.9'
    text = main.render_list_section('proxies', items)
    assert '# 本地节点' in text, text
    assert yaml.safe_load(text)['proxies'][0]['server'] == '10.9.9.9'
    print('[OK] 列表段编辑：改字段保留同条目注释')


def test_ui_save_accepts_list_sections(tmp_path, monkeypatch):
    """界面保存 proxies / proxy-groups 时，服务端校验与写回都要通过。"""
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_LIST_SAMPLE, encoding='utf-8')
    client = ui_client(cfg, monkeypatch, _LIST_SAMPLE_PW)

    blocks = main.split_config_blocks(_LIST_SAMPLE)
    groups = main.list_section_items(blocks['proxy-groups'])
    groups[0]['name'] = '🚀 改名组'
    payload = main.render_list_section('proxy-groups', groups)

    r = client.post('/ui/save', json={'blocks': {'proxy-groups': payload}})
    body = r.get_json()
    assert body.get('ok') is True, body.get('error')

    saved = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    names = [g['name'] for g in saved['proxy-groups']]
    assert '🚀 改名组' in names, names
    # 未动过的 proxies 段必须原样
    assert saved['proxies'][0]['cipher'] == 'aes-256-gcm'
    assert saved['rules'] == ['MATCH,DIRECT']
    print('[OK] /ui/save：列表段可保存，未改动的段不受影响')


def test_ui_save_rejects_broken_list_sections(tmp_path, monkeypatch):
    """写坏的列表段必须被拒（缺 name 的代理组、非法结构）。"""
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_LIST_SAMPLE, encoding='utf-8')
    client = ui_client(cfg, monkeypatch, _LIST_SAMPLE_PW)

    bad_cases = [
        ('代理组缺 name', 'proxy-groups:\n  - type: select\n'),
        ('段类型不对', 'proxy-groups: not-a-list\n'),
    ]
    for name, payload in bad_cases:
        r = client.post('/ui/save', json={'blocks': {'proxy-groups': payload}})
        assert r.get_json().get('ok') is False, '%s 竟然被接受' % name
    # 被拒之后原文件必须没被改动
    assert yaml.safe_load(cfg.read_text(encoding='utf-8'))['proxy-groups']
    print('[OK] /ui/save：坏列表段被拒且不落盘')

def test_ui_page_inline_js_is_valid(tmp_path, monkeypatch):
    """页面的内联 JS 必须是可执行语法。

    这条是必须的：占位符替换后若把 JSON 塞进 `window.__X__ = ...` 的**左侧**，
    页面结构看着正常、文本断言也都能过，但脚本一执行就报语法错，整个界面白屏。
    只断言「HTML 里有某个字符串」抓不到这种问题，必须真的做语法检查。
    """
    import json
    import re
    import subprocess

    from tests.test_e2e import _find_node      # 复用 node 定位
 
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_UI_SAMPLE, encoding='utf-8')
    monkeypatch.setattr(main, 'CONFIG_FILE', str(cfg))
    monkeypatch.setattr(main.load_local_config, '__defaults__', (str(cfg),))
    main.invalidate_config()

    html = ui_page_html()
    assert '__FIELDS__' not in html and '__RAW__' not in html

    blocks = re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>', html, re.S)
    assert blocks, '页面应包含内联 script'
    js = tmp_path / 'inline.js'
    js.write_text('\n;\n'.join(blocks), encoding='utf-8')

    node = _find_node()
    if not node:
        print('[SKIP] 未找到 node，跳过内联 JS 语法检查')
        return
    r = subprocess.run([node, '--check', str(js)], capture_output=True, timeout=60)
    assert r.returncode == 0, '内联 JS 语法错误:\n%s' % r.stderr.decode('utf-8', 'replace')
    print('[OK] /ui 内联 JS 语法正确')


def test_ui_page_xss_escaped(tmp_path, monkeypatch):
    """配置内容里出现 `</script>` 时不能逃出内联 <script>（存储型 XSS）。

    真实漏洞：/ui 把 config.yaml 的内容 `json.dumps` 后直接 replace 进
    `<script>`，**`json.dumps` 只保证 JSON 合法、不保证 HTML 安全**——它不转义 `<`。
    于是只要配置里出现 `</script><script>...</script>`，浏览器就会提前闭合
    当前 script 标签，注入的脚本**真的会执行**。
    （实测：headless Edge 打开 /ui，`window.__PWNED` 变成 1。）

    修法见 `_json_for_script`：把 `<` `>` `&` 与 U+2028/U+2029 转成 `\\uXXXX`。
    这些是合法的 JSON 转义，`JSON.parse` 后与原文完全一致，前端零改动。

    这条测试守两点，缺一不可：
      1. **安全**：页面里不应出现配置带来的裸 `</script>`；
      2. **不破坏功能**：转义后 `JSON.parse` 出来的内容必须与原文逐字相同
         （尤其换行 —— 若误把反斜杠也转义一遍，`\\n` 会变成字面量「反斜杠+n」）。
    """
    import json
    import re

    # 规则里塞 </script> 与 <img onerror>，节点名里塞 img 注入
    evil_rule = 'DOMAIN-SUFFIX,evil.com,PROXY # </script><script>window.__PWNED=1</script>'
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(
        'password: old-pw\n'
        'sub_url: https://example.com/sub\n'
        'rules:\n'
        '  - ' + evil_rule + '\n'
        '  - MATCH,DIRECT\n'
        'proxy-groups:\n'
        '  - name: PROXY\n'
        '    type: select\n'
        '    proxies:\n'
        '      - DIRECT\n',
        encoding='utf-8')
    client = ui_client(cfg, monkeypatch)
    html = ui_page_html()

    # ---- 1. 安全：只应有页面自己的 script 闭合标签 ----
    n_close = html.count('</script>')
    assert n_close == 2, \
        '页面出现 %d 个 </script>（正常 2：登录无关，页面自身两个 script 块）—— ' \
        '配置内容逃出了 <script>，构成存储型 XSS' % n_close
    assert '</script><script>' not in html, '注入序列原样出现，可被浏览器执行'
    assert '\\u003c' in html, '未做 < 转义，_json_for_script 可能没接上'

    # ---- 2. 不破坏功能：注入的 JSON 解析后必须与配置原文一致 ----
    m = re.search(r'const F = (.*?), RAW = (.*?);', html, re.S)
    assert m, '未找到字段注入语句'
    raw = json.loads(m.group(2))
    assert evil_rule in raw['rules'], \
        '转义破坏了内容：rules 里读不回原始规则\n%r' % raw['rules'][:200]
    # 换行必须是真实换行（防止 \\n 被二次转义成字面量）
    assert '\n' in raw['rules'] and '\\n' not in raw['rules'].replace('\n', ''), \
        'rules 里的换行被改坏了（疑似把反斜杠也转义了一遍）'
    print('[OK] /ui：配置内容里的 </script> 已被转义，且内容往返无损')


def test_ui_page_renders_fields(tmp_path, monkeypatch):
    """页面必须把槽位表、目标候选与原始内容注入成可用的 JS 字面量。"""
    import json
    import re

    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_UI_SAMPLE, encoding='utf-8')
    monkeypatch.setattr(main, 'CONFIG_FILE', str(cfg))
    monkeypatch.setattr(main.load_local_config, '__defaults__', (str(cfg),))
    main.invalidate_config()

    html = ui_page_html()
    m = re.search(r'const F = (.*?), RAW = (.*?);', html, re.S)
    assert m, '未找到字段注入语句'
    meta = json.loads(m.group(1))
    raw = json.loads(m.group(2))

    # 元信息：槽位表 + 目标候选（本地代理组名必须出现，供目标输入框自动补全）
    assert [s['key'] for s in meta['slots']] == [s['key'] for s in main.UI_SLOTS]
    assert 'G1' in meta['targets'] and 'DIRECT' in meta['targets']
    assert meta['api_path']

    # 原始内容：订阅槽位逐行值 + 规则正文（不含 `rules:` 首行）
    assert raw['sub_url'] == 'https://example.com/sub'
    assert raw['sub_url1'] == ''
    assert raw['rules'].splitlines()[0].startswith('  - DOMAIN-SUFFIX,')
    # 代理组 / 本地节点的整段原文也要注入（前端用 parseListSection 拆）
    assert raw['proxy-groups'].startswith('proxy-groups:')
    assert raw['proxies'].startswith('proxies:')
    assert [k['key'] for k in meta['extra_keys']], '其他配置下拉不该为空'
    print('[OK] /ui：槽位表、目标候选与原始内容注入为合法 JS 字面量')


def _edge_path():
    """定位系统 Edge（用于把页面真渲染一遍）。"""
    import os as _os
    for c in (r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe',
              r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe',
              r'%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe'):
        p = _os.path.expandvars(c)
        if _os.path.exists(p):
            return p
    return None


def test_ui_target_is_real_dropdown(tmp_path, monkeypatch):
    """规则的目标列必须是真正的 <select>，且选项里含全部代理组。

    这条对应一个真实反馈：「目标代理组没法切换，下拉框没有其他代理组可选」。
    根因是目标列用的是 `<input list=...>` + 只在初始化时建一次的 datalist——
    点进输入框什么都不显示，必须先打字才出候选，且代理组增删后不刷新。
    光看 HTML 里「有 targets 数据」抓不到，必须把页面真跑一遍看 DOM。
    """
    import json as _json
    import os as _os
    import subprocess

    edge = _edge_path()
    if not edge:
        print('[SKIP] 未找到 Edge，跳过下拉框检查')
        return

    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_UI_SAMPLE, encoding='utf-8')
    monkeypatch.setattr(main, 'CONFIG_FILE', str(cfg))
    monkeypatch.setattr(main.load_local_config, '__defaults__', (str(cfg),))
    main.invalidate_config()

    html = ui_page_html()
    probe = '''
const rows = document.querySelectorAll('#rules-body tr');
const sel = rows[0] ? rows[0].querySelectorAll('td')[4].querySelector('select') : null;
const out = {
  isSelect: !!sel,
  hasDatalist: !!document.getElementById('target-list'),
  options: sel ? Array.from(sel.options).map(o => o.value) : [],
  groupRowInputs: Array.from(document.querySelectorAll('#pg-body tr')).map(
    tr => tr.querySelectorAll('td')[2].querySelector('input').value),
  nodeRowInputs: Array.from(document.querySelectorAll('#px-body tr')).map(
    tr => tr.querySelectorAll('td')[2].querySelector('input').value),
  extraKeys: Array.from(document.querySelectorAll('#ex-key option')).map(o => o.value)
};
const d = document.createElement('div');
d.id = 'PROBE_OUT';
d.textContent = JSON.stringify(out);
document.body.appendChild(d);
'''
    page = tmp_path / 'page.html'
    page.write_text(html.replace('</body>',
                                 '<script>%s</script></body>' % probe),
                    encoding='utf-8')
    r = subprocess.run([edge, '--headless=new', '--disable-gpu', '--no-sandbox',
                        '--dump-dom', 'file:///' + str(page).replace(_os.sep, '/')],
                       capture_output=True, text=True, timeout=180)
    dom = r.stdout or ''
    i = dom.find('id="PROBE_OUT"')
    assert i > 0, '页面探针没跑起来'
    raw = dom[i + len('id="PROBE_OUT">'):]
    got = _json.loads(raw[:raw.find('</div>')])

    assert got['isSelect'], '目标列不是 <select>，用户点开看不到候选'
    assert not got['hasDatalist'], '旧的 datalist 应已移除'
    for name in ('G1', 'DIRECT', 'REJECT'):
        assert name in got['options'], '目标下拉缺少 %s：%r' % (name, got['options'])
    # 代理组 / 本地节点表格真的渲染出了数据
    assert got['groupRowInputs'] == ['G1'], got['groupRowInputs']
    assert got['nodeRowInputs'] == ['A'], got['nodeRowInputs']
    assert got['extraKeys'], '其他配置下拉为空'
    print('[OK] /ui：目标列是真下拉框，代理组/节点/其他配置表格均已渲染')


def test_ui_table_rows_are_not_draggable(tmp_path, monkeypatch):
    """表格行本身不能可拖；只有手柄（⠿）可拖。

    真实反馈：「代理规则、本地节点、代理组 输入框选择文字：按住鼠标滑动会把整排
    条目拖走」。根因是 `tr.draggable = true` —— 浏览器把「在行内输入框按住鼠标
    横向滑动选字」判定成拖拽整行：文字选不中，条目还会被拖走。

    修法是把 `draggable` 从行挪到拖拽手柄上（`.handle` 那个 `⠿`），行只当放置目标。

    注意**不要**再给行内的 input/textarea/select 加 `draggable = false`：
    实测（Edge）这些元素的 `draggable` 默认为 `false`，显式设置是冗余代码 ——
    变异测试删掉它不会有任何变化，等于白加。

    这条只能用真实 DOM 断言：`draggable` 是运行时由 JS 设的元素属性，
    静态读 HTML 源码看不出「谁可拖」。
    """
    import json as _json
    import os as _os
    import subprocess

    edge = _edge_path()
    if not edge:
        print('[SKIP] 未找到 Edge，跳过拖拽属性检查')
        return

    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_UI_SAMPLE, encoding='utf-8')
    ui_client(cfg, monkeypatch)          # 走统一的登录/配置切换路径
    html = ui_page_html()
    probe = '''
const TABLES = ['#rules-body', '#pg-body', '#px-body'];
const out = {rows: 0, badRows: 0, handles: 0, badHandles: 0,
             controls: 0, handleText: null};
TABLES.forEach(sel => {
  document.querySelectorAll(sel + ' tr').forEach(tr => {
    out.rows++;
    if (tr.draggable) out.badRows++;
    const hd = tr.querySelector('td.handle');
    if (hd) { out.handles++; if (!hd.draggable) out.badHandles++; }
    out.controls += tr.querySelectorAll('input, textarea, select').length;
  });
});
const h0 = document.querySelector('#rules-body tr td.handle');
out.handleText = h0 ? h0.textContent : null;
const d = document.createElement('div');
d.id = 'PROBE_OUT';
d.textContent = JSON.stringify(out);
document.body.appendChild(d);
'''
    page = tmp_path / 'page.html'
    page.write_text(html.replace('</body>',
                                 '<script>%s</script></body>' % probe),
                    encoding='utf-8')
    r = subprocess.run([edge, '--headless=new', '--disable-gpu', '--no-sandbox',
                        '--dump-dom', 'file:///' + str(page).replace(_os.sep, '/')],
                       capture_output=True, text=True, timeout=180)
    dom = r.stdout or ''
    i = dom.find('id="PROBE_OUT"')
    assert i > 0, '页面探针没跑起来'
    raw = dom[i + len('id="PROBE_OUT">'):]
    got = _json.loads(raw[:raw.find('</div>')])

    assert got['rows'] > 0, '三张表格一行都没渲染出来，探针选择器可能失效'
    assert got['controls'] > 0, '没找到任何输入控件，探针选择器可能失效'
    assert got['badRows'] == 0, \
        '%d 个 <tr> 仍带 draggable —— 在输入框里选文字会把整行拖走' % got['badRows']
    assert got['handles'] == got['rows'], \
        '有行缺拖拽手柄（%d/%d）' % (got['handles'], got['rows'])
    assert got['badHandles'] == 0, '%d 个手柄不可拖，排序功能会失效' % got['badHandles']
    assert got['handleText'] == '⠿', '手柄内容不是 ⠿：%r' % got['handleText']
    print('[OK] /ui：%d 行均不可拖、手柄可拖（%d 个输入控件不受影响）'
          % (got['rows'], got['controls']))


def test_ui_save_rejects_bad_yaml(tmp_path, monkeypatch):
    """非法 YAML 必须被拒绝，且不破坏原文件。"""
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_UI_SAMPLE, encoding='utf-8')
    client = ui_client(cfg, monkeypatch)
    before = cfg.read_text(encoding='utf-8')
    r = client.post('/ui/save', json={'blocks': {'rules': 'rules:\n  - ::: 坏 ::: ['}})
    body = r.get_json()
    assert body['ok'] is False and '校验失败' in body['error']
    assert cfg.read_text(encoding='utf-8') == before
    print('[OK] /ui/save：非法 YAML 被拒绝且不破坏原文件')


def test_ui_save_ignores_unknown_keys(tmp_path, monkeypatch):
    """界面传来的未知字段一律忽略（防止越权写入任意顶层 key）。"""
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_UI_SAMPLE, encoding='utf-8')
    monkeypatch.setattr(main, 'CONFIG_FILE', str(cfg))
    monkeypatch.setattr(main.load_local_config, '__defaults__', (str(cfg),))

    client = main.app.test_client()
    before = cfg.read_text(encoding='utf-8')
    r = client.post('/ui/save', json={'blocks': {'偷偷加的键': 'x'}})
    assert r.get_json()['ok'] is False
    assert cfg.read_text(encoding='utf-8') == before
    print('[OK] /ui/save：未知字段被忽略')


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
    test_cache_written_is_permanent()
    test_wait_load_returns_true_when_already_finished()
    test_control_int_fallback()
    test_split_config_blocks()
    test_assemble_is_idempotent()
    test_assemble_multiline_block()
    test_assemble_appends_new_block()
    test_ui_blocks_payload()
    # 以下测试依赖 pytest 的 monkeypatch / tmp_path / requests mock，用 pytest 运行：
    # test_full_convert_with_mock / test_fetch_remote_success / test_refresh_* /
    # test_trigger_load_dedup / test_cache_file_roundtrip /
    # test_convert_fetch_fail_* / test_deprecated_control_keys_not_leaked /
    # test_api_selects_subscription_by_index / test_ui_page_and_save /
    # test_ui_save_rejects_bad_yaml / test_ui_save_ignores_unknown_keys
    print('\n全部基础测试通过 ✅')
