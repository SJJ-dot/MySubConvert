"""Clash 配置转换核心（纯函数，不依赖 flask / gevent，便于测试）。

转换原则（本地配置是唯一的配置来源，机场只提供代理节点）：

1. 输出配置的顶层 key **完全取自本地配置**——顺序与内容都以本地 config.yaml 为准，
   机场除 `proxies`（代理节点）以外的内容一律不参与输出。
2. `proxies`：本地节点 + 机场节点，按 `name` 去重，**本地同名优先**。
3. 本地配置中可用表达式 `*` 指定「此处插入机场的全部节点」，可写在代理组的 `proxies`
   列表，也可写在顶层 `proxies` 列表。展开的是**机场带来的节点**——与本地模板同名的
   节点不算（本地定义优先）。表达式未出现时，机场节点统一追加到 `proxies` 末尾
   （保证组内引用的节点有定义）。

   注意：YAML 中该表达式必须写作 `- "*"`。裸写 `- *` 是 YAML 的别名（alias）语法，
   解析直接失败。
"""

from collections import OrderedDict
from copy import deepcopy

import base64
import yaml


# 表达式：在本地配置中代表「机场的全部代理节点」
REMOTE_PROXIES = '*'

# Clash 内置策略：规则的 target 指向它们时不算悬空
BUILTIN_POLICIES = {
    'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'COMPATIBLE', 'GLOBAL',
}


def parse_clash(text):
    """解析机场订阅内容，支持纯 YAML 或 base64(YAML)。

    返回 dict；无法解析时抛 ValueError。
    """
    if not text:
        raise ValueError('订阅内容为空')
    # 先尝试直接按 YAML 解析（绝大多数机场订阅为纯 YAML）
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # base64 兜底（部分订阅为 base64 编码的 YAML）
    try:
        decoded = base64.b64decode(text.strip(), validate=True)
        data = yaml.safe_load(decoded)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    raise ValueError('订阅内容不是合法的 Clash YAML')


def is_placeholder(item):
    """判断列表项是否为「插入机场节点」表达式。"""
    return isinstance(item, str) and item.strip() == REMOTE_PROXIES


def names_of(items):
    """取出列表项的 name 列表（跳过无 name 的畸形项），proxies / proxy-groups 通用。"""
    return [p['name'] for p in items or [] if isinstance(p, dict) and p.get('name')]


def remote_index(remote_proxies):
    """机场节点：name -> 定义（保持订阅中的顺序，同名只保留首个）。"""
    idx = OrderedDict()
    for p in remote_proxies or []:
        if isinstance(p, dict) and p.get('name'):
            idx.setdefault(p['name'], p)
    return idx


def merge_proxies(template_proxies, remote_nodes):
    """合并代理节点：本地优先，机场节点补充；表达式控制机场节点的插入位置。

    表达式未出现在本地 `proxies` 中时，机场节点统一追加到末尾。无论如何，
    机场节点的**定义**都会进入输出，避免代理组引用了不存在的节点。

    :param template_proxies: 本地模板的 proxies 列表
    :param remote_nodes: remote_index() 的返回值（name -> 机场节点定义）
    """
    merged = OrderedDict()
    placed = False
    for item in template_proxies or []:
        if is_placeholder(item):
            placed = True
            for name, node in remote_nodes.items():
                merged.setdefault(name, deepcopy(node))
            continue
        if isinstance(item, dict) and item.get('name'):
            merged.setdefault(item['name'], deepcopy(item))
    if not placed:
        for name, node in remote_nodes.items():
            merged.setdefault(name, deepcopy(node))
    return list(merged.values())


def expand_members(members, remote_names):
    """展开代理组成员里的表达式，并按首次出现位置去重。"""
    out = []
    for m in members or []:
        for name in (remote_names if is_placeholder(m) else [m]):
            if name not in out:
                out.append(name)
    return out


def merge_proxy_groups(template_groups, remote_names):
    """代理组只使用本地配置，并把组内的表达式展开为机场节点名。"""
    groups = []
    for g in template_groups or []:
        if not isinstance(g, dict):
            continue
        g = deepcopy(g)
        members = g.get('proxies')
        if members is not None:
            expanded = expand_members(members, remote_names)
            # 展开后为空（组内原本只有表达式，且没有机场节点）时兜底 DIRECT，
            # 避免输出非法配置：Clash 要求代理组至少有一个成员。
            g['proxies'] = expanded or ['DIRECT']
        groups.append(g)
    return groups


def find_dangling_rules(rules, group_names, node_names):
    """找出目标不存在的规则（如仍指向机场组名）。"""
    known = set(group_names or []) | set(node_names or []) | BUILTIN_POLICIES
    dangling = []
    for rule in rules or []:
        if not isinstance(rule, str) or not rule.strip():
            continue
        parts = [p.strip() for p in rule.split(',')]
        target = parts[2] if len(parts) >= 3 else parts[-1]
        if target not in known:
            dangling.append(rule)
    return dangling


def find_dangling_members(groups, group_names, node_names):
    """找出引用了不存在成员的代理组，返回 [(组名, 成员名)]。

    覆盖两类常见笔误：写错的节点名、以及旧版表达式 `__REMOTE_PROXIES__` 残留
    （现已改为 `*`）。
    """
    known = set(group_names or []) | set(node_names or []) | BUILTIN_POLICIES
    bad = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        for m in g.get('proxies') or []:
            if isinstance(m, str) and m not in known:
                bad.append((g.get('name'), m))
    return bad


def uses_remote_placeholder(template):
    """本地配置是否用到了表达式（顶层 proxies 或任一代理组）。"""
    seqs = [template.get('proxies')]
    for g in template.get('proxy-groups') or []:
        if isinstance(g, dict):
            seqs.append(g.get('proxies'))
    for seq in seqs:
        if isinstance(seq, list) and any(is_placeholder(x) for x in seq):
            return True
    return False


def merge_configs(template, remote):
    """转换订阅：输出顶层 key 完全取自本地配置，机场只贡献代理节点。

    输出 key 的**顺序与内容都以本地模板为准**，机场的顶层 key 一个都不会进入输出。
    本地未定义 `proxies` 而机场有节点时，节点定义追加到末尾（否则代理组引用的
    节点不存在，客户端会拒绝加载）。

    :param template: 本地 Clash 模板（已剔除控制项）
    :param remote: 机场配置 dict，或 None（无订阅时）
    """
    remote = remote or {}

    nodes = remote_index(remote.get('proxies') or [])
    # 表达式展开的是「机场带来的节点」：与本地模板同名的节点不算（本地定义优先，
    # 也避免第 3 步的最小配置（就是本地模板本身）被当成一份订阅重复插入）。
    local_names = set(names_of(template.get('proxies') or []))
    airport_nodes = OrderedDict(
        (n, p) for n, p in nodes.items() if n not in local_names)

    # 用普通 dict 而非 OrderedDict：yaml.dump 会把 OrderedDict 序列化成
    # !!python/object/apply:collections.OrderedDict，客户端无法解析。
    # Python 3.7+ 的 dict 本身保序，足够。
    result = {}
    for k, v in template.items():
        if k == 'proxies':
            result[k] = merge_proxies(v, airport_nodes)
        elif k == 'proxy-groups':
            result[k] = merge_proxy_groups(v, list(airport_nodes))
        else:
            result[k] = deepcopy(v)

    if 'proxies' not in template and airport_nodes:
        result['proxies'] = [deepcopy(p) for p in airport_nodes.values()]
    return result
