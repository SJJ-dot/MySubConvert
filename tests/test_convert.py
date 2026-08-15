"""合并逻辑测试：覆盖本地优先、列表去重、代理组机场优先、rules 顺序、
exclude_groups、remove_keys、空 sub_url 等场景。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import merge
import main


FAKE_REMOTE = {
    'port': 8888,                       # 与本地不同，用于验证「本地优先覆盖」
    'dns': {'enable': True, 'nameserver': ['1.1.1.1']},
    'proxies': [
        {'name': 'Airport-HK', 'type': 'ss', 'server': '1.2.3.4', 'port': 100},
        {'name': 'Airport-JP', 'type': 'ss', 'server': '5.6.7.8', 'port': 200},
    ],
    'proxy-groups': [
        {'name': '🚀 节点选择', 'type': 'select',
         'proxies': ['Airport-HK', 'Airport-JP', 'DIRECT']},
        {'name': '♻️ 自动选择', 'type': 'url-test',
         'proxies': ['Airport-HK', 'Airport-JP'], 'url': 'http://test', 'interval': 300},
        {'name': '🎯 全球直连', 'type': 'select', 'proxies': ['DIRECT']},
        {'name': '🐟 漏网之鱼', 'type': 'select',
         'proxies': ['🚀 节点选择', 'DIRECT']},
        {'name': '🛑 全球拦截', 'type': 'select', 'proxies': ['REJECT', 'DIRECT']},
    ],
    'rules': [
        'DOMAIN-SUFFIX,google.com,🚀 节点选择',
        'MATCH,🐟 漏网之鱼',
    ],
}


def _load_template():
    _, template = main.load_local_config()
    return template


def test_top_level_local_override_and_remote_keep():
    template = _load_template()
    out = merge.merge_configs(template, dict(FAKE_REMOTE))
    # 本地 port 7890 覆盖机场 8888
    assert out['port'] == 7890, out.get('port')
    # 机场独有 key 保留
    assert out['dns'] == {'enable': True, 'nameserver': ['1.1.1.1']}
    # 本地 external-controller 保留
    assert out['external-controller'] == '127.0.0.1:9090'
    print('[OK] 顶层 key：本地已有用本地，本地没有用订阅的')


def test_proxies_merge_subscription_priority():
    template = _load_template()
    out = merge.merge_configs(template, dict(FAKE_REMOTE))
    names = [p['name'] for p in out['proxies']]
    # 订阅节点在前，本地 Home 作为新项补充
    assert names == ['Airport-HK', 'Airport-JP', 'Home'], names
    print('[OK] proxies：订阅优先（机场节点在前），本地新项补充')


def test_proxy_groups_subscription_priority():
    template = _load_template()
    out = merge.merge_configs(template, dict(FAKE_REMOTE))
    groups = {g['name']: g for g in out['proxy-groups']}
    # 机场完整组覆盖本地占位 [DIRECT]
    assert groups['🚀 节点选择']['proxies'] == ['Airport-HK', 'Airport-JP', 'DIRECT'], groups['🚀 节点选择']
    # 本地自定义组被保留
    assert '🏠 回家' in groups and '🤖 Github' in groups
    print('[OK] proxy-groups：订阅优先（占位组被完整组覆盖），自定义组保留')


def test_exclude_groups():
    template = _load_template()
    out = merge.merge_configs(template, dict(FAKE_REMOTE), exclude_groups=['🛑 全球拦截'])
    names = [g['name'] for g in out['proxy-groups']]
    assert '🛑 全球拦截' not in names, names
    # 规则中目标组为 🛑 全球拦截 的也被剔除（本例无，下面验证规则剔除）
    out2 = merge.merge_configs(
        template, dict(FAKE_REMOTE),
        exclude_groups=['🚀 节点选择'])
    assert 'DOMAIN-SUFFIX,google.com,🚀 节点选择' not in out2['rules']
    print('[OK] exclude_groups：代理组与对应规则均被剔除')


def test_remove_keys():
    template = _load_template()
    out = merge.merge_configs(template, dict(FAKE_REMOTE), remove_keys=['dns'])
    assert 'dns' not in out, 'dns 应被剥离'
    # remove_keys 对本地已有的 key 同样生效
    out2 = merge.merge_configs(template, dict(FAKE_REMOTE), remove_keys=['port'])
    assert 'port' not in out2, '本地 port 也应被剥离'
    print('[OK] remove_keys：订阅与本地 key 均可被剥离')


def test_rules_local_first():
    template = _load_template()
    out = merge.merge_configs(template, dict(FAKE_REMOTE))
    # 本地规则应在机场规则之前
    local_first = out['rules'][0]
    assert local_first.startswith('DOMAIN-SUFFIX,githubcopilot.com'), out['rules'][:2]
    # MATCH 规则去重（本地无 MATCH，机场有 MATCH）
    assert out['rules'].count('MATCH,🐟 漏网之鱼') == 1
    print('[OK] rules：本地规则在前，去重生效')


def test_empty_remote_returns_template():
    template = _load_template()
    out = merge.merge_configs(template, None)
    # 空订阅时返回完整本地模板（含占位标准组）
    names = [g['name'] for g in out['proxy-groups']]
    assert '🚀 节点选择' in names and '🏠 回家' in names
    print('[OK] 空订阅：返回完整本地模板')


def test_base64_fallback():
    yaml_text = yaml.safe_dump(FAKE_REMOTE, allow_unicode=True)
    b64 = __import__('base64').b64encode(yaml_text.encode()).decode()
    parsed = merge.parse_clash(b64)
    assert parsed['port'] == 8888
    print('[OK] base64 兜底解析')


def test_full_convert_with_mock(monkeypatch):
    control = {
        'exclude_groups': ['🛑 全球拦截'],
        'remove_keys': [],
        'cache_ttl': 0,
    }
    template = _load_template()
    monkeypatch.setattr(main, 'fetch_remote', lambda sub_url, ttl=0: (dict(FAKE_REMOTE), 'upload=1; download=2'))
    text, userinfo = main.convert('http://fake', control, template)
    out = yaml.safe_load(text)
    assert out['port'] == 7890                       # 本地有 port，用本地
    assert 'dns' in out                               # 本地无 dns，用订阅的
    assert [p['name'] for p in out['proxies']] == ['Airport-HK', 'Airport-JP', 'Home']
    assert '🛑 全球拦截' not in [g['name'] for g in out['proxy-groups']]
    print('[OK] convert 全链路（mock 机场）：合并正确')


if __name__ == '__main__':
    test_top_level_local_override_and_remote_keep()
    test_proxies_merge_subscription_priority()
    test_proxy_groups_subscription_priority()
    test_exclude_groups()
    test_remove_keys()
    test_rules_local_first()
    test_empty_remote_returns_template()
    test_base64_fallback()
    # test_full_convert_with_mock 需要 pytest 的 monkeypatch，单独用 pytest 跑
    print('\n全部基础测试通过 ✅')
