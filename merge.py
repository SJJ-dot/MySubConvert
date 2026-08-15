"""Clash 配置合并核心（纯函数，不依赖 flask / gevent，便于测试）。

合并原则（本地配置作为模板，节点与代理组以订阅优先）：
1. 本地配置先剔除与控制项无关的 key（由调用方处理），以及 remove_keys 要求移除的 key。
2. proxies / proxy-groups：按 name 合并去重，订阅（机场）同名项优先，本地仅补充机场
   没有的新项；代理组额外支持 exclude_groups 剔除。
3. 其他顶层 key：本地已有则用本地；本地没有且不在 remove_keys 中则用订阅的。
4. rules：本地规则置于前面（Clash 自上而下匹配，本地自定义规则优先级更高），
   按字符串去重，并剔除目标组在 exclude 中的规则。
"""

from collections import OrderedDict
from copy import deepcopy

import base64
import yaml


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


def merge_named(local_list, remote_list, local_first, exclude=None):
    """按 name 合并列表（proxies / proxy-groups），去重。

    local_first=True 时本地同名项优先；False 时机场同名项优先。
    exclude 中的 name 会被整体剔除。
    """
    exclude = set(exclude or [])
    merged = OrderedDict()
    order = (local_list, remote_list) if local_first else (remote_list, local_list)
    for lst in order:
        for item in lst:
            name = item.get('name') if isinstance(item, dict) else None
            if not name or name in exclude:
                continue
            if name not in merged:
                merged[name] = deepcopy(item)
    return list(merged.values())


def merge_rules(local_rules, remote_rules, exclude=None):
    """合并规则：本地规则优先（置于前面），按字符串去重。

    剔除目标组（规则最后一个逗号字段，或 >=3 字段时的第 3 字段）在 exclude 中的规则。
    """
    exclude = set(exclude or [])
    seen = set()
    merged = []
    for rule in list(local_rules) + list(remote_rules):
        if not isinstance(rule, str) or not rule.strip():
            continue
        if rule in seen:
            continue
        parts = [p.strip() for p in rule.split(',')]
        target = parts[2] if len(parts) >= 3 else parts[-1]
        if target in exclude:
            continue
        seen.add(rule)
        merged.append(rule)
    return merged


def _rule_target_index(rule):
    """返回规则中目标组字段的下标：>=3 字段时是第 3 字段，否则是最后一个字段。"""
    parts = rule.split(',')
    return 2 if len(parts) >= 3 else len(parts) - 1


def redirect_rule_target(rule, sources, target):
    """将规则中指向 sources 中任一组的字段改写为 target。"""
    parts = rule.split(',')
    idx = _rule_target_index(rule)
    if parts[idx].strip() in sources:
        parts[idx] = target
        return ','.join(parts)
    return rule


def apply_merge_groups(groups, proxies, rules, merge_groups):
    """指定代理组合并：把 sources 组列出的「节点」去重并入 target 组。

    默认行为（可通过配置开关调整）：
    - remove_sources=True：合并后从输出中删除源组；
    - redirect_rules=True：规则中指向源组的目标自动改指 target，避免规则失效。

    :param groups: 合并后的 proxy-groups 列表（就地修改）
    :param proxies: 合并后的 proxies 列表（用于识别哪些名字是节点）
    :param rules: 合并后的 rules 列表（就地修改）
    :param merge_groups: [{'target','sources',...}, ...]
    """
    if not merge_groups:
        return
    proxy_names = {
        p.get('name') for p in proxies
        if isinstance(p, dict) and p.get('name')
    }

    for spec in merge_groups:
        target = spec.get('target')
        sources = set(spec.get('sources') or [])
        if not target or not sources:
            continue
        tg = next((g for g in groups
                   if isinstance(g, dict) and g.get('name') == target), None)
        if tg is None:
            continue  # 目标组不存在（本地与订阅都没有），跳过

        # 收集源组列出的节点（仅并入真正的节点，排除组名/内置策略/目标自身）
        added = []
        for src in sources:
            sg = next((g for g in groups
                       if isinstance(g, dict) and g.get('name') == src), None)
            if sg is None:
                continue
            for member in (sg.get('proxies') or []):
                if member in proxy_names and member != target and member not in added:
                    added.append(member)

        existing = list(tg.get('proxies') or [])
        for m in added:
            if m not in existing:
                existing.append(m)
        tg['proxies'] = existing

        if spec.get('remove_sources', True):
            groups[:] = [
                g for g in groups
                if not (isinstance(g, dict) and g.get('name') in sources)
            ]
            if spec.get('redirect_rules', True):
                for i, rule in enumerate(rules):
                    rules[i] = redirect_rule_target(rule, sources, target)


def merge_configs(template, remote, exclude_groups=None, remove_keys=None,
                  merge_groups=None):
    """合并本地模板与机场配置，返回合并后的 dict。

    :param template: 本地 Clash 模板（已剔除控制项）
    :param remote: 机场配置 dict，或 None（无订阅时）
    :param exclude_groups: 需排除的代理组 / 规则目标组名
    :param remove_keys: 需要从最终配置中移除的顶层 key
    :param merge_groups: 指定代理组合并：sources 组的节点并入 target 组
    """
    if not remote:
        return deepcopy(template)

    remove = set(remove_keys or [])

    # 以本地模板为基础
    result = deepcopy(template)
    # 移除要求剔除的 key
    for k in remove:
        result.pop(k, None)

    # 其他顶层 key：本地已有 -> 用本地；本地没有且未要求移除 -> 用订阅的
    for k, v in remote.items():
        if k in ('proxies', 'proxy-groups', 'rules'):
            continue
        if k in remove:
            continue
        if k not in result:
            result[k] = deepcopy(v)

    # proxies / proxy-groups：订阅优先（机场同名项优先，本地仅补充新项）
    result['proxies'] = merge_named(
        template.get('proxies') or [],
        remote.get('proxies') or [],
        local_first=False,
    )
    result['proxy-groups'] = merge_named(
        template.get('proxy-groups') or [],
        remote.get('proxy-groups') or [],
        local_first=False,
        exclude=exclude_groups,
    )
    # rules：本地在前
    result['rules'] = merge_rules(
        template.get('rules') or [],
        remote.get('rules') or [],
        exclude_groups,
    )
    # 指定代理组合并（节点并入目标组、删除源组、规则改指）
    apply_merge_groups(
        result['proxy-groups'],
        result['proxies'],
        result['rules'],
        merge_groups,
    )
    return result
