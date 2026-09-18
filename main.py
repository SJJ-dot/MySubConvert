"""MySubConvert —— 机场订阅转换服务。

请求处理流程：
  1. 订阅在**后台线程**拉取，拉取成功后写入内存缓存（并落盘到 cache 文件，重启不丢）；
  2. 处理请求时**优先命中缓存**：
     - 有缓存且上次加载距今小于 cache_ttl（默认 60 秒）→ 直接用缓存，不等待；
     - 无缓存、或上次加载已超过 cache_ttl → 触发一次后台加载，并最多等待 3 秒
       （超时不等，有缓存就用缓存，无缓存则退化为本地最小配置）。
  3. 缓存未命中且无订阅地址时，根据本地配置生成最小配置文件（仅本地节点，不含机场内容）；
  4. 根据本地配置转换订阅文件：除代理节点外只使用本地配置，机场只贡献代理节点
     （插入位置由本地配置中的 * 表达式指定）。

缓存策略要点：
  - **同一订阅同一时刻只允许一个加载线程**（加载中再次触发会被忽略）；
  - **缓存一经写入即永久有效**，不因时间推移失效（缓存文件同样不过期）；
  - 加载失败不做任何缓存改动，下次请求超过 cache_ttl 后会自动重试。
"""

from gevent import monkey

monkey.patch_all()

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from copy import deepcopy

import requests
import yaml
from flask import (Flask, jsonify, make_response, redirect, request, Response)
from urllib3.exceptions import InsecureRequestWarning

import merge

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.yaml')
HOME_CACHE_FILE = os.path.join(BASE_DIR, 'home_cache.yaml')
# 机场配置缓存文件（成功拉取后落盘，进程重启后可直接命中，无需等首次拉取）
SUBCACHE_FILE = os.path.join(BASE_DIR, 'sub_cache.json')

# 后台加载的默认节流间隔（秒）：上次加载距今小于该值就不再触发加载
DEFAULT_CACHE_TTL = 60
# 请求触发加载后最多等待的秒数，超时直接返回
LOAD_WAIT_TIMEOUT = 3
# 单次拉取超时：(连接, 读取)
FETCH_TIMEOUT = (5, 50)

# 订阅地址：config.yaml 可配置 sub_url（默认）与 sub_url1 ~ sub_url5（备用）。
# 请求 ?sub_url=N（1~5）选择第 N 个，未传或回退时使用 sub_url。
SUB_URL_COUNT = 5
SUB_URL_KEYS = ['sub_url'] + ['sub_url%d' % i for i in range(1, SUB_URL_COUNT + 1)]

# 控制配置项（不会出现在输出的 Clash 配置中，合并前会被剥离）
CONTROL_KEYS = {
    'api_path',                     # 订阅接口路径（修改需重启）
    'password',                     # 【兼容旧配置】接口访问密码（明文，加载后自动升级为哈希）
    'password_hash',                # 接口访问密码的哈希（pbkdf2_sha256$...，唯一推荐写法）
    'basic_auth',                   # 动态更新 Home 节点 IP/端口 的基础认证 username:password
    'server_url',                   # 获取 Home 节点最新 IP/端口 的服务地址
    'cache_ttl',                    # 机场配置缓存时长（秒，0 表示不过期）
    # 以下三项已废弃：输出顶层 key 完全取自本地配置，无需再剥离；代理组与规则
    # 也只取本地配置，无需再排除/合并机场的组。
    # 仍列在控制项中，只为把旧配置里的残留剥离掉，避免泄漏进输出的 Clash 配置。
    'remove_keys',
    'exclude_groups',
    'merge_groups',
} | set(SUB_URL_KEYS)               # sub_url / sub_url1 ~ sub_url5

app = Flask(__name__)


# ===================== 日志 =====================
def configure_logging(app):
    fmt = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    formatter = logging.Formatter(fmt)

    gunicorn_error = logging.getLogger('gunicorn.error')
    gunicorn_access = logging.getLogger('gunicorn.access')

    if gunicorn_error.handlers:
        handlers = gunicorn_error.handlers[:]
        root_level = gunicorn_error.level
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
        handlers = logging.root.handlers[:]
        root_level = logging.root.level

    for h in handlers:
        h.setFormatter(formatter)

    logging.root.handlers = handlers
    logging.root.setLevel(root_level)

    app.logger.handlers = handlers[:]
    app.logger.setLevel(root_level)
    app.logger.propagate = False

    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.handlers = handlers[:]
    werkzeug_logger.setLevel(root_level)
    werkzeug_logger.propagate = False

    if gunicorn_access.handlers:
        gunicorn_access.handlers = handlers[:]
        gunicorn_access.setLevel(root_level)
        gunicorn_access.propagate = False


configure_logging(app)


@app.before_request
def log_request():
    g_start = time.time()
    raw = request.get_data(cache=True)
    try:
        body = raw.decode('utf-8', errors='replace')
    except Exception:
        body = '<binary>'
    if len(body) > 2000:
        body = body[:2000] + '...[truncated]'
    body = mask_secret_fields(body)

    from urllib.parse import urlencode
    args_multi = request.args.to_dict(flat=False)
    sanitized_args = {
        k: (['***'] if k.lower() == 'password' else v)
        for k, v in args_multi.items()
    }
    args_for_log = {
        k: (v[0] if isinstance(v, list) and len(v) == 1 else v)
        for k, v in sanitized_args.items()
    }
    query = mask_secret_fields(urlencode(sanitized_args, doseq=True))
    path = request.path + ('?' + query if query else '')

    logging.info(
        "INCOMING %s %s %s Headers=%s Args=%s Body=%s",
        request.remote_addr, request.method, path,
        dict(request.headers), args_for_log, body,
    )


# JSON / 表单里凡是名字带 password 的字段，值一律替换成 ***
_SECRET_FIELD_RE = re.compile(
    r'(["\']?[\w.-]*password[\w.-]*["\']?\s*[:=]\s*)'
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^&\s,}]+)',
    re.IGNORECASE)


def _mask_secret_value(m):
    """把值换成 `***`；带引号的值补回同种引号，免得把 JSON 文本写坏。"""
    val = m.group(2)
    return m.group(1) + ('%s***%s' % (val[0], val[0]) if val[:1] in ('"', "'")
                         else '***')


def mask_secret_fields(s):
    """把报文里形如 `password=xx` / `"new_password": "xx"` 的值抹成 `***`。

    请求日志是要长期留存（甚至外送）的明文文本，而登录表单、改密码接口、
    订阅请求都带着口令：`Body=password=abc123` 会把口令原样写进日志 ——
    相当于「换了个地方继续存明文」。所以**统一在写日志这一步**抹掉，
    任何新接口只要字段名里带 password 就自动受益。
    """
    if not s:
        return s
    return _SECRET_FIELD_RE.sub(_mask_secret_value, s)


# ===================== 配置加载 =====================
# 配置缓存：内容未变时直接返回上次结果，避免每个请求都读盘 + 解析 YAML。
# 用「文件内容哈希」而不是 mtime 作缓存键——mtime 粒度粗，保存后立刻重载会撞上
# 同一个时间戳从而读到旧值（实测踩到过），哈希则不会。
_config_lock = threading.RLock()
_config_cache = None      # (path, digest, control, template)
_config_dirty = True      # 由网页界面置位，强制下次读取时重新加载


# ===================== 访问密码：只保存哈希 =====================
# config.yaml 会被界面读到、被备份、被 git 提交——明文口令一旦泄漏，
# 等于把「订阅内容 + 配置编辑权」一起交出去。所以：
#   - 配置里**只存哈希**：`password_hash: pbkdf2_sha256$迭代次数$salt$b64(摘要)`；
#   - salt 每次随机：同一口令两次导出的哈希不同，拖库也凑不出彩虹表；
#   - 迭代 20 万次：单次校验约 100ms，离线爆破成本高到不划算（stdlib 实现，无新依赖）；
#   - **默认值为空**（未设置密码）：此时订阅接口一律拒绝，界面提示立即设置；
#   - 旧配置里的明文 `password` 仍然认（否则升级就把自己锁在门外），
#     但**首次加载就地改写成哈希**（见 migrate_legacy_password），明文不再落盘。
PW_MIN_LEN = 8            # 新密码最短长度
PW_MAX_LEN = 256
PBKDF2_ITERATIONS = 200000
_PW_ALGO = 'pbkdf2_sha256'
_HASH_KEY = 'password_hash'
_LEGACY_KEY = 'password'


def hash_password(pw, iterations=PBKDF2_ITERATIONS):
    """导出口令摘要：`pbkdf2_sha256$迭代次数$salt$摘要`（salt 每次随机）。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt, iterations)
    return '%s$%d$%s$%s' % (_PW_ALGO, iterations,
                            base64.b64encode(salt).decode('ascii'),
                            base64.b64encode(dk).decode('ascii'))


def verify_password(pw, stored):
    """校验明文口令：匹配哈希格式的存储值，也认旧配置里的明文。

    空口令、空存储值一律算失败：未设置密码时不能被 `?password=` 空值绕过。
    """
    stored = str(stored or '')
    pw = pw or ''
    if not stored:
        return False
    if '$' not in stored:
        # 旧配置的明文口令（加载时已尝试自动升级，这里只做兜底）
        return hmac.compare_digest(pw.encode('utf-8'), stored.encode('utf-8'))
    try:
        algo, iters, salt_b64, hash_b64 = stored.split('$', 3)
        if algo != _PW_ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(hash_b64)
    except Exception:
        logging.warning("password_hash 无法解析，按校验失败处理")
        return False
    got = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt, int(iters))
    return hmac.compare_digest(got, want)


def stored_password(control):
    """配置里已保存的密码串（哈希优先，兼容旧明文）；未设置时返回 ''。"""
    return str(control.get(_HASH_KEY) or control.get(_LEGACY_KEY) or '')


def write_config_file(path, text):
    """写回配置：先备份 .bak，再 tmp → replace 原子替换。

    原子替换保证写一半断电也只会留下旧文件或新文件，不会出现半个文件。
    """
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                old = f.read()
            with open(path + '.bak', 'w', encoding='utf-8') as f:
                f.write(old)
        except Exception as e:
            logging.warning("备份原配置失败（继续写入）: %s", e)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def migrate_legacy_password(path, control):
    """旧配置里的明文 `password` → `password_hash`（原地改写，明文不再落盘）。

    只在「有明文、且尚没有哈希」时执行一次。行位置与行尾注释都保留，
    界面上没有的字段也不会被整理掉；改写失败（配置只读挂载等）不影响本次使用
    ——明文仍然有效，下次加载会再试一次。
    """
    plain = str(control.get(_LEGACY_KEY) or '')
    if not plain or str(control.get(_HASH_KEY) or ''):
        return control

    hashed = hash_password(plain)
    try:
        with _config_lock:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
            new_lines = []
            replaced = False
            for line in text.splitlines(keepends=True):
                stripped = line.rstrip('\n').rstrip('\r')
                # 只认 0 缩进的 `password:`：节点里的 `password: xxx` 是节点自己的东西
                if not replaced and not line[:1].isspace() \
                        and stripped.startswith(_LEGACY_KEY + ':'):
                    idx = stripped.find(' #')
                    comment = stripped[idx:] if idx >= 0 else ''
                    new_lines.append('%s: %s%s\n' % (_HASH_KEY, hashed, comment))
                    replaced = True
                else:
                    new_lines.append(line)
            if not replaced:
                return control
            write_config_file(path, ''.join(new_lines))
        logging.warning("检测到明文 password，已就地升级为 password_hash"
                        "（%s 中不再保存明文）", os.path.basename(path))
    except Exception as e:
        logging.warning("明文密码升级为哈希失败，本次继续使用旧配置: %s", e)
        return control

    control = dict(control)
    control.pop(_LEGACY_KEY, None)
    control[_HASH_KEY] = hashed
    return control


def read_yaml_config(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logging.error("读取 YAML 配置失败 %s: %s", file_path, e)
        return None


def load_local_config(path=CONFIG_FILE):
    """读取 config.yaml，拆分为 (control 控制项, template Clash 模板)。

    带缓存：内容未变时直接返回上次结果（避免每个请求重复解析）；
    文件被外部修改（或网页界面保存时置位 _config_dirty）后自动重新加载。
    """
    global _config_cache, _config_dirty

    if path != CONFIG_FILE:
        raw = read_yaml_config(path) or {}
        control = {k: raw.get(k) for k in CONTROL_KEYS if k in raw}
        template = {k: v for k, v in raw.items() if k not in CONTROL_KEYS}
        return control, template

    with _config_lock:
        try:
            with open(path, 'rb') as f:
                digest = hashlib.sha1(f.read()).hexdigest()
        except Exception:
            digest = None

        if _config_cache and not _config_dirty and _config_cache[0] == path \
                and _config_cache[1] == digest:
            return _config_cache[2], _config_cache[3]

        raw = read_yaml_config(path) or {}
        control = {k: raw.get(k) for k in CONTROL_KEYS if k in raw}
        template = {k: v for k, v in raw.items() if k not in CONTROL_KEYS}
        # 旧配置里的明文口令：就地升级为哈希（失败也不影响本次使用）
        control = migrate_legacy_password(path, control)
        _config_cache = (path, digest, control, template)
        _config_dirty = False
        return control, template


def invalidate_config():
    """置位配置缓存失效标记，使下次 load_local_config() 重新读盘（保存后调用）。"""
    global _config_dirty
    with _config_lock:
        _config_dirty = True


def control_int(control, key, default):
    """读取控制项里的整数，非法或缺失时用默认值。"""
    try:
        val = int(control.get(key, default))
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def resolve_sub_url(requested, control):
    """把请求参数 sub_url 解析为真实订阅地址。

    - `?sub_url=N`（1~5）：取 config.yaml 中的 sub_urlN；该项为空时回退 sub_url；
    - 传入完整地址（含非数字）：直接使用，兼容旧用法；
    - 数字超出 1~5 或未传：使用 sub_url。
    """
    requested = str(requested or '').strip()
    if not requested:
        return control.get('sub_url') or ''

    if requested.isdigit():
        idx = int(requested)
        if 1 <= idx <= SUB_URL_COUNT:
            url = control.get('sub_url%d' % idx) or ''
            if url:
                logging.info("按请求选择订阅地址: sub_url=%s -> sub_url%d", requested, idx)
                return url
            logging.info("sub_url%d 未配置，回退默认 sub_url", idx)
            return control.get('sub_url') or ''
        logging.info("请求的 sub_url=%s 超出 1~%d 范围，使用默认 sub_url",
                     requested, SUB_URL_COUNT)
        return control.get('sub_url') or ''

    return requested


# ===================== 订阅获取：后台加载 + 内存/文件缓存 =====================
# sub_url -> {'ts': float, 'data': dict, 'userinfo': str}
remote_cache = {}
# sub_url -> threading.Thread：正在加载中的订阅（同一订阅同时只允许一个加载线程）
_loading = {}
# sub_url -> threading.Event：加载完成后置位，供等待方唤醒
_load_done = {}
_state_lock = threading.RLock()

# 拉取订阅统一使用的请求头
SUB_HEADER = {'Accept': '*/*', 'User-Agent': 'clash-verge/v2.4.7'}


def _load_cache_file():
    """进程启动时读入落盘的订阅缓存（成功写入的缓存永不失效）。"""
    try:
        with open(SUBCACHE_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        logging.error("读取订阅缓存文件失败 %s: %s", SUBCACHE_FILE, e)
        return

    if not isinstance(raw, dict):
        return
    with _state_lock:
        for sub_url, item in raw.items():
            if isinstance(item, dict) and isinstance(item.get('data'), dict):
                remote_cache[sub_url] = {
                    'ts': float(item.get('ts') or 0),
                    'data': item['data'],
                    'userinfo': item.get('userinfo') or '',
                }
    if remote_cache:
        logging.info("已从缓存文件加载 %d 个订阅: %s",
                     len(remote_cache), list(remote_cache))


def _save_cache_file():
    """把内存缓存整体落盘（原子替换，避免写入中途被读到半截文件）。"""
    with _state_lock:
        snapshot = {k: dict(v) for k, v in remote_cache.items()}
    tmp = SUBCACHE_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp, SUBCACHE_FILE)
    except Exception as e:
        logging.error("写入订阅缓存文件失败 %s: %s", SUBCACHE_FILE, e)


def get_cached(sub_url):
    """读取缓存，返回 (data, userinfo, age)；无缓存时 age 为 None。

    缓存永不失效，age 只用于判断是否需要触发重新加载。
    """
    with _state_lock:
        cached = remote_cache.get(sub_url)
        if not cached:
            return None, '', None
        return deepcopy(cached['data']), cached['userinfo'], time.time() - cached['ts']


def _fetch_and_cache(sub_url):
    """加载线程主体：拉取机场订阅并更新缓存。失败时不改动任何缓存。"""
    try:
        resp = requests.get(sub_url, headers=SUB_HEADER, verify=False,
                            timeout=FETCH_TIMEOUT)
        resp.encoding = 'utf-8'
        if resp.status_code != 200:
            raise ValueError('意外的状态码 %s' % resp.status_code)
        userinfo = resp.headers.get('subscription-userinfo', '')
        data = merge.parse_clash(resp.text)
        with _state_lock:
            remote_cache[sub_url] = {'ts': time.time(), 'data': data,
                                     'userinfo': userinfo}
        _save_cache_file()
        logging.info("加载成功：订阅 %s，节点 %d 个（缓存已更新）",
                     sub_url, len(data.get('proxies') or []))
    except Exception as e:
        logging.error("加载失败：订阅 %s: %s（沿用原缓存，不做改动）", sub_url, e)
    finally:
        with _state_lock:
            _loading.pop(sub_url, None)
            ev = _load_done.pop(sub_url, None)
        if ev:
            ev.set()


def trigger_load(sub_url):
    """触发一次后台加载；正在加载中的订阅不重复触发。

    :return: 本次是否真的启动了新线程
    """
    if not sub_url:
        return False
    with _state_lock:
        if sub_url in _loading:
            logging.info("订阅 %s 正在加载中，本次不重复触发", sub_url)
            return False
        ev = threading.Event()
        _load_done[sub_url] = ev
        th = threading.Thread(target=_fetch_and_cache, args=(sub_url,),
                              name='sub-loader', daemon=True)
        _loading[sub_url] = th
    logging.info("触发后台加载订阅: %s", sub_url)
    th.start()
    return True


def wait_load(sub_url, timeout=LOAD_WAIT_TIMEOUT):
    """等待该订阅的加载完成，返回是否已完成（在超时前完成 / 根本没有待等事件）。

    注意：加载线程结束时会把自己从 _load_done 里摘掉。若此时才来查询，
    get() 拿不到事件 —— 那是「已经加载完」而非「没在等」，必须按完成处理，
    否则会误报「等待超过 3 秒」，把排查方向带偏。
    """
    with _state_lock:
        ev = _load_done.get(sub_url)
        if ev is None:
            return sub_url not in _loading     # 已摘除且不在加载中 = 已完成
    return ev.wait(timeout)


def refresh_subscription(sub_url, control):
    """按缓存优先策略取回用于合并的订阅配置，返回 (config|None, userinfo)。

    - 有缓存且上次加载距今小于 cache_ttl → 直接返回缓存（不等待）；
    - 否则触发后台加载并最多等待 LOAD_WAIT_TIMEOUT 秒：
      等到新内容就返回新内容，超时（或加载失败）则返回原缓存；
    - 完全没有缓存 → 返回 (None, '')，由调用方生成最小配置文件。
    """
    ttl = control_int(control, 'cache_ttl', DEFAULT_CACHE_TTL)
    data, userinfo, age = get_cached(sub_url)

    if data is not None and age is not None and age < ttl:
        logging.info("缓存命中：订阅 %s（距今 %d 秒 < %d 秒），直接返回",
                     sub_url, int(age), ttl)
        return data, userinfo

    # 缓存缺失或已超过 cache_ttl：触发加载并有限等待
    trigger_load(sub_url)
    started = time.time()
    finished = wait_load(sub_url, LOAD_WAIT_TIMEOUT)
    waited = time.time() - started
    if not finished:
        logging.warning("等待订阅 %s 加载超过 %d 秒（已等 %.1f 秒），先返回现有内容",
                        sub_url, LOAD_WAIT_TIMEOUT, waited)
    else:
        logging.info("订阅 %s 加载完成，等待 %.2f 秒", sub_url, waited)

    new_data, new_userinfo, new_age = get_cached(sub_url)
    if new_age is not None and (age is None or new_age < age):
        logging.info("使用刚加载的订阅：%s（节点 %d 个）",
                     sub_url, len((new_data or {}).get('proxies') or []))
        return new_data, new_userinfo

    if new_data is not None:
        logging.info("沿用原缓存（加载未在 %d 秒内完成或加载失败）: %s",
                     LOAD_WAIT_TIMEOUT, sub_url)
        return new_data, new_userinfo

    logging.info("无可用缓存: %s", sub_url)
    return None, ''


# 兼容旧调用方式：直接拉取（不走缓存策略），供自测 / 排查使用
def fetch_remote(sub_url, ttl=DEFAULT_CACHE_TTL):
    """拉取机场订阅并更新缓存，返回 (dict|None, userinfo)。失败返回缓存。"""
    _fetch_and_cache(sub_url)
    data, userinfo, _ = get_cached(sub_url)
    return data, userinfo


# ===================== Home 节点动态 IP =====================
def load_home_cache():
    # home_cache.yaml 初始可能不存在，缺失时静默返回空（不打印 ERROR）
    try:
        with open(HOME_CACHE_FILE, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.error("读取 Home 缓存失败 %s: %s", HOME_CACHE_FILE, e)
        return {}


def save_home_cache(data):
    try:
        with open(HOME_CACHE_FILE, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False)
    except Exception as e:
        logging.error("写入 Home 缓存失败: %s", e)


def apply_home_cache(template):
    """将动态刷新的 Home 节点 IP/端口应用到本地模板（仅内存，不修改 config.yaml）。"""
    home = load_home_cache()
    if not home:
        return
    name = home.get('name')
    for p in template.get('proxies') or []:
        if p.get('name') == name:
            if 'server' in home:
                p['server'] = home['server']
            if 'port' in home:
                p['port'] = int(home['port'])


def refresh_proxy_ip_port(control):
    """从 server_url 获取 Home 节点最新 IP/端口并持久化到 home_cache.yaml。"""
    basic_auth = control.get('basic_auth')
    url = control.get('server_url')
    if not url:
        return
    try:
        headers = {}
        if basic_auth:
            encoded = base64.b64encode(basic_auth.encode('utf-8')).decode('utf-8')
            headers['Authorization'] = 'Basic ' + encoded
        resp = requests.get(url, headers=headers, verify=False, timeout=(5, 15))
        resp.encoding = 'utf-8'
        if resp.status_code != 200:
            logging.info("刷新 Home IP 失败，状态码: %s", resp.status_code)
            return
        data = resp.json()
        ip = data.get('ip')
        port = data.get('port')
        if not ip or not port:
            logging.info("刷新 Home IP 返回为空")
            return
        save_home_cache({'name': 'Home', 'server': ip, 'port': int(port)})
        logging.info("刷新 Home IP 成功 -> %s:%s", ip, port)
    except Exception as e:
        logging.error("刷新 Home IP 异常: %s", e)


# ===================== 生成最小配置文件 =====================
def build_minimal_config(template):
    """机场订阅与缓存都不可用时，根据本地配置生成最小配置文件。

    本地模板本身就是最小可用配置：只含本地节点（Home）与本地代理组 / 规则，
    不含任何机场内容。本地代理组里的 * 表达式在此分支展开为空，
    组内保留其余本地成员（整组为空时兜底 DIRECT），客户端仍能正常分流。
    """
    logging.info("无订阅可用，根据本地配置生成最小配置文件")
    return deepcopy(template)


def load_subscription(sub_url, control, template):
    """获取待转换的订阅配置，返回 (config, userinfo)。

    缓存优先：命中新鲜缓存直接返回；否则触发后台加载并最多等 3 秒；
    始终无缓存（且无 sub_url）时退化为本地最小配置文件。

    :param sub_url: 机场订阅地址（为空则跳过加载，直接生成最小配置文件）
    :param control: 控制项配置（取 cache_ttl 作为加载节流间隔）
    :param template: 本地配置模板（退化为最小配置时据此生成）
    """
    if not sub_url:
        logging.info("未配置 sub_url，跳过订阅加载")
        return build_minimal_config(template), ''

    config, userinfo = refresh_subscription(sub_url, control)
    if config is None:
        return build_minimal_config(template), ''
    return config, userinfo


# ===================== 第 4 步：订阅文件转换（合并入口） =====================
def convert(sub_url, control, template):
    """根据本地配置转换订阅文件。

    转换规则（详见 merge.merge_configs）：
    - 输出顶层 key 完全取自本地配置，机场除代理节点外的内容一律丢弃；
    - 代理节点 = 本地节点 + 机场节点（按 name 去重，本地同名优先）；
    - 机场节点的插入位置由本地配置中的 merge.REMOTE_PROXIES 表达式（`*`）指定。
    """
    apply_home_cache(template)

    if any(control.get(k) for k in ('remove_keys', 'exclude_groups', 'merge_groups')):
        logging.info("remove_keys / exclude_groups / merge_groups 已废弃"
                     "（输出只用本地配置），本次忽略")

    remote, userinfo = load_subscription(sub_url, control, template)

    merged = merge.merge_configs(template, remote)

    remote_count = len((remote or {}).get('proxies') or [])
    logging.info(
        "转换完成，节点 %d 个（含机场 %d 个）/ 代理组 %d 个 / 规则 %d 条",
        len(merged.get('proxies') or []), remote_count,
        len(merged.get('proxy-groups') or []),
        len(merged.get('rules') or []),
    )
    if remote_count and not merge.uses_remote_placeholder(template):
        logging.warning(
            "本地配置未使用 %s 表达式，机场 %d 个节点未被任何代理组引用",
            merge.REMOTE_PROXIES, remote_count)

    group_names = merge.names_of(merged.get('proxy-groups') or [])
    node_names = merge.names_of(merged.get('proxies') or [])

    dangling = merge.find_dangling_rules(merged.get('rules') or [], group_names, node_names)
    if dangling:
        logging.warning(
            "%d 条规则的目标不存在（需在本地配置中定义对应的组或节点）: %s",
            len(dangling), dangling[:3],
        )

    bad_members = merge.find_dangling_members(
        merged.get('proxy-groups') or [], group_names, node_names)
    if bad_members:
        logging.warning(
            "%d 个代理组成员不存在（检查拼写；旧版表达式 __REMOTE_PROXIES__ "
            "现已改为 \"%s\"）: %s",
            len(bad_members), merge.REMOTE_PROXIES, bad_members[:3],
        )
    return yaml.dump(merged, allow_unicode=True, sort_keys=False), userinfo


# ===================== 路由 =====================
try:
    _c, _ = load_local_config()
    API_PATH = '/' + str(_c.get('api_path', 'api')).lstrip('/')
except Exception:
    API_PATH = '/api'

# 进程启动时读入落盘缓存，重启后首个请求即可直接命中，无需等待拉取
_load_cache_file()


@app.route(API_PATH)
def api():
    control, template = load_local_config()
    stored = stored_password(control)
    if not stored:
        # 没设密码时的默认行为是「一律拒绝」：宁可配不出来，也不能把订阅交给陌生人
        logging.warning("未设置访问密码，订阅接口拒绝请求（请到 /ui 里设置密码）")
        return 'Hello World!'
    if not verify_password(request.args.get('password'), stored):
        return 'Hello World!'

    refresh_proxy_ip_port(control)

    sub_url = resolve_sub_url(request.args.get('sub_url'), control)
    clash_yaml, userinfo = convert(sub_url, control, template)
    if clash_yaml is None:
        return 'Hello World!'

    headers = {}
    if userinfo:
        headers['subscription-userinfo'] = userinfo
    else:
        logging.info("本次响应未携带 subscription-userinfo（客户端将保留上次显示值）")
    return Response(clash_yaml, mimetype='text/plain', headers=headers)


@app.route('/health')
def health():
    return 'ok'


# ===================== 网页配置界面 =====================
# 界面已暴露到公网，因此**必须登录**才能访问（唯一例外：还没设置密码的首次部署，
# 否则连「设置密码」这一步都进不去）。密码只在哈希形态下保存与校验；
# 登录后发放一个 HMAC 签名的短期 cookie，避免每次请求都带明文密码。
# 保存后直接写回 config.yaml 并热重载，无需重启。

# ===================== 网页界面登录 =====================
# 设计要点：
#  - 密码复用 config.yaml 的访问控制项（与订阅接口同一个），不再引入第二套口令；
#  - 只用签名 cookie 记登录态，cookie 里放**过期时间戳**，不含密码本身；
#  - 签名密钥进程启动时随机生成 → 重启即全部登出（简单且安全，不用落盘密钥）；
#  - 签名里带上当前密码的指纹 → **改密码后其他会话立即失效**（改完当场登出别人）；
#  - 配置里只存 PBKDF2 哈希，校验走 hmac.compare_digest（避开时序侧信道）。
UI_SESSION_COOKIE = 'mysub_ui'
UI_SESSION_TTL = 7 * 24 * 3600          # 7 天
# 每次进程启动随机生成：重启后旧 cookie 自动失效
UI_SECRET = secrets.token_bytes(32)


def _ui_stored_password():
    """当前保存的密码串（哈希，热重载后立即生效）；未设置时返回 ''。"""
    try:
        control, _ = load_local_config()
        return stored_password(control)
    except Exception:
        return ''


def _pw_fingerprint():
    """当前密码的指纹。

    它参与 cookie 签名后，「改密码」这件事会自然让之前发出的 cookie 全部失效，
    不必另外维护会话黑名单，也没有多余状态要落盘。
    """
    return hashlib.sha256(_ui_stored_password().encode('utf-8')).hexdigest()[:16]


def _ui_sign(ts):
    """对「过期时间戳 + 密码指纹」签名。"""
    payload = '%d.%s' % (ts, _pw_fingerprint())
    return hmac.new(UI_SECRET, payload.encode('utf-8'), hashlib.sha256).hexdigest()


def _ui_make_token():
    ts = int(time.time()) + UI_SESSION_TTL
    return '%d.%s' % (ts, _ui_sign(ts))


def _ui_check_token(token):
    """校验 cookie：签名对且未过期才算登录。"""
    if not token or '.' not in token:
        return False
    ts_s, sig = token.rsplit('.', 1)
    try:
        ts = int(ts_s)
    except ValueError:
        return False
    if ts < time.time():
        return False
    return hmac.compare_digest(sig, _ui_sign(ts))


def ui_authed():
    """当前请求是否已登录。"""
    return _ui_check_token(request.cookies.get(UI_SESSION_COOKIE, ''))


def ui_login_required():
    """未登录时的统一响应：网页跳登录页，接口返回 401 JSON。

    例外：**还没设置密码**时直接放行——否则首次部署的人连「设置密码」的界面
    都进不去（又不想回到手工编辑 config.yaml 的老路上）。此时订阅接口仍然是
    全拒状态，敞开的只是这一个界面，且页面顶部会用醒目横幅催一句。
    """
    if ui_authed():
        return None
    if not _ui_stored_password():
        return None
    if request.path.startswith('/ui/') and request.path != '/ui/login':
        return jsonify({'ok': False, 'error': '未登录或登录已过期，请刷新页面重新登录'}), 401
    return redirect('/ui/login?next=' + request.path, code=302)


UI_LOGIN_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MySubConvert 登录</title>
<style>
  :root { --bd:#dcdfe6; --mut:#8a94a6; --bg:#f5f7fa; --pri:#2f6fed; --err:#c0392b; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
         background:var(--bg); color:#252b37; }
  .box { background:#fff; border:1px solid var(--bd); border-radius:10px;
         padding:28px 26px; width:340px; }
  h1 { font-size:16px; margin:0 0 20px; font-weight:600; }
  label { font-size:13px; color:#3d4555; font-weight:500; display:block; margin-bottom:6px; }
  input { width:100%; border:1px solid var(--bd); border-radius:6px; padding:10px 11px;
          font-size:14px; font-family:inherit; }
  input:focus { outline:none; border-color:var(--pri); box-shadow:0 0 0 3px rgba(47,111,237,.12); }
  button { width:100%; margin-top:16px; border:1px solid var(--pri); background:var(--pri);
           color:#fff; border-radius:6px; padding:10px; font-size:14px; cursor:pointer; }
  button:hover { opacity:.9; }
  #err { color:var(--err); font-size:13px; margin-top:12px; display:none; }
  .hint { color:var(--mut); font-size:12px; margin-top:14px; }
</style>
</head>
<body>
<form class="box" method="post" action="/ui/login">
  <h1>MySubConvert 配置</h1>
  <!-- 用户名只为了满足浏览器的「保存密码」启发式：没有 username 字段时
       Chrome/Edge 不会弹出保存密码提示，也不会自动填充。
       这里**不做任何校验**，值被后端直接忽略。 -->
  <label for="un">用户名</label>
  <input type="text" id="un" name="username" autocomplete="username"
         placeholder="任意填写，不校验" autofocus>
  <label for="pw" style="margin-top:14px">访问密码</label>
  <input type="password" id="pw" name="password" autocomplete="current-password">
  <input type="hidden" name="next" value="__NEXT__">
  <button type="submit">登录</button>
  <div id="err">__ERR__</div>
  <p class="hint">用户名任意填写、不做校验；密码与订阅接口 ?password= 是同一个，
    配置里只保存它的哈希（看不到明文）。</p>
</form>
</body>
</html>
"""


@app.route('/ui/login', methods=['GET', 'POST'])
def ui_login():
    stored = _ui_stored_password()
    # 还没设密码：没有东西可以「登录」，直接去界面把它设上
    if request.method == 'GET' and not stored and not ui_authed():
        return redirect('/ui', code=302)

    if request.method == 'GET':
        if ui_authed():
            return redirect(request.args.get('next') or '/ui', code=302)
        nxt = request.args.get('next') or '/ui'
        html = UI_LOGIN_HEAD.replace('__NEXT__', _html_escape(nxt))
        return Response(html.replace('__ERR__', ''), mimetype='text/html')

    pw = request.form.get('password', '')
    nxt = request.form.get('next') or '/ui'
    # 只允许站内跳转，避免被当成开放重定向跳板
    if not nxt.startswith('/') or nxt.startswith('//'):
        nxt = '/ui'
    if stored and not verify_password(pw, stored):
        logging.warning("网页界面登录失败（来源 %s）", request.remote_addr)
        html = UI_LOGIN_HEAD.replace('__NEXT__', _html_escape(nxt))
        return Response(html.replace('__ERR__', '密码错误'), status=403,
                        mimetype='text/html')

    resp = make_response(redirect(nxt, code=302))
    resp.set_cookie(UI_SESSION_COOKIE, _ui_make_token(), max_age=UI_SESSION_TTL,
                    httponly=True, samesite='Lax')
    logging.info("网页界面登录成功（来源 %s）", request.remote_addr)
    return resp


@app.route('/ui/logout')
def ui_logout():
    resp = make_response(redirect('/ui/login', code=302))
    resp.delete_cookie(UI_SESSION_COOKIE)
    return resp


def _html_escape(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


UI_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MySubConvert 配置</title>
<style>
  :root { --bd:#dcdfe6; --mut:#8a94a6; --bg:#f5f7fa; --pri:#2f6fed;
          --ok:#1a7f37; --warn:#b45309; --err:#c0392b; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
         background:var(--bg); color:#252b37; }
  header { background:#fff; border-bottom:1px solid var(--bd); padding:14px 22px;
           display:flex; align-items:center; gap:14px; position:sticky; top:0; z-index:9;
           flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  header .grow { flex:1; }
  button { border:1px solid var(--bd); background:#fff; border-radius:6px;
           padding:7px 15px; cursor:pointer; font-size:14px; }
  button:hover { border-color:var(--pri); color:var(--pri); }
  button.pri { background:var(--pri); border-color:var(--pri); color:#fff; }
  button.pri:hover { opacity:.88; color:#fff; }
  button.danger:hover { border-color:var(--err); color:var(--err); }
  main { padding:20px 22px 70px; max-width:1180px; margin:0 auto; }
  .card { background:#fff; border:1px solid var(--bd); border-radius:10px;
          margin-bottom:18px; overflow:hidden; }
  .card > h2 { font-size:14px; margin:0; padding:12px 16px; border-bottom:1px solid var(--bd);
               font-weight:600; display:flex; align-items:center; gap:10px; }
  .card > h2 .tag { font-size:12px; color:var(--mut); font-weight:400; }
  .card .body { padding:16px; }
  .hint { font-size:12px; color:var(--mut); }
  label { font-size:13px; color:#3d4555; font-weight:500; display:block; margin-bottom:6px; }
  select, input[type=text], input[type=password] { border:1px solid var(--bd);
        border-radius:6px; padding:9px 11px; font-size:13px; font-family:inherit;
        background:#fff; }
  input[type=text], input[type=password] { width:100%; }
  select:focus, input[type=text]:focus, input[type=password]:focus { outline:none;
        border-color:var(--pri); box-shadow:0 0 0 3px rgba(47,111,237,.12); }
  input[type=password]:disabled { background:#f5f7fa; color:var(--mut); }
  .row { display:flex; gap:14px; align-items:flex-end; flex-wrap:wrap; }
  .row .col { display:flex; flex-direction:column; }
  .row .col.grow { flex:1; min-width:260px; }
  #msg { padding:9px 14px; border-radius:6px; font-size:13px; display:none; }
  #msg.ok { background:#e8f6ec; color:var(--ok); border:1px solid #a3d9b1; }
  #msg.err { background:#fdecec; color:var(--err); border:1px solid #f3b3b3; }
  .stat { font-size:12px; color:var(--mut); display:flex; gap:16px; flex-wrap:wrap; }
  .stat b { color:#252b37; font-weight:600; }
  /* 未设置密码的顶部横幅：默认安装就是空密码，必须催着改 */
  #nopw { max-width:1180px; margin:16px auto 0; padding:11px 15px;
          border:1px solid #f0cf9a; background:#fff7e8; border-radius:10px;
          color:var(--warn); font-size:13px; display:none; align-items:center; gap:12px; }
  #nopw .grow { flex:1; }
  #nopw button { padding:5px 12px; font-size:13px; }

  /* ---- 右上角「访问密码」弹窗 ---- */
  .mask { position:fixed; inset:0; background:rgba(20,26,38,.45); z-index:50;
          display:none; align-items:center; justify-content:center; padding:20px; }
  .modal { background:#fff; border-radius:12px; width:100%; max-width:540px;
           box-shadow:0 18px 48px rgba(20,26,38,.25); overflow:hidden; }
  .modal h3 { margin:0; padding:14px 18px; font-size:15px; font-weight:600;
              border-bottom:1px solid var(--bd); display:flex; align-items:center; gap:10px; }
  .modal h3 .tag { font-size:12px; color:var(--mut); font-weight:400; }
  .modal .body { padding:18px; }
  .modal .body .row .col.grow { min-width:200px; }
  .modal .body .row .col:not(.grow) { flex:1; min-width:200px; }
  .modal .body input[type=password] { margin-top:0; }
  .modal-acts { display:flex; gap:10px; align-items:center; margin-top:18px; }
  .modal-acts .grow { flex:1; }
  #btn-pw-open.attn { border-color:var(--warn); color:var(--warn); background:#fff7e8; }
  #btn-pw-open.attn:hover { border-color:var(--warn); color:var(--warn); opacity:.85; }

  /* ---- 折叠卡片：本地节点 / 其他配置 默认收起 ---- */
  details.card > summary { font-size:14px; padding:12px 16px; font-weight:600;
               display:flex; align-items:center; gap:10px; cursor:pointer;
               user-select:none; list-style:none; }
  details.card > summary::-webkit-details-marker { display:none; }
  details.card > summary .tag { font-size:12px; color:var(--mut); font-weight:400; }
  details.card > summary .tip { margin-left:auto; font-size:12px; color:var(--mut);
                                font-weight:400; }
  details.card > summary:hover { color:var(--pri); }
  details.card > .body { border-top:1px solid var(--bd); }

  /* ---- 规则编辑表格 ---- */
  .rules-toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap;
                   margin-bottom:12px; }
  .rules-toolbar .grow { flex:1; }
  table.rules { width:100%; border-collapse:separate; border-spacing:0; }
  table.rules th { font-size:12px; color:var(--mut); font-weight:500; text-align:left;
                   padding:0 8px 6px; white-space:nowrap; }
  table.rules td { padding:4px 4px; vertical-align:middle; }
  table.rules tr.dragging { opacity:.4; }
  table.rules tr.drop-before td { border-top:2px solid var(--pri); }
  table.rules tr.drop-after td { border-bottom:2px solid var(--pri); }
  table.rules input, table.rules select { padding:7px 9px; font-size:13px; }
  table.rules .num { font-size:12px; color:var(--mut); text-align:right; width:38px;
                     padding-right:8px; font-variant-numeric:tabular-nums; }
  .handle { cursor:grab; color:var(--mut); user-select:none; width:26px; text-align:center;
            font-size:15px; }
  .handle:active { cursor:grabbing; }
  .acts { white-space:nowrap; width:1%; }
  .acts button { padding:4px 8px; font-size:12px; line-height:1.2; }
  .row-bad input, .row-bad select { border-color:#f0a0a0; background:#fff8f8; }
  .empty { text-align:center; color:var(--mut); font-size:13px; padding:22px 0; }
  .badge { font-size:11px; padding:2px 7px; border-radius:10px; border:1px solid var(--bd);
           color:var(--mut); }
  .badge.set { background:#eef4ff; border-color:#c3d6fb; color:var(--pri); }
  details.adv { margin-top:14px; }
  details.adv > summary { cursor:pointer; color:var(--pri); font-size:13px; }
  details.adv textarea { width:100%; margin-top:10px; border:1px solid var(--bd);
        border-radius:6px; padding:10px; font-family:ui-monospace,Consolas,monospace;
        font-size:12.5px; line-height:1.6; resize:vertical; }
  main .card > .body > textarea { width:100%; margin-top:12px; border:1px solid var(--bd);
        border-radius:6px; padding:10px; font-family:ui-monospace,Consolas,monospace;
        font-size:12.5px; line-height:1.6; resize:vertical; }
  table.rules textarea { padding:6px 8px; border:1px solid var(--bd); border-radius:6px;
        font-size:13px; }
</style>
</head>
<body>
<header>
  <h1>MySubConvert 配置</h1>
  <span class="stat" id="stat"></span>
  <span class="grow"></span>
  <span id="msg"></span>
  <button id="btn-pw-open">访问密码</button>
  <button id="btn-reload">重新载入</button>
  <button id="btn-save" class="pri">保存并生效</button>
  <a href="/ui/logout"><button type="button">退出</button></a>
</header>
<div id="nopw">
  <span class="grow">⚠️ 当前<b>没有</b>访问密码：任何人都能打开本页并修改配置
    （订阅接口在此期间一律拒绝）。</span>
  <button type="button" id="nopw-set">立即设置</button>
</div>
<main>
  <!-- ============ 订阅链接 ============ -->
  <div class="card">
    <h2>订阅链接 <span class="tag">选择槽位后填写地址；槽位对应客户端请求 ?sub_url=N</span></h2>
    <div class="body">
      <div class="row">
        <div class="col" style="min-width:220px">
          <label for="slot">配置哪个订阅</label>
          <select id="slot"></select>
        </div>
        <div class="col grow">
          <label for="slot-url">订阅地址<span class="hint">　留空表示不使用该槽位</span></label>
          <input type="text" id="slot-url" spellcheck="false"
                 placeholder="https://example.com/api/v1/client/subscribe?token=...">
        </div>
        <div class="col">
          <button id="btn-clear-url" class="danger">清空此项</button>
        </div>
      </div>
      <p class="hint" id="slot-note" style="margin:12px 0 0"></p>
    </div>
  </div>

  <!-- ============ 代理组 ============ -->
  <div class="card">
    <h2>代理组 <span class="tag">组名即规则里的「目标」；书写顺序 = 客户端里的显示顺序</span></h2>
    <div class="body">
      <div class="rules-toolbar">
        <span class="hint" id="pg-count"></span>
        <span class="grow"></span>
        <button id="btn-add-group">+ 新增代理组</button>
      </div>
      <table class="rules" id="pg-table">
        <thead>
          <tr>
            <th style="width:26px"></th>
            <th style="width:34px"></th>
            <th style="width:22%">名称</th>
            <th style="width:150px">类型</th>
            <th>成员（每行一个；<code>"*"</code> = 展开机场全部节点）</th>
            <th style="width:110px">操作</th>
          </tr>
        </thead>
        <tbody id="pg-body"></tbody>
      </table>
      <div class="empty" id="pg-empty" style="display:none">暂无代理组</div>
      <details class="adv">
        <summary>高级：直接编辑 proxy-groups 原文</summary>
        <p class="hint" style="margin:8px 0 0">
          表格只编辑名称 / 类型 / 成员；url-test 的 url、interval 等参数在原文里改。
          <b>改完点「用文本覆盖表格」</b>——保存时以表格为准，不点的话原文里的改动会被丢弃。
        </p>
        <textarea id="pg-raw" rows="8" spellcheck="false"></textarea>
        <div style="margin-top:10px">
          <button id="btn-pg-apply">用文本覆盖表格</button>
          <button id="btn-pg-sync">从表格同步到文本</button>
        </div>
      </details>
    </div>
  </div>

  <!-- ============ 代理规则 ============ -->
  <div class="card">
    <h2>代理规则 <span class="tag">自上而下匹配，首条命中生效；末行建议保留 MATCH 兜底</span></h2>
    <div class="body">
      <div class="rules-toolbar">
        <input type="text" id="rule-filter" spellcheck="false" placeholder="筛选规则（按类型 / 值 / 目标）"
               style="max-width:290px">
        <span class="hint" id="rules-count"></span>
        <span class="grow"></span>
        <button id="btn-add-rule">+ 新增规则</button>
        <button id="btn-add-match">+ 兜底 MATCH</button>
        <button id="btn-tidy" class="danger">清除无效行</button>
      </div>
      <table class="rules">
        <thead>
          <tr>
            <th style="width:26px"></th>
            <th style="width:34px"></th>
            <th style="width:172px">类型</th>
            <th style="width:34%">值</th>
            <th style="width:26%">目标（代理组 / 策略）</th>
            <th style="width:150px">操作</th>
          </tr>
        </thead>
        <tbody id="rules-body"></tbody>
      </table>
      <div class="empty" id="rules-empty" style="display:none">暂无规则，点「+ 新增规则」开始</div>
      <details class="adv">
        <summary>高级：直接编辑 rules 原文</summary>
        <p class="hint" style="margin:8px 0 0">
          上方表格与这里的文本是同一份数据，以最后一次编辑为准。支持任意
          Clash 规则语法（含 RULE-SET / GEOIP / PROCESS-NAME 等）。
        </p>
        <textarea id="rules-raw" rows="10" spellcheck="false"></textarea>
        <div style="margin-top:10px">
          <button id="btn-raw-apply">用文本覆盖表格</button>
          <button id="btn-raw-sync">从表格同步到文本</button>
        </div>
      </details>
    </div>
  </div>

  <!-- ============ 本地节点（不常用，放最底部，默认折叠） ============ -->
  <details class="card" id="card-px">
    <summary>本地节点 <span class="tag">不走机场、直接写在本地配置里的节点</span>
      <span class="tip">点击展开</span>
    </summary>
    <div class="body">
      <div class="rules-toolbar">
        <span class="hint" id="px-count"></span>
        <span class="grow"></span>
        <button id="btn-add-proxy">+ 新增节点</button>
      </div>
      <table class="rules" id="px-table">
        <thead>
          <tr>
            <th style="width:26px"></th>
            <th style="width:34px"></th>
            <th style="width:20%">名称</th>
            <th style="width:130px">类型</th>
            <th style="width:24%">服务器</th>
            <th style="width:100px">端口</th>
            <th style="width:110px">操作</th>
          </tr>
        </thead>
        <tbody id="px-body"></tbody>
      </table>
      <div class="empty" id="px-empty" style="display:none">暂无本地节点</div>
      <details class="adv">
        <summary>高级：直接编辑 proxies 原文</summary>
        <p class="hint" style="margin:8px 0 0">
          表格只编辑名称 / 类型 / 服务器 / 端口；密码、加密方式、udp 等协议参数在原文里改。
          <b>改完点「用文本覆盖表格」</b>——保存时以表格为准，不点的话原文里的改动会被丢弃。
        </p>
        <textarea id="px-raw" rows="8" spellcheck="false"></textarea>
        <div style="margin-top:10px">
          <button id="btn-px-apply">用文本覆盖表格</button>
          <button id="btn-px-sync">从表格同步到文本</button>
        </div>
      </details>
    </div>
  </details>

  <!-- ============ 其他配置（不常用，放最底部，默认折叠） ============ -->
  <details class="card" id="card-ex">
    <summary>其他配置 <span class="tag">端口 / DNS / 客户端行为等；按段编辑</span>
      <span class="tip">点击展开</span>
    </summary>
    <div class="body">
      <div class="row">
        <div class="col grow">
          <label for="ex-key">配置段</label>
          <select id="ex-key"></select>
        </div>
      </div>
      <p class="hint" id="ex-note" style="margin:10px 0 0"></p>
      <textarea id="ex-raw" rows="10" spellcheck="false"></textarea>
    </div>
  </details>
</main>

<!-- ============ 访问密码弹窗（入口在右上角） ============ -->
<div class="mask" id="pw-mask">
  <div class="modal">
    <h3>访问密码 <span class="tag">配置里只保存哈希，看不到也推不出明文</span></h3>
    <div class="body">
      <div class="row">
        <div class="col grow">
          <label for="pw-old">当前密码</label>
          <input type="password" id="pw-old" autocomplete="current-password"
                 spellcheck="false" placeholder="未设置密码时无需填写">
        </div>
      </div>
      <div class="row" style="margin-top:12px">
        <div class="col grow">
          <label for="pw-new">新密码<span class="hint">　至少 8 位，留空表示清除密码</span></label>
          <input type="password" id="pw-new" autocomplete="new-password" spellcheck="false">
        </div>
        <div class="col grow">
          <label for="pw-new2">确认新密码</label>
          <input type="password" id="pw-new2" autocomplete="new-password" spellcheck="false">
        </div>
      </div>
      <p class="hint" id="pw-note" style="margin:12px 0 0"></p>
      <div class="modal-acts">
        <button id="btn-pw-clear" class="danger" style="display:none">清除密码</button>
        <span class="grow"></span>
        <button id="btn-pw-cancel">取消</button>
        <button id="btn-pw" class="pri">设置密码</button>
      </div>
    </div>
  </div>
</div>
<script>
const F = __FIELDS__, RAW = __RAW__;
</script>
<script>
const $ = (s, r) => (r || document).querySelector(s);

/* ---------------- 订阅链接：下拉选择槽位 ---------------- */
const SLOTS = F.slots;                 // [{key,label,hint}]
const slotSel = $('#slot'), slotUrl = $('#slot-url');
let curSlot = SLOTS[0].key;

function renderSlotSelect() {
  slotSel.innerHTML = '';
  SLOTS.forEach(s => {
    const o = document.createElement('option');
    o.value = s.key;
    const has = (RAW[s.key] || '').trim();
    o.textContent = s.label + (has ? '　● 已配置' : '　○ 未配置');
    slotSel.appendChild(o);
  });
  slotSel.value = curSlot;
}

function showSlot(key) {
  curSlot = key;
  slotUrl.value = RAW[key] || '';
  const s = SLOTS.find(x => x.key === key);
  $('#slot-note').textContent = s ? s.hint : '';
  slotUrl.classList.remove('dirty');
}

slotSel.addEventListener('change', () => {
  // 把当前输入写回内存模型，再切到新槽位
  RAW[curSlot] = slotUrl.value;
  showSlot(slotSel.value);
});
slotUrl.addEventListener('input', () => slotUrl.classList.add('dirty'));
$('#btn-clear-url').addEventListener('click', () => {
  slotUrl.value = '';
  slotUrl.classList.add('dirty');
});

/* ---------------- 代理规则：结构化增删改 ---------------- */
// 常见规则类型（可自由输入其它类型）
const RULE_TYPES = [
  'DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'DOMAIN-REGEX', 'GEOSITE',
  'IP-CIDR', 'IP-CIDR6', 'SRC-IP-CIDR', 'GEOIP', 'IP-ASN', 'SRC-GEOIP',
  'DST-PORT', 'SRC-PORT', 'IN-PORT', 'RULE-SET', 'PROCESS-NAME',
  'PROCESS-PATH', 'PROCESS-NAME-REGEX', 'NETWORK', 'MATCH', 'FINAL',
];
// MATCH / FINAL 只有「类型,目标」两段
const NO_VALUE = new Set(['MATCH', 'FINAL']);
// 与后端 BUILTIN_TARGETS 一致：Clash 认可的策略名，任何时候都该出现在目标候选里
const BUILTIN_TARGETS_JS = ['DIRECT', 'REJECT', 'REJECT-DROP', 'PASS',
                            'COMPATIBLE', 'GLOBAL'];

let rules = [];          // [{type, value, target}]
let knownTargets = [];   // 本地代理组名 + 内置策略，用于目标下拉建议
let groups = [];         // 代理组条目（含 _extra/_nested/_order）
let proxies = [];        // 本地节点条目（同上）

function splitRule(line) {
  // 先剥掉 YAML 行尾注释（` # ...`）——注释只在 YAML 里合法，
  // 不能混进 target，否则界面显示和回写都会带上它。
  let s = String(line || '');
  const hash = s.indexOf(' #');
  if (hash >= 0) s = s.slice(0, hash);
  const parts = s.split(',').map(x => x.trim());
  if (!parts[0]) return {type: '', value: '', target: ''};
  const type = parts[0].toUpperCase();
  if (NO_VALUE.has(type)) {
    return {type: type, value: '', target: parts[1] || ''};
  }
  if (parts.length === 1) return {type: type, value: '', target: ''};
  if (parts.length === 2) return {type: type, value: parts[1], target: ''};
  // 第 3 段是目标，其后可能还有 no-resolve 等附加参数，原样保留
  const extra = parts.slice(3).filter(Boolean);
  return {type: type, value: parts[1],
          target: parts[2] + (extra.length ? ',' + extra.join(',') : '')};
}

function joinRule(r) {
  if (!r.type) return '';
  if (NO_VALUE.has(r.type)) return r.target ? r.type + ',' + r.target : r.type;
  const t = String(r.target || '').trim();
  if (!t) return r.type + ',' + String(r.value || '').trim();
  return r.type + ',' + String(r.value || '').trim() + ',' + t;
}

function parseRules(text) {
  return String(text || '').split('\\n')
    .map(l => l.trim())
    .filter(l => l && !l.startsWith('#'))
    // 去掉 YAML 列表标记：界面拿到的是 rules 段正文，条目形如 `- TYPE,value,target`
    .map(l => l.replace(/^-\\s+/, '').trim())
    .filter(Boolean)
    .map(splitRule);
}

/* ---------------- 列表段（proxies / proxy-groups）：结构化解析 ---------------- */
// 与后端 list_section_items 对应：把段原文拆成 [{字段...}]。
// 只提取界面要编辑的字段；**其余字段与注释一律塞进 _extra**，
// 保存时原样带回——节点协议字段动辄十几个，界面不全列出来，
// 但一个都不能丢，所以「不认识的行」必须整行保留。
function parseListSection(text, fields) {
  let lines = String(text || '').split('\\n');
  // 去掉 `key:` 首行
  if (lines.length && /^[A-Za-z0-9_-]+:\\s*$/.test(lines[0])) lines = lines.slice(1);
  else if (lines.length && /^[A-Za-z0-9_-]+:\\s*\\[\\s*\\]\\s*$/.test(lines[0])) return [];
  else if (lines.length) lines = lines.slice(1);

  const items = [];
  let cur = null, baseIndent = null;
  lines.forEach(line => {
    const m = line.match(/^(\\s*)-\\s/);
    if (m && (baseIndent === null || m[1].length <= baseIndent)) {
      if (baseIndent === null) baseIndent = m[1].length;
      if (cur) items.push(cur);
      cur = [line];
    } else if (cur) {
      cur.push(line);
    }
  });
  if (cur) items.push(cur);

  return items.map(itemLines => {
    const it = { _extra: [], _nested: [], _order: [] };
    let i = 0;
    while (i < itemLines.length) {
      let raw = itemLines[i];
      let mm = raw.match(/^(\\s*)-\\s+(.*)$/);
      if (mm) raw = mm[2];      // 首行剥掉 `- `
      const km = raw.match(/^(\\s*)([A-Za-z0-9_.-]+):(.*)$/);
      if (!km) { it._extra.push(itemLines[i]); i++; continue; }
      const key = km[2], indent = km[1].length;
      const val = stripComment(km[3]);
      if (val) {
        it[key] = unquote(val);
        it._order.push(key);
        // 记下原文有没有加引号：没加引号 = 可以让 YAML 按原生类型读
        // （`udp: true` 是布尔、`port: 8388` 是数字）。写回时据此决定要不要加引号，
        // 否则 `udp: true` 会被写成 `udp: "true"` —— 字符串，语义就变了。
        const q = val.length >= 2 && (val[0] === '"' || val[0] === "'") && val[val.length - 1] === val[0];
        (it._quoted = it._quoted || {})[key] = q;
        const cmt = commentOf(km[3]);
        if (cmt) (it._comments = it._comments || {})[key] = cmt;
        i++;
        continue;
      }
      // `key:` 无值 → 看后面的更深缩进行是否为嵌套列表
      const sub = [];
      let j = i + 1;
      while (j < itemLines.length) {
        const nxt = itemLines[j];
        if (!nxt.trim()) break;
        const lead = nxt.length - nxt.replace(/^\\s+/, '').length;
        if (lead <= indent) break;
        sub.push(nxt); j++;
      }
      const entries = sub.filter(x => /^\\s*-\\s/.test(x));
      if (entries.length) {
        it._nested.push(key);
        it[key] = entries.map(x => unquote(x.replace(/^\\s*-\\s+/, '').trim()));
        it._order.push(key);
        i = j;
        continue;
      }
      it._extra.push(itemLines[i]);
      i++;
    }
    return it;
  });
}

function stripComment(s) {
  const i = s.indexOf(' #');
  return (i < 0 ? s : s.slice(0, i)).trim();
}
function commentOf(s) {
  const i = s.indexOf(' #');
  return i < 0 ? '' : s.slice(i);
}
function unquote(s) {
  s = String(s == null ? '' : s).trim();
  if (s.length >= 2 && (s[0] === '"' || s[0] === "'") && s[s.length - 1] === s[0]) {
    const inner = s.slice(1, -1);
    return s[0] === '"' ? inner.replace(/\\\\"/g, '"').replace(/\\\\\\\\/g, '\\\\') : inner;
  }
  return s;
}

// YAML 标量渲染：**只在必要时**加双引号（与后端 _yaml_scalar 同一套规则）。
// 粗暴地「含特殊字符就加引号」会让 URL/规则串满屏引号，用户明确反馈过不想要；
// 实测只有这几类必须加：空值、首尾空白、值里出现 ` #`、`: `、制表符/换行、
// 以 `#`/`*`/`&` 开头、整串像 bool/null。
// **数字不加引号**：裸写会被 YAML 读回 int，正是想要的（port 不能变成字符串）。
const YAML_QUOTE_RE = /^\\s|\\s$|\\s#|:\\s|[\\t\\n\\r]|^[#*&]/;
const YAML_AMBIG_RE = /^(?:true|false|yes|no|on|off|null|none|~|)$/i;
// forceStr=false 表示「该值在原文里是裸写的」：直接裸写输出，让 YAML 按原生类型读回
// （`udp: true` 保持布尔、`port: 8388` 保持数字）。少了这个参数，
// 界面上「没动过的字段」会被当字符串重新加引号 —— `udp: true` 就变成 `udp: "true"`，
// 布尔变字符串，配置语义被静默改掉。
function yamlScalar(v, forceStr) {
  if (v === null || v === undefined) return '';
  const s = String(v);
  if (forceStr === false) {
    return /[\\t\\n\\r]/.test(s) ? quoteScalar(s) : s;
  }
  if (!YAML_QUOTE_RE.test(s) && !YAML_AMBIG_RE.test(s)) return s;
  return quoteScalar(s);
}
function quoteScalar(s) {
  return '"' + s.replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"')
                 .replace(/\\n/g, '\\\\n').replace(/\\r/g, '\\\\r').replace(/\\t/g, '\\\\t') + '"';
}

// 把条目数组渲染回整段文本（含 `key:` 首行），字段顺序：表格字段优先，其余按原顺序
function renderListSection(key, fields, items) {
  if (!items.length) return key + ': []\\n';
  const out = [key + ':\\n'];
  items.forEach(it => {
    const order = (it._order || []).filter(k => !k.startsWith('_') && it[k] !== undefined);
    const keys = fields.filter(f => order.includes(f) && String(it[f] || '').trim())
      .concat(order.filter(k => !fields.includes(k) && String(it[k] || '').trim()));
    let first = true;
    keys.forEach(f => {
      const head = first ? '  - ' : '    ';
      const cmt = (it._comments && it._comments[f]) ? '  ' + it._comments[f] : '';
      if (it._nested && it._nested.includes(f) && Array.isArray(it[f])) {
        out.push(head + f + ':' + cmt + '\\n');
        it[f].forEach(sub => {
          if (String(sub).trim()) out.push('      - ' + yamlScalar(sub) + '\\n');
        });
      } else {
        // 原文没加引号的字段：裸写，交给 YAML 按原生类型读（保住 udp: true 这类布尔）。
        // 原文加了引号、或界面新填的值，才当字符串转义。
        const fq = !(it._quoted && it._quoted[f] === false);
        out.push(head + f + ': ' + yamlScalar(it[f], fq) + cmt + '\\n');
      }
      first = false;
    });
    if (first) out.push('  - name: ""\\n');
    (it._extra || []).forEach(l => out.push(l.replace(/\\n?$/, '\\n')));
  });
  return out.join('');
}

// 目标候选项：本地代理组名 + 内置策略 + 规则里已出现过的目标（去重保序）。
// 代理组增删后调用 refreshTargets() 重建，否则下拉框看不到新组。
function targetOptions() {
  const list = knownTargets.slice();
  rules.forEach(r => {
    if (r.target && list.indexOf(r.target) < 0) list.push(r.target);
  });
  return list;
}

// 目标单元格：下拉选择为主的「选中即切换」，
// 需要写 no-resolve 这类附加参数或临时目标时切到手填输入框。
function makeTargetCell(r) {
  const wrap = document.createElement('div');
  wrap.style.display = 'flex';
  wrap.style.gap = '6px';

  const opts = targetOptions();
  const isCustom = !!r.target && opts.indexOf(r.target) < 0;
  // 已知候选走下拉；未知目标（含 DIRECT,no-resolve 这种带附加参数的）直接用手填框
  const useSelect = !isCustom;

  if (useSelect) {
    const sel = document.createElement('select');
    sel.style.flex = '1';
    const o0 = document.createElement('option');
    o0.value = ''; o0.textContent = '（未设置）';
    sel.appendChild(o0);
    opts.forEach(t => {
      const o = document.createElement('option');
      o.value = t; o.textContent = t;
      sel.appendChild(o);
    });
    // 当前值不在候选里时补一项，避免下拉框「显示为空但实际有值」
    if (r.target && opts.indexOf(r.target) < 0) {
      const o = document.createElement('option');
      o.value = r.target; o.textContent = r.target;
      sel.appendChild(o);
    }
    sel.value = r.target || '';
    sel.addEventListener('change', () => {
      r.target = sel.value;
      syncRawFromRules();
    });
    wrap.appendChild(sel);

    // 手填入口：值需要附加参数（如 ,no-resolve）时用
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '✎';
    btn.title = '手动输入（需要附加参数如 no-resolve 时用）';
    btn.addEventListener('click', () => {
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.value = r.target || '';
      inp.placeholder = '例：DIRECT,no-resolve';
      inp.addEventListener('input', () => { r.target = inp.value; syncRawFromRules(); });
      wrap.replaceChild(inp, sel);
      inp.focus();
    });
    wrap.appendChild(btn);
  } else {
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.value = r.target || '';
    inp.placeholder = '例：DIRECT,no-resolve';
    inp.addEventListener('input', () => { r.target = inp.value; syncRawFromRules(); });
    wrap.appendChild(inp);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '▾';
    btn.title = '从下拉列表选择代理组';
    btn.addEventListener('click', () => {
      if (targetOptions().indexOf(r.target) < 0) { r.target = ''; }
      renderRules();
      syncRawFromRules();
    });
    wrap.appendChild(btn);
  }
  return wrap;
}

function makeTypeCell(r) {
  const wrap = document.createElement('div');
  wrap.style.display = 'flex';
  wrap.style.gap = '6px';
  const sel = document.createElement('select');
  sel.style.flex = '1';
  const has = RULE_TYPES.includes(r.type);
  const opts = has ? RULE_TYPES : (r.type ? [r.type].concat(RULE_TYPES) : RULE_TYPES);
  opts.forEach(t => {
    const o = document.createElement('option');
    o.value = t; o.textContent = t;
    sel.appendChild(o);
  });
  sel.value = r.type || 'DOMAIN-SUFFIX';
  sel.addEventListener('change', () => {
    r.type = sel.value;
    refreshRow(row, r);
    syncRawFromRules();
  });
  wrap.appendChild(sel);
  return wrap;
}

let dragIdx = null;

function refreshRow(tr, r) {
  // 类型为 MATCH/FINAL 时隐藏「值」输入
  const valueWrap = tr.querySelector('.value-cell');
  const valueInp = tr.querySelector('.value-inp');
  const noVal = NO_VALUE.has(r.type);
  valueWrap.style.visibility = noVal ? 'hidden' : 'visible';
  valueInp.disabled = noVal;
  if (noVal) valueInp.value = '';
}

function renderRules() {
  const body = $('#rules-body');
  const filter = ($('#rule-filter').value || '').trim().toLowerCase();
  body.innerHTML = '';
  let shown = 0;

  rules.forEach((r, i) => {
    const line = joinRule(r);
    if (filter && line.toLowerCase().indexOf(filter) < 0) return;
    shown++;

    const tr = document.createElement('tr');
    tr.dataset.idx = i;
    // 注意：**不要把 draggable 设在整个 tr 上** —— 那样在行内的输入框里按住鼠标
    // 选文字、横向滑一下，浏览器会判定成拖拽整行，文字选不中、条目还会被拖走。
    // 只有手柄可拖，行本身只当放置目标（见下面 dragover/drop）。

    // 拖拽手柄
    const hd = document.createElement('td');
    hd.className = 'handle';
    hd.draggable = true;
    hd.textContent = '⠿';
    hd.title = '拖动排序';
    tr.appendChild(hd);

    // 序号
    const num = document.createElement('td');
    num.className = 'num';
    num.textContent = String(i + 1);
    tr.appendChild(num);

    // 类型
    const tdType = document.createElement('td');
    tdType.appendChild(makeTypeCell(r));
    tr.appendChild(tdType);

    // 值
    const tdVal = document.createElement('td');
    tdVal.className = 'value-cell';
    const valInp = document.createElement('input');
    valInp.type = 'text';
    valInp.className = 'value-inp';
    valInp.value = r.value || '';
    valInp.placeholder = r.type.indexOf('DOMAIN') === 0 ? 'example.com' : '参数值';
    valInp.addEventListener('input', () => { r.value = valInp.value; syncRawFromRules(); });
    tdVal.appendChild(valInp);
    tr.appendChild(tdVal);

    // 目标
    const tdTgt = document.createElement('td');
    tdTgt.appendChild(makeTargetCell(r));
    tr.appendChild(tdTgt);

    // 操作
    const acts = document.createElement('td');
    acts.className = 'acts';
    const up = document.createElement('button');
    up.textContent = '↑'; up.title = '上移';
    up.addEventListener('click', () => moveRule(i, -1));
    const down = document.createElement('button');
    down.textContent = '↓'; down.title = '下移';
    down.addEventListener('click', () => moveRule(i, 1));
    const dup = document.createElement('button');
    dup.textContent = '复制'; dup.title = '复制此规则';
    dup.addEventListener('click', () => {
      rules.splice(i + 1, 0, Object.assign({}, rules[i]));
      renderRules(); syncRawFromRules();
    });
    const del = document.createElement('button');
    del.textContent = '删除'; del.className = 'danger';
    del.addEventListener('click', () => {
      rules.splice(i, 1); renderRules(); syncRawFromRules();
    });
    acts.appendChild(up); acts.appendChild(down);
    acts.appendChild(dup); acts.appendChild(del);
    tr.appendChild(acts);

    refreshRow(tr, r);

    // 拖拽排序：dragstart 挂手柄（只有手柄 draggable），
    // dragover/drop 挂行（行是放置目标）。
    hd.addEventListener('dragstart', e => {
      dragIdx = i; tr.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', String(i)); } catch (_) {}
    });
    hd.addEventListener('dragend', () => {
      tr.classList.remove('dragging');
      body.querySelectorAll('tr').forEach(x =>
        x.classList.remove('drop-before', 'drop-after'));
      dragIdx = null;
    });
    tr.addEventListener('dragover', e => {
      e.preventDefault();
      const rect = tr.getBoundingClientRect();
      const after = (e.clientY - rect.top) > rect.height / 2;
      tr.classList.toggle('drop-after', after);
      tr.classList.toggle('drop-before', !after);
    });
    tr.addEventListener('dragleave', () => {
      tr.classList.remove('drop-before', 'drop-after');
    });
    tr.addEventListener('drop', e => {
      e.preventDefault();
      const rect = tr.getBoundingClientRect();
      const after = (e.clientY - rect.top) > rect.height / 2;
      let to = i + (after ? 1 : 0);
      if (dragIdx === null || dragIdx === i) return;
      if (dragIdx < to) to--;
      const [moved] = rules.splice(dragIdx, 1);
      rules.splice(to, 0, moved);
      renderRules(); syncRawFromRules();
    });

    body.appendChild(tr);
  });

  $('#rules-empty').style.display = rules.length ? 'none' : 'block';
  $('#rules-count').textContent = filter
    ? '显示 ' + shown + ' / 共 ' + rules.length + ' 条'
    : '共 ' + rules.length + ' 条';
}

function moveRule(i, delta) {
  const j = i + delta;
  if (j < 0 || j >= rules.length) return;
  const [m] = rules.splice(i, 1);
  rules.splice(j, 0, m);
  renderRules(); syncRawFromRules();
}

/* ---------------- 代理组 / 本地节点：表格渲染 ---------------- */
const GROUP_TYPES = ['select', 'url-test', 'fallback', 'load-balance', 'relay'];
const NODE_TYPES = ['ss', 'ssr', 'vmess', 'vless', 'trojan', 'hysteria',
                    'hysteria2', 'tuic', 'snell', 'http', 'socks5', 'wireguard'];

// 通用列表表格渲染：cells 定义每列怎么造控件，acts 定义右侧操作按钮
function renderListTable(opt) {
  const body = $(opt.bodySel);
  body.innerHTML = '';
  const rows = opt.items;
  rows.forEach((it, i) => {
    const tr = document.createElement('tr');
    tr.dataset.idx = i;
    // 同上：draggable 只给手柄。挂整行会导致「在输入框里按住鼠标选文字」
    // 被浏览器当成拖拽整行 —— 选不中文字，条目还会被拖走。

    const hd = document.createElement('td');
    hd.className = 'handle'; hd.textContent = '⠿'; hd.title = '拖动排序';
    hd.draggable = true;
    tr.appendChild(hd);

    const num = document.createElement('td');
    num.className = 'num'; num.textContent = String(i + 1);
    tr.appendChild(num);

    opt.cells.forEach(c => {
      const td = document.createElement('td');
      td.appendChild(c.make(it, i));
      tr.appendChild(td);
    });

    const acts = document.createElement('td');
    acts.className = 'acts';
    const up = document.createElement('button');
    up.textContent = '↑'; up.title = '上移';
    up.addEventListener('click', () => {
      if (i < 1) return;
      const [m] = rows.splice(i, 1); rows.splice(i - 1, 0, m);
      opt.render();
    });
    const down = document.createElement('button');
    down.textContent = '↓'; down.title = '下移';
    down.addEventListener('click', () => {
      if (i >= rows.length - 1) return;
      const [m] = rows.splice(i, 1); rows.splice(i + 1, 0, m);
      opt.render();
    });
    const del = document.createElement('button');
    del.textContent = '删除'; del.className = 'danger';
    del.addEventListener('click', () => { rows.splice(i, 1); opt.render(); });
    acts.appendChild(up); acts.appendChild(down); acts.appendChild(del);
    tr.appendChild(acts);

    hd.addEventListener('dragstart', e => {
      opt.dragIdx = i; tr.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', String(i)); } catch (_) {}
    });
    hd.addEventListener('dragend', () => {
      tr.classList.remove('dragging');
      body.querySelectorAll('tr').forEach(x =>
        x.classList.remove('drop-before', 'drop-after'));
      opt.dragIdx = null;
    });
    tr.addEventListener('dragover', e => {
      e.preventDefault();
      const rect = tr.getBoundingClientRect();
      const after = (e.clientY - rect.top) > rect.height / 2;
      tr.classList.toggle('drop-after', after);
      tr.classList.toggle('drop-before', !after);
    });
    tr.addEventListener('dragleave', () => {
      tr.classList.remove('drop-before', 'drop-after');
    });
    tr.addEventListener('drop', e => {
      e.preventDefault();
      const rect = tr.getBoundingClientRect();
      const after = (e.clientY - rect.top) > rect.height / 2;
      let to = i + (after ? 1 : 0);
      if (opt.dragIdx === null || opt.dragIdx === i) return;
      if (opt.dragIdx < to) to--;
      const [m] = rows.splice(opt.dragIdx, 1);
      rows.splice(to, 0, m);
      opt.render();
    });

    body.appendChild(tr);
  });
  $(opt.emptySel).style.display = rows.length ? 'none' : 'block';
}

// 文本输入单元格
function textCell(get, set, ph, width) {
  return {
    make(it) {
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.value = get(it);
      if (ph) inp.placeholder = ph;
      if (width) inp.style.width = width;
      inp.addEventListener('input', () => { set(it, inp.value); });
      return inp;
    }
  };
}

// 下拉 + 可手填：类型既可能在下拉里，也可能是自定义值
function typeCell(types, get, set) {
  return {
    make(it) {
      const wrap = document.createElement('div');
      wrap.style.display = 'flex';
      wrap.style.gap = '6px';
      const cur = get(it);
      const sel = document.createElement('select');
      sel.style.flex = '1';
      const opts = types.includes(cur) ? types
        : (cur ? [cur].concat(types) : types);
      opts.forEach(t => {
        const o = document.createElement('option');
        o.value = t; o.textContent = t;
        sel.appendChild(o);
      });
      sel.value = cur || types[0];
      sel.addEventListener('change', () => set(it, sel.value));
      wrap.appendChild(sel);
      return wrap;
    }
  };
}

// 多行文本（代理组成员：每行一个）
function linesCell(get, set, ph) {
  return {
    make(it) {
      const ta = document.createElement('textarea');
      ta.value = get(it);
      ta.spellcheck = false;
      if (ph) ta.placeholder = ph;
      ta.style.width = '100%';
      ta.style.resize = 'vertical';
      ta.style.fontFamily = 'ui-monospace,Consolas,monospace';
      ta.style.fontSize = '12.5px';
      // 首次渲染也要按内容行数撑开，不能固定 rows=2：
      // 代理组「成员」动辄十几行，只给 2 行高会被框裁掉、看着像内容丢了。
      const fitRows = () => {
        ta.rows = Math.max(2, Math.min(12, ta.value.split('\\n').length));
      };
      fitRows();
      // 输入时同步模型；行数多时自动长高一点，避免要滚动
      ta.addEventListener('input', () => { set(it, ta.value); fitRows(); });
      return ta;
    }
  };
}

/* ---------------- 其他配置：按段选择 + 原文编辑 ---------------- */
let exKey = null;

function renderExtraSelect() {
  const sel = $('#ex-key');
  sel.innerHTML = '';
  F.extra_keys.forEach(k => {
    const o = document.createElement('option');
    o.value = k.key;
    const has = (RAW[k.key] || '').trim();
    o.textContent = k.key + ' — ' + k.label + (has ? '　● 已配置' : '　○ 未配置');
    sel.appendChild(o);
  });
}

function showExtra(key) {
  exKey = key;
  $('#ex-raw').value = RAW[key] || '';
  const meta = F.extra_keys.find(k => k.key === key);
  const isLine = (RAW[key] || '').indexOf('\\n') < 0;
  $('#ex-note').textContent = meta
    ? (isLine
       ? '该项是单行值：直接改等号右边的内容即可（如 `' + key + ': 7890`）。'
       : '该项是多行段：整段替换，注意保持缩进（YAML 用空格，不要用 Tab）。')
    : '';
}

function syncRawFromRules() {
  $('#rules-raw').value = rules.map(joinRule).filter(Boolean).join('\\n');
  markDirty();
}

function applyRaw() {
  rules = parseRules($('#rules-raw').value);
  renderRules();
}

$('#btn-add-rule').addEventListener('click', () => {
  rules.push({type: 'DOMAIN-SUFFIX', value: '', target: ''});
  renderRules(); syncRawFromRules();
  const inputs = $('#rules-body').querySelectorAll('tr');
  if (inputs.length) inputs[inputs.length - 1].querySelector('.value-inp').focus();
});
$('#btn-add-match').addEventListener('click', () => {
  rules.push({type: 'MATCH', value: '', target: '🐟 漏网之鱼'});
  renderRules(); syncRawFromRules();
});
$('#btn-tidy').addEventListener('click', () => {
  const before = rules.length;
  rules = rules.filter(r => r.type && (NO_VALUE.has(r.type) ? r.target : r.value));
  renderRules(); syncRawFromRules();
  flash('已清除 ' + (before - rules.length) + ' 条无效行', true);
});
$('#btn-raw-apply').addEventListener('click', () => {
  applyRaw(); flash('已用文本覆盖表格', true);
});
$('#btn-raw-sync').addEventListener('click', () => {
  syncRawFromRules(); flash('已从表格同步到文本', true);
});
$('#rule-filter').addEventListener('input', renderRules);

/* ---------------- 统计 / 保存 ---------------- */
let dirty = false;
function markDirty() { dirty = true; }

function refreshStat() {
  const set = SLOTS.filter(s => (RAW[s.key] || '').trim()).length;
  $('#stat').innerHTML =
    '<span>接口路径 <b>' + F.api_path + '</b></span>' +
    '<span>已配订阅 <b>' + set + '</b> / ' + SLOTS.length + '</span>' +
    '<span>规则 <b>' + rules.length + '</b> 条</span>' +
    '<span>缓存节流 <b>' + F.cache_ttl + 's</b></span>';
}

function collect() {
  // 当前槽位的输入可能还没写回 RAW（未切换过、未触发 change）
  RAW[curSlot] = slotUrl.value;
  const blocks = {};

  // rules 是「多行段」，保存时要自带 `rules:` 首行（assemble_config 整体替换该段）。
  // 每行必须是 `- 条目`：YAML 列表项缺了 `- ` 会被解析成 dict 的键，写回去就是坏配置。
  // （表格里不显示 `- `，parseRules 会剥掉它，所以只有写回这一刻才补。）
  const body = rules.map(joinRule).filter(Boolean).map(r => '  - ' + r).join('\\n');
  blocks.rules = 'rules:\\n' + body + (body ? '\\n' : '');

  // 代理组 / 本地节点：由表格模型渲染回整段文本（未编辑字段原样带回）
  blocks['proxy-groups'] = renderListSection('proxy-groups',
    ['name', 'type', 'proxies'], groups);
  blocks.proxies = renderListSection('proxies',
    ['name', 'type', 'server', 'port'], proxies);

  // 订阅槽位
  SLOTS.forEach(s => { blocks[s.key] = (RAW[s.key] || '').trim(); });

  // 其他配置：当前选中的段取文本区内容，其余段回传原文（保持原样写回）
  F.extra_keys.forEach(k => {
    if (k.key === exKey) {
      RAW[k.key] = $('#ex-raw').value;
      blocks[k.key] = RAW[k.key];
      return;
    }
    const v = RAW[k.key];
    if (v === undefined) return;
    blocks[k.key] = v;
  });
  return blocks;
}

function flash(text, ok) {
  const m = $('#msg');
  m.textContent = text;
  m.className = ok ? 'ok' : 'err';
  m.style.display = 'inline-block';
  if (ok) setTimeout(() => { m.style.display = 'none'; }, 3500);
}

/* ---------------- 访问密码：在界面里改（后端只落哈希） ---------------- */
// 密码**不走「保存并生效」那条路**：那里会把配置原文逐块带回服务端，
// 明文口令混进去就可能被写进 config.yaml / .bak / 日志。改密码有独立接口，
// 服务端收到明文后只导出 PBKDF2 摘要再写盘。
const PW_MIN = F.password_min || 8;
let pwSet = !!F.password_set;

function refreshPw() {
  $('#pw-old').disabled = !pwSet;
  $('#btn-pw').textContent = pwSet ? '修改密码' : '设置密码';
  $('#btn-pw-clear').style.display = pwSet ? '' : 'none';
  $('#nopw').style.display = pwSet ? 'none' : 'flex';
  // 右上角入口：没密码时标成橙色提醒，按钮文案也跟着变
  const open = $('#btn-pw-open');
  open.textContent = pwSet ? '访问密码' : '设置密码';
  open.classList.toggle('attn', !pwSet);
  $('#pw-note').textContent = pwSet
    ? '改完立即生效：旧密码当场作废，之前登录的会话（本页除外）需要重新登录，'
      + '订阅接口同步改用新密码。'
    : '还没有密码：任何人都能打开本页修改配置，订阅接口则一律拒绝。'
      + '建议现在设一个（至少 ' + PW_MIN + ' 位）。';
}

function openPw() {
  $('#pw-old').value = $('#pw-new').value = $('#pw-new2').value = '';
  refreshPw();
  $('#pw-mask').style.display = 'flex';
  // 弹窗刚显示时元素还不可聚焦，下一帧再 focus
  setTimeout(() => (pwSet ? $('#pw-old') : $('#pw-new')).focus(), 30);
}

function closePw() { $('#pw-mask').style.display = 'none'; }

// clear=true 表示「清除密码」：新密码按空串提交
async function changePassword(clear) {
  const old = $('#pw-old').value;
  const n1 = clear ? '' : $('#pw-new').value;
  const n2 = clear ? '' : $('#pw-new2').value;
  if (!clear && n1 !== n2) { flash('两次输入的新密码不一致', false); return; }
  if (n1 && n1.length < PW_MIN) { flash('密码至少 ' + PW_MIN + ' 位', false); return; }
  if (!n1 && pwSet && !confirm('清除密码后任何人都能打开本界面并修改配置，确定继续？')) return;
  if (!n1 && !pwSet) { flash('当前没有密码，无需清除', false); return; }
  if (pwSet && !old) { flash('请先填写当前密码', false); return; }
  try {
    const r = await fetch('/ui/password', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_password: old, new_password: n1 })
    });
    const j = await r.json();
    if (!j.ok) { flash(j.error || '修改失败', false); return; }
    pwSet = !!n1;
    closePw();
    refreshPw();
    flash(n1 ? '密码已更新：旧会话需重新登录' : '已清除密码（本界面不再校验登录）', true);
  } catch (e) { flash('修改失败: ' + e, false); }
}

$('#btn-pw-open').addEventListener('click', openPw);
$('#nopw-set').addEventListener('click', openPw);
$('#btn-pw-cancel').addEventListener('click', closePw);
$('#btn-pw').addEventListener('click', () => changePassword(false));
$('#btn-pw-clear').addEventListener('click', () => changePassword(true));
// 点遮罩空白处关闭；弹窗内部点击不冒泡出去
$('#pw-mask').addEventListener('click', e => { if (e.target === $('#pw-mask')) closePw(); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('#pw-mask').style.display === 'flex') closePw();
});

$('#btn-save').addEventListener('click', async () => {
  const before = curSlot;
  try {
    const r = await fetch('/ui/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({blocks: collect()})
    });
    const j = await r.json();
    if (!j.ok) { flash(j.error || '保存失败', false); return; }
    // 保存成功后刷新内存模型（顺便让下拉里的「已配置」标记变准）
    SLOTS.forEach(s => { RAW[s.key] = (RAW[s.key] || '').trim(); });
    renderSlotSelect();
    showSlot(before);
    refreshStat();
    flash('已保存并热重载生效' + (j.restart_hint ? '（接口路径需重启进程）' : ''), true);
  } catch (e) { flash('保存失败: ' + e, false); }
});

$('#btn-reload').addEventListener('click', () => {
  if (!dirty || confirm('有未保存的修改，确定放弃并重新载入？')) location.reload();
});

/* ---------------- 代理组 / 本地节点：渲染与事件 ---------------- */
function renderGroups() {
  renderListTable({
    bodySel: '#pg-body', emptySel: '#pg-empty', items: groups,
    dragIdx: null, render: renderGroups,
    cells: [
      textCell(g => g.name || '', (g, v) => { g.name = v; refreshGroupNames(); },
               '组名（规则里引用它）'),
      typeCell(GROUP_TYPES, g => g.type || '', (g, v) => { g.type = v; }),
      linesCell(g => Array.isArray(g.proxies) ? g.proxies.join('\\n') : (g.proxies || ''),
                (g, v) => {
                  g.proxies = v.split('\\n').map(s => s.trim()).filter(Boolean);
                  g._nested = g._nested || [];
                  if (g._nested.indexOf('proxies') < 0) g._nested.push('proxies');
                  if (g._order.indexOf('proxies') < 0) g._order.push('proxies');
                },
                '每行一个：组名 / 节点名 / DIRECT / "*"'),
    ]
  });
  // 表格里的组名可能已改，同步名字候选给规则的目标下拉
  refreshGroupNames();
  $('#pg-count').textContent = '共 ' + groups.length + ' 个组';
  $('#pg-raw').value = renderListSection('proxy-groups',
    ['name', 'type', 'proxies'], groups);
}

function renderProxies() {
  renderListTable({
    bodySel: '#px-body', emptySel: '#px-empty', items: proxies,
    dragIdx: null, render: renderProxies,
    cells: [
      textCell(p => p.name || '', (p, v) => { p.name = v; refreshGroupNames(); },
               '节点名'),
      typeCell(NODE_TYPES, p => p.type || '', (p, v) => { p.type = v; }),
      textCell(p => p.server || '', (p, v) => { p.server = v; }, '域名或 IP'),
      textCell(p => (p.port == null ? '' : String(p.port)),
               (p, v) => { p.port = v; }, '443', '90px'),
    ]
  });
  $('#px-count').textContent = '共 ' + proxies.length + ' 个节点';
  $('#px-raw').value = renderListSection('proxies',
    ['name', 'type', 'server', 'port'], proxies);
}

// 反向同步：把「高级原文」的文本解析回表格模型。
// 保存时是以表格模型为准渲染 proxy-groups / proxies 段的，不走 RAW，
// 所以用户在原文里改的字段（如节点级 udp、url-test 的 interval）必须先
// 覆盖回模型，否则保存时被静默丢弃 —— 用户反馈过「改 udp: false 没效果」。
function applyPgRaw() {
  groups = parseListSection($('#pg-raw').value,
    ['name', 'type', 'proxies']);
  renderGroups();
}
function applyPxRaw() {
  proxies = parseListSection($('#px-raw').value,
    ['name', 'type', 'server', 'port']);
  renderProxies();
}

// 代理组名 + 本地节点名都可能是规则的目标，改了要让下拉跟上
function refreshGroupNames() {
  const names = [];
  groups.forEach(g => { if (g.name) names.push(g.name); });
  proxies.forEach(p => { if (p.name) names.push(p.name); });
  knownTargets = names.concat(BUILTIN_TARGETS_JS);
  renderRules();
}

// 代理组增删后重建候选并重绘表格（否则已有的目标下拉看不到新组）
function refreshTargets() { refreshGroupNames(); }

$('#btn-add-group').addEventListener('click', () => {
  groups.push({name: '新代理组', type: 'select', proxies: [],
               _order: ['name', 'type', 'proxies'], _nested: ['proxies'],
               _extra: []});
  renderGroups();
  const rows = $('#pg-body').querySelectorAll('tr');
  if (rows.length) rows[rows.length - 1].querySelector('input').focus();
});
$('#btn-add-proxy').addEventListener('click', () => {
  proxies.push({name: '新节点', type: 'ss', server: '', port: '',
                _order: ['name', 'type', 'server', 'port'],
                _nested: [], _extra: []});
  renderProxies();
  const rows = $('#px-body').querySelectorAll('tr');
  if (rows.length) rows[rows.length - 1].querySelector('input').focus();
});
$('#btn-pg-apply').addEventListener('click', () => {
  applyPgRaw(); refreshGroupNames(); markDirty();
  flash('已用文本覆盖代理组表格', true);
});
$('#btn-pg-sync').addEventListener('click', () => {
  renderGroups(); flash('已从表格同步到文本', true);
});
$('#btn-px-apply').addEventListener('click', () => {
  applyPxRaw(); refreshGroupNames(); markDirty();
  flash('已用文本覆盖节点表格', true);
});
$('#btn-px-sync').addEventListener('click', () => {
  renderProxies(); flash('已从表格同步到文本', true);
});
$('#ex-key').addEventListener('change', () => {
  // 切段前先把当前段的内容存回模型，否则切走就丢了
  if (exKey) RAW[exKey] = $('#ex-raw').value;
  showExtra($('#ex-key').value);
});
$('#ex-raw').addEventListener('input', () => {
  if (exKey) { RAW[exKey] = $('#ex-raw').value; markDirty(); }
});

/* ---------------- 初始化 ---------------- */
knownTargets = (F.targets || []).slice();
groups = parseListSection(RAW['proxy-groups'], ['name', 'type', 'proxies']);
proxies = parseListSection(RAW.proxies, ['name', 'type', 'server', 'port']);

rules = parseRules(RAW.rules);
renderSlotSelect();
showSlot(SLOTS[0].key);
renderGroups();
renderProxies();
renderRules();
renderExtraSelect();
showExtra(F.extra_keys.length ? F.extra_keys[0].key : null);
$('#rules-raw').value = rules.map(joinRule).filter(Boolean).join('\\n');
refreshStat();
refreshPw();
</script>
</body>
</html>
"""

# ===================== 网页配置界面：字段与读写 =====================
# 按 config.yaml 的**原始文本块**编辑（而非解析成 YAML 再回写），
# 这样注释、空行、键顺序都能原样保留，界面保存不会「整理」用户的配置。
#
# 界面只暴露两组配置：
#   - 订阅链接：sub_url ~ sub_url5（UI_SLOTS）
#   - 代理规则：rules
# 其余配置段仍可手工编辑 config.yaml —— 界面保存逐块替换，不会碰它们。
UI_SLOTS = [
    {'key': 'sub_url', 'label': '默认订阅（不传 sub_url 时使用）',
     'hint': '客户端不带 ?sub_url=N 参数时使用这个地址。'},
    {'key': 'sub_url1', 'label': '订阅 1（?sub_url=1）',
     'hint': '客户端请求 ?sub_url=1 时使用；留空则回退到默认订阅。'},
    {'key': 'sub_url2', 'label': '订阅 2（?sub_url=2）',
     'hint': '客户端请求 ?sub_url=2 时使用；留空则回退到默认订阅。'},
    {'key': 'sub_url3', 'label': '订阅 3（?sub_url=3）',
     'hint': '客户端请求 ?sub_url=3 时使用；留空则回退到默认订阅。'},
    {'key': 'sub_url4', 'label': '订阅 4（?sub_url=4）',
     'hint': '客户端请求 ?sub_url=4 时使用；留空则回退到默认订阅。'},
    {'key': 'sub_url5', 'label': '订阅 5（?sub_url=5）',
     'hint': '客户端请求 ?sub_url=5 时使用；留空则回退到默认订阅。'},
]

# 界面可写的「单行」段（保存时只换该行、保留行尾注释）
_LINE_KEYS = {s['key'] for s in UI_SLOTS}
# 界面「其他配置」可写的段：整段原文替换（键名 → 界面上的说明）
_EXTRA_KEYS = [
    ('port', 'HTTP 代理端口'),
    ('socks-port', 'SOCKS5 代理端口'),
    ('mixed-port', '混合端口（HTTP + SOCKS 同端口）'),
    ('allow-lan', '允许局域网访问'),
    ('bind-address', '监听地址（* = 所有网卡）'),
    ('mode', '运行模式：rule / global / direct'),
    ('log-level', '日志级别：silent / error / warning / info / debug'),
    ('ipv6', '是否解析并允许 AAAA 记录'),
    ('udp', '允许 UDP 转发（QUIC / 游戏 / TUN 需要）'),
    ('unified-delay', '统一延迟口径'),
    ('tcp-concurrent', 'TCP 并发连接'),
    ('keep-alive-interval', 'TCP keep-alive 间隔（秒）'),
    ('global-client-fingerprint', '全局 TLS 指纹'),
    ('external-controller', '控制端口（dashboard 用）'),
    ('dns', 'DNS 配置'),
    ('hosts', '强制指定解析结果'),
    ('experimental', '实验性功能'),
    ('tun', 'TUN 模式'),
    ('profile', '订阅信息与自动更新'),
]
# 界面可写的「多行」段（整体替换）
_TEXT_KEYS = {'rules', 'proxies', 'proxy-groups'} | {k for k, _ in _EXTRA_KEYS}

import re as _re

# 顶层 key 的行首匹配（0 缩进 + key:）
_TOP_KEY_RE = _re.compile(r'^([A-Za-z0-9_-]+):')

# 目标候选项：内置策略（Clash 认可的策略名）
BUILTIN_TARGETS = ['DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'COMPATIBLE', 'GLOBAL']


def split_config_blocks(text):
    """把 config.yaml 原文切成 {顶层 key: 原始文本块}，文件头归入 '__head__'。

    以「0 缩进的 key: 开头」为界切分，注释、空行、缩进子项都随所属 key 原样保留，
    保存时逐块替换回去即可，不会丢注释或重排顺序。
    """
    blocks = {}
    head = []
    cur_key = None
    cur = []

    def flush():
        if cur_key is not None:
            blocks[cur_key] = ''.join(cur)

    for line in (text or '').splitlines(keepends=True):
        m = _TOP_KEY_RE.match(line)
        if m:
            flush()
            cur_key = m.group(1)
            cur = [line]
        elif cur_key is None:
            head.append(line)
        else:
            cur.append(line)
    flush()
    blocks['__head__'] = ''.join(head)
    return blocks


def _line_value(block):
    """从 `key: value  # 注释` 取出 value（去注释、去引号、去首尾空白）。

    引号必须剥掉：`sub_url: "https://a.c/x"` 在界面上应该显示成
    `https://a.c/x` 而不是带引号的样子——用户反馈过「输入框里显示双引号」。
    写回时由 `_quote_if_needed` 按需重新加引号，语义不变。
    """
    if not block:
        return ''
    line = block.splitlines()[0]
    val = line.split(':', 1)[1] if ':' in line else ''
    idx = val.find(' #')
    if idx >= 0:
        val = val[:idx]
    val = val.strip()
    # 空值的几种等价写法（""、''、空）在界面上统一显示为空
    if val in ('""', "''"):
        return ''
    return _unquote_scalar(val)


def _quote_if_needed(val):
    """单行段回写时按需加引号。

    空值写成 `""`（否则 `key: ` 后面空空如也，看着像被截断了）；
    含 YAML 特殊字符（`:`、`#`、首尾空白等）也加引号，避免被误解析
    ——比如订阅地址带 `?token=x#y` 时 `#` 会被当成注释起点。
    与 `_yaml_scalar` 同款转义，只是额外兜底空值。
    """
    if val is None or str(val).strip() == '':
        return '""'
    return _yaml_scalar(val)


def assemble_config(text, updates):
    """把 updates（顶层 key -> 新内容）写回原文，其余内容逐字保留。

    - 单行段（原文该段只有 `key: value` 一行）：只替换值，保留原行尾注释；
    - 多行段：整体替换（新内容需自带 `key:` 行）；
    - 原文中不存在的新段：追加到文件末尾。

    单行/多行的判定**看原文形态**，而不是只看界面声明过的 `_LINE_KEYS`——
    这样 password、cache_ttl 之类非界面字段被写入时也不会丢掉 `key:` 前缀。
    """
    pat = _TOP_KEY_RE
    lines = (text or '').splitlines(keepends=True)
    out = []
    seen = set()

    for line in lines:
        m = pat.match(line)
        if not m:
            # 文件头注释（首个顶层 key 之前）原样保留；其余为上一段的续行，
            # 若上一段被替换则已在替换体里带上，这里跳过。
            if not seen:
                out.append(line)
            continue
        key = m.group(1)
        if key in seen:
            continue                     # 异常配置里的重复顶层 key：只保留第一处
        seen.add(key)
        if key not in updates:
            out.append(line)             # 原样保留该段的起始行
            out.append(_segment_body(text, key))   # 及其续行
            continue
        new = updates[key]
        if new is None:
            continue                     # None = 删除该段（含其续行）
        # 替换体若自带**本段自己**的 `key:` 首行，就是「整段替换」，不再套单行格式，
        # 否则会写出 `rules: rules:` 这种双重 header。
        # 必须比对 key 名：单行值里的 `https://...` 也长得像 `key:`，不能误判。
        m_new = _TOP_KEY_RE.match(new.lstrip('\n'))
        has_header = bool(m_new) and m_new.group(1) == key
        if not has_header and (key in _LINE_KEYS or _is_line_block(line)
                               or not _is_block_text(new)):
            # 单行段：只换值，保留行尾注释
            old_line = line.rstrip('\n').rstrip('\r')
            comment = ''
            idx = old_line.find(' #')
            if idx >= 0:
                comment = old_line[idx:]
            out.append('%s: %s%s\n' % (key, _quote_if_needed(new), comment))
        else:
            body = new if new.endswith('\n') else new + '\n'
            out.append(body)

    for key, new in updates.items():
        if key not in seen and new is not None:
            # 原文没有这个段：单行段按 `key: value` 追加，其余按整段写入。
            if key in _LINE_KEYS or (_TOP_KEY_RE.match(new.lstrip('\n')) is None
                                     and not new.strip().startswith('-')):
                out.append('%s: %s\n' % (key, _quote_if_needed(new)))
            else:
                body = new if new.endswith('\n') else new + '\n'
                out.append(body)
    return ''.join(out)


def _is_block_text(new):
    """新内容是「整段文本」还是「一个单行标量」。

    多行段（自带 `key:` 首行或列表项）必须整段替换；单行标量则要按 `key: value`
    写回。这里的判定**只看新内容自身**：原文里 `password_hash:` 这种「冒号后写空」
    的行（YAML 里等于 None，很常见）会让 `_is_line_block` 判成 False，此时若新
    内容只是个单行值（哈希串），就只能按单行写——否则会把 `key:` 整个丢掉，
    写出 `pbkdf2_sha256$...` 这种没有键名的坏配置。
    """
    s = str(new or '').lstrip('\n')
    return '\n' in s.rstrip('\n') or s.startswith('-') or s.lstrip().startswith('-')


def _is_line_block(line):
    """判断顶层段的起始行是否为「单行 `key: value`」形态。

    只看起始行冒号后是否**有非注释内容**——段后面跟的注释行、空行不算数，
    否则 `cache_ttl: 60` 后面那些说明性注释会把它误判成多行段。
    """
    stripped = line.rstrip('\n').rstrip('\r')
    m = _TOP_KEY_RE.match(stripped)
    if not m:
        return False
    val = stripped[m.end():]
    idx = val.find(' #')
    if idx >= 0:
        val = val[:idx]
    return bool(val.strip())


def _segment_body(text, key):
    """取某个顶层段在起始行之后的全部续行。"""
    lines = (text or '').splitlines(keepends=True)
    body = []
    started = False
    for line in lines:
        m = _TOP_KEY_RE.match(line)
        if m:
            if started:
                break
            started = (m.group(1) == key)
            continue
        if started:
            body.append(line)
    return ''.join(body)


def ui_blocks_payload(text):
    """构造网页界面需要的原始文本块。

    前端读取方式：
    - 订阅槽位：`RAW['sub_url3']`       → 该行当前值（不含注释）
    - 代理规则：`RAW['rules']`          → rules 段正文（不含 `rules:` 首行）
    - 代理组/节点：`RAW['proxy-groups']` / `RAW['proxies']` → 整段原文（含 `key:` 首行），
      前端再用 `parseListSection` 拆成结构化条目
    - 其他配置：同 key 的整段原文
    """
    src = split_config_blocks(text)
    payload = {}
    for key in _LINE_KEYS:
        payload[key] = _line_value(src.get(key, ''))
    # rules 取正文（表格按条目编辑）；其余多行段取整段原文
    payload['rules'] = _rules_body(src.get('rules', ''))
    for key in _TEXT_KEYS:
        if key in payload:
            continue
        payload[key] = (src.get(key, '') or '').rstrip('\n')
    return payload


def ui_list_section_payload(text):
    """代理组 / 本地节点的结构化数据（前端直接渲染成表格）。

    只给界面要编辑的字段（名称 / 类型 / 成员 / 服务器 / 端口）；
    其余字段留在 `RAW` 原文里，保存时由服务端或前端原样带回，不会被抹掉。
    """
    src = split_config_blocks(text)
    out = {}
    for key in _LIST_SECTIONS:
        fields = _LIST_TABLE_FIELDS.get(key, ('name',))
        rows = []
        for it in list_section_items(src.get(key, '')):
            row = {}
            for f in fields:
                v = it.get(f)
                if isinstance(v, list):
                    v = '\n'.join(str(x) for x in v)
                row[f] = '' if v is None else str(v)
            rows.append(row)
        out[key] = rows
    return out


def _rules_body(raw):
    """从 rules 段原文里去掉 `rules:` 首行，只留规则条目，便于文本区编辑。"""
    if not raw:
        return ''
    lines = raw.splitlines()
    return '\n'.join(lines[1:]).rstrip('\n')


# ---------- 列表段（proxies / proxy-groups）：结构化编辑 ----------
# 界面把「节点列表」「代理组列表」拆成扁平行编辑（每行几个关键字段），
# 但节点类型极多（ss/vmess/trojan/hysteria2/... 各有专属字段），
# 所以**只把关键字段提出来做表格，其余字段原样保留在每个条目的 extra 里**，
# 保存时再拼回去——这样界面不用为每种协议写一个表单，也不会吃掉高级字段。
#
# 每条目的文本形态：
#   - name: A
#     type: ss
#     server: 1.2.3.4
#     port: 443
#     cipher: aes-128-gcm            ← 非关键字段，收进 extra 原样带回
_LIST_SECTIONS = ('proxies', 'proxy-groups')

# 各段的关键字段（表格里可编辑）；其余字段走 extra
_SECTION_FIELDS = {
    'proxies': ('name', 'type', 'server', 'port'),
    'proxy-groups': ('name', 'type', 'proxies'),
}

# YAML 标量的安全转义：只在**必要**时加引号。
#
# 注意这里不能照抄「含特殊字符就加引号」的粗暴规则：Clash 的值大多是 URL、
# 用户名、规则串，带 `:` `?` `&` `,` `'` 的值裸写完全合法，一律加引号只会让
# 生成的配置满屏引号（用户明确反馈过不想要）。实测只有这几类必须加引号：
#   1. 空值——`key:` 后面空着会被读成 None；
#   2. 首尾空白——会被 YAML 吃掉；
#   3. 值里出现 ` #`——会被当成注释起点；
#   4. 整串看起来像 bool / null 的（true/yes/on/off/null/~/空）——裸写会被
#      YAML 读成布尔或 None，语义直接变了；
#   5. 值里出现 `: `——裸写会直接解析失败（不是歧义，是语法错误）；
#   6. 含制表符或换行——裸写同样解析失败；
#   7. 以 `#` 开头（整行会被当注释）、以 `*`/`&` 开头（会被当别名/锚点）。
# 其余（URL、域名、带撇号或逗号的串）一律裸写。
#
# **数字不在此列**：`8388` 这种裸写会被 YAML 读回 int，正是想要的结果
# （曾被误加引号，把 `port: 8388` 写成 `port: "8388"`，Clash 拿到字符串）。
_NEEDS_QUOTE_RE = _re.compile(r'^\s|\s$|\s#|:\s|[\t\n\r]|^[#*&]')
_AMBIGUOUS_RE = _re.compile(r'^(?:true|false|yes|no|on|off|null|none|~|)$',
                            _re.IGNORECASE)


def _yaml_scalar(v, force_str=True):
    """把一个值渲染成 YAML 标量。必要时才加双引号，其余裸写。

    `force_str=False` 表示「这个值在原文里是裸写的」，那就直接裸写、让 YAML
    按原生类型读回（`udp: true` → 布尔，`port: 8388` → 数字）。否则把值当
    字符串：`v` 本身就是 int/bool 时裸写才是正确语义（不能被引成 `"8388"`），
    是 str 时则要防「看起来像 bool/null」被误读。
    """
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if not isinstance(v, str):
        return str(v)
    s = v
    if not force_str:
        # 原文裸写：原样输出即可（除非含制表符/换行这类写出来就坏的内容）
        return s if not _re.search(r'[\t\n\r]', s) else _quote_scalar(s)
    if _NEEDS_QUOTE_RE.search(s) or _AMBIGUOUS_RE.match(s):
        return _quote_scalar(s)
    return s


def _quote_scalar(s):
    """按 YAML 双引号标量规则转义（反斜杠/引号/制表/换行都要转）。"""
    body = (s.replace('\\', '\\\\').replace('"', '\\"')
             .replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t'))
    return '"%s"' % body


def _unquote_scalar(s):
    """去掉 YAML 标量外层引号（界面显示不想要引号）。"""
    s = (s or '').strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        inner = s[1:-1]
        if s[0] == '"':
            inner = inner.replace('\\"', '"').replace('\\\\', '\\')
        return inner
    return s


def _is_placeholder(v):
    """占位表达式（`*`、`"*"` 等）：merge 时会被展开成节点列表。"""
    return isinstance(v, str) and v.strip() in ('*',)


def _split_comment(s):
    """拆出 `值  # 注释` 里的值与行尾注释（值里的 URL 不受影响，只看 ` #`）。"""
    idx = s.find(' #')
    if idx < 0:
        return s.strip(), ''
    return s[:idx].strip(), s[idx:]


def _parse_list_item(lines):
    """把「一条列表项」的多行文本解析成 (item, extra_lines)。

    item 里放：
    - 普通标量字段（`key: value`）→ 值已去引号、去行尾注释；
    - 嵌套块字段（`key:` 后跟更深的 `- ` 行，如 proxy-groups 的 `proxies`）
      → 值是一个**字符串列表**（每一项已去引号）。
    `_order` 记录字段出现顺序，`_nested` 记录嵌套字段名，`_extra` 记录无法
    归类的原始行（注释、空行）。三者合起来保证：界面上没动过的字段写回时
    一个都不会丢——这是最要紧的，静默丢字段等于改坏用户的配置。
    """
    item = {'_order': []}
    nested = []
    extra = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        m = _re.match(r'^(\s*)([A-Za-z0-9_.-]+):(.*)$', raw)
        if not m:
            extra.append(raw)
            i += 1
            continue
        key, rest, indent = m.group(2), m.group(3), len(m.group(1))
        val, cmt = _split_comment(rest)
        if val:
            item[key] = _unquote_scalar(val)
            item['_order'].append(key)
            # 记下原文有没有加引号：没加引号说明用户可以接受 YAML 按原生类型读
            # （`udp: true` 是布尔、`port: 8388` 是数字）。写回时照着还原，
            # 否则 `udp: true` 会被写成 `udp: "true"`——字符串，语义就变了。
            item.setdefault('_quoted', {})[key] = (
                len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"))
            if cmt:
                # 行尾注释记下来，重渲染时贴着该字段带回，不然改一次值注释就没了
                item.setdefault('_comments', {})[key] = cmt
            i += 1
            continue
        # `key:` 后面没有值：收集缩进更深的续行，看看是不是嵌套列表
        sub = []
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip():
                break
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break
            sub.append(nxt)
            j += 1
        entries = [x for x in sub if _re.match(r'^\s*-\s', x)]
        if entries:
            nested.append(key)
            item[key] = [_unquote_scalar(_strip_dash(x)) for x in entries]
            item['_order'].append(key)
            i = j
            continue
        extra.append(raw)
        i += 1
    item['_nested'] = nested
    return item, extra


def split_list_section(body):
    """把列表段正文切成 [条目行列表, ...]。

    `body` 是去掉 `key:` 首行后的原文（形如 `  - name: A\\n    type: ss\\n`）。

    只按**首次出现的那个缩进层**切分——`proxy-groups` 的条目里有嵌套列表
    （`proxies:` 下的 `- "Home"`，缩进更深），那些不能被当成新条目，
    否则一个组会被切成好几条。以第一个 `- ` 的缩进为基准，更深的一律算续行。
    """
    items = []
    cur = None
    base_indent = None
    for line in (body or '').splitlines(keepends=True):
        m = _re.match(r'^(\s*)-\s', line)
        if m and (base_indent is None or len(m.group(1)) <= base_indent):
            if base_indent is None:
                base_indent = len(m.group(1))
            if cur is not None:
                items.append(cur)
            cur = [line]
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        items.append(cur)
    return items


def _strip_dash(line):
    """去掉列表标记 `- `，返回标记后的内容（条目首个字段）。"""
    return _re.sub(r'^\s*-\s+', '', line.rstrip('\n').rstrip('\r'), count=1)


def list_section_items(raw):
    """解析 proxies / proxy-groups 段，返回 [{fields..., '_extra': [...], '_raw': [...]}]。

    `_raw` 保留条目原始行（含注释、空行），用于「未编辑的条目原样写回」——
    界面上只改名字之类的轻改动时，不该把用户的注释和缩进去掉。
    """
    if not raw:
        return []
    body = '\n'.join(raw.splitlines()[1:])
    out = []
    for lines in split_list_section(body):
        norm = [(_strip_dash(lines[0]) + '\n') if i == 0 else line
                for i, line in enumerate(lines)]
        item, extra = _parse_list_item(norm)
        item['_extra'] = extra
        item['_raw'] = lines
        out.append(item)
    return out


# 界面表格里可编辑的字段（顺序即表格列顺序）
_LIST_TABLE_FIELDS = {
    'proxies': ('name', 'type', 'server', 'port'),
    'proxy-groups': ('name', 'type', 'proxies'),
}


def _render_list_item(fields, item, indent='  '):
    """把一条列表项渲染成 YAML 文本行。

    渲染顺序：**表格字段**（`fields`）在前，未在表格里出现的字段按原顺序
    接在后面——后者是必须的：节点/代理组有大量界面不暴露的字段
    （cipher / password / url / interval / tolerance ...），漏掉它们等于
    静默改坏配置。最后再接 `_extra`（注释、空行）。

    缩进规则（与手写配置一致）：
      `  - name: X`      首个字段带列表标记
      `    type: ss`     后续字段与 name 的值对齐（缩进 + 2）
      `    proxies:`     嵌套列表字段
      `      - Y`       子项再缩进 2
    """
    nested = item.get('_nested') or []
    order = [k for k in (item.get('_order') or []) if not k.startswith('_')]
    # 表格字段优先（固定列顺序、便于对照），其余字段保持原文顺序
    keys = [f for f in fields if f in order and item.get(f) not in (None, '')]
    keys += [k for k in order if k not in keys and item.get(k) not in (None, '')]

    lines = []
    first = True
    comments = item.get('_comments') or {}
    quoted = item.get('_quoted') or {}
    for f in keys:
        v = item.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        head = (indent + '- ') if first else (indent + '  ')
        cmt = ('  ' + comments[f]) if comments.get(f) else ''
        # 原文没加引号的字段：值裸写，交给 YAML 按原生类型读（`udp: true` 保持布尔、
        # `port: 8388` 保持数字）。原文加了引号、或界面新填的值，才当字符串转义。
        force_str = quoted.get(f, True)
        if f in nested and isinstance(v, list):
            lines.append(head + '%s:%s\n' % (f, cmt))
            for sub in v:
                if str(sub).strip():
                    lines.append(indent + '    - ' + _yaml_scalar(sub) + '\n')
        elif isinstance(v, list):
            # 非嵌套列表却存成了 list（异常输入）：按单值处理，别写出坏结构
            lines.append(head + '%s: %s%s\n'
                         % (f, _yaml_scalar(','.join(map(str, v)), force_str), cmt))
        else:
            lines.append(head + '%s: %s%s\n'
                         % (f, _yaml_scalar(v, force_str), cmt))
        first = False
    # 没有可渲染字段时兜底，避免写出空条目
    if first:
        lines.append(indent + '- name: ""\n')
    for raw in item.get('_extra') or []:
        lines.append(raw if raw.endswith('\n') else raw + '\n')
    return ''.join(lines)


def render_list_section(key, items, comment=''):
    """把条目列表渲染成完整的「列表段」文本（含 `key:` 首行）。

    空列表时输出 `key: []`——比留一个空行更好，YAML 下语义明确。
    """
    if not items:
        return '%s: []\n' % key
    fields = _LIST_TABLE_FIELDS.get(key, ('name',))
    body = ''.join(_render_list_item(fields, it) for it in items)
    header = '%s:\n' % key
    if comment:
        header += '  # ' + comment.replace('\n', '\n  # ') + '\n'
    return header + body


def _normalize_rules(value):
    """把 `rules:` 段解析出的各种形态统一成规则字符串列表。

    YAML 下同一段可能解析成多种类型，界面保存时都要能接受：
    - `None`        → `[]`（段是空的，等于没有规则）
    - `list`        → 原样返回
    - `str`         → 单条规则（用户把规则写在了 `rules:` 同一行，如 `rules: MATCH,DIRECT`）
    其它类型（dict / int 等）判为写错，抛错让调用方给出提示。
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value] if value.strip() else []
    raise ValueError('rules 段只能是规则列表（每行一条，如 DOMAIN-SUFFIX,example.com,GITHUB）')


def _ui_targets(template):
    """目标候选项：模板里出现过的代理组名 + 内置策略，去重后保序。

    `proxy-groups` 属于 Clash 模板（不在 CONTROL_KEYS 里），要从 template 取。
    """
    targets = []
    seen = set()

    def add(name):
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            targets.append(name)

    try:
        for g in (template or {}).get('proxy-groups') or []:
            if isinstance(g, dict):
                add(g.get('name'))
    except Exception:
        pass
    for name in BUILTIN_TARGETS:
        add(name)
    return targets


def _json_for_script(obj):
    """把一个对象序列化成**可安全内联进 <script>** 的 JSON。

    `json.dumps` 只保证 JSON 合法，**不保证 HTML 安全**：它不会转义 `<`。
    而配置内容（规则串、节点名、订阅 URL…）是用户可控的，只要里面出现
    `</script>`，浏览器就会提前闭合当前的 <script> 标签，后面的内容被当成
    HTML 解析 —— 于是 `</script><script>...</script>` 就是**可执行的存储型 XSS**。
    （实测：headless Edge 打开 /ui，注入的 `window.__PWNED = 1` 真的跑起来了。）

    修法：把 `<` `>` `&` 以及行分隔符 U+2028/U+2029 转成 `\\uXXXX` 转义。
    这些都是 **JSON 标准转义**，`JSON.parse` 出来的字符串与原文完全一致
    （`\\u003c` 解析回来就是 `<`），所以前端逻辑零改动，只是 HTML 解析器
    再也看不到 `</script>` 了。

    **只替换这几个字符本身，绝对不能动反斜杠**：JSON 文本里的 `\\n` 已经是
    合法转义，若再整体转义一遍反斜杠，`\\n` 会变成 `\\\\n` —— 换行符变成
    字面量「反斜杠+n」两个字符，配置内容就被改坏了。

    注意 U+2028/U+2029 也要转：它们在 JS 里是**行终止符**，直接出现在字符串
    字面量里会导致语法错误（JSON 允许，JS 不允许）——会让整页白屏。
    """
    s = json.dumps(obj, ensure_ascii=False)
    for ch, esc in (('<', '\\u003c'), ('>', '\\u003e'), ('&', '\\u0026'),
                    ('\u2028', '\\u2028'), ('\u2029', '\\u2029')):
        s = s.replace(ch, esc)
    return s


@app.route('/ui')
def ui():
    need = ui_login_required()
    if need is not None:
        return need
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            text = f.read()
    except Exception as e:
        logging.error("网页界面读取配置失败: %s", e)
        return Response('读取 config.yaml 失败: %s' % e, status=500, mimetype='text/plain')

    control, template = load_local_config()
    meta = {
        'slots': UI_SLOTS,
        'api_path': API_PATH,
        'cache_ttl': control_int(control, 'cache_ttl', DEFAULT_CACHE_TTL),
        'sub_url_count': sum(1 for k in SUB_URL_KEYS if control.get(k)),
        'targets': _ui_targets(template),
        # 是否已设置密码（决定界面上的表单形态与顶部提示），
        # 只给「有没有」，**绝不回传哈希本身**
        'password_set': bool(stored_password(control)),
        'password_min': PW_MIN_LEN,
        # 「其他配置」下拉：只列出**配置里确实存在**的段，避免界面推荐一堆空段
        'extra_keys': [{'key': k, 'label': lbl} for k, lbl in _EXTRA_KEYS
                       if k in split_config_blocks(text)],
        'mtime': time.strftime('%Y-%m-%d %H:%M:%S',
                               time.localtime(os.path.getmtime(CONFIG_FILE))),
    }
    html = UI_HEAD.replace('__FIELDS__', _json_for_script(meta))
    html = html.replace('__RAW__', _json_for_script(ui_blocks_payload(text)))
    return Response(html, mimetype='text/html')


@app.route('/ui/save', methods=['POST'])
def ui_save():
    need = ui_login_required()
    if need is not None:
        return need
    data = request.get_json(silent=True) or {}
    blocks = data.get('blocks') or {}
    if not isinstance(blocks, dict):
        return jsonify({'ok': False, 'error': '参数格式错误'})

    updates = {k: v for k, v in blocks.items() if k in _LINE_KEYS or k in _TEXT_KEYS}
    if not updates:
        return jsonify({'ok': False, 'error': '没有需要保存的内容'})
    if any(not isinstance(v, str) for v in updates.values()):
        return jsonify({'ok': False, 'error': '内容必须是文本'})

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            text = f.read()
    except Exception as e:
        return jsonify({'ok': False, 'error': '读取配置失败：%s' % e})

    new_text = assemble_config(text, updates)

    # 校验：写入前必须是合法 YAML，且关键段结构正确，否则拒绝（避免写出坏配置）
    try:
        parsed = yaml.safe_load(new_text)
        if not isinstance(parsed, dict):
            raise ValueError('解析结果不是 YAML 字典')
        if 'rules' in parsed:
            # `rules:` 段空着解析出来是 None（合法：没有规则）；
            # 若用户把规则直接写在 `rules:` 同一行（如 `rules: MATCH,DIRECT`），
            # YAML 会解析成字符串——这是单条规则的写法，容错接受。
            rules = _normalize_rules(parsed['rules'])
            for r in rules:
                if not isinstance(r, str) or not r.strip():
                    raise ValueError('rules 中每一项都必须是非空文本（如 MATCH,DIRECT）')
        for key in ('proxies', 'proxy-groups'):
            if key not in parsed:
                continue
            val = parsed[key]
            # 空段（None / []）合法：等于该段没有内容
            if val is None:
                continue
            if not isinstance(val, list):
                raise ValueError('%s 必须是列表（每项一条，如 `- name: xxx`）' % key)
            for it in val:
                if not isinstance(it, dict):
                    raise ValueError('%s 中每一项都应是 `- name: xxx` 形式的映射' % key)
                if not it.get('name'):
                    raise ValueError('%s 中每一项都需要 name' % key)
        if 'proxies' in parsed:
            for p in (parsed['proxies'] or []):
                # "*" 占位表达式只该写在代理组的 proxies 里
                if not isinstance(p, dict) and not merge.is_placeholder(p):
                    raise ValueError('proxies 中每一项都应是节点定义'
                                     '（"*" 表达式只写在代理组里）')
    except Exception as e:
        logging.error("网页界面保存被拒绝，新内容未通过校验: %s", e)
        return jsonify({'ok': False, 'error': '配置校验失败：%s' % e})

    old_api_path = API_PATH
    try:
        write_config_file(CONFIG_FILE, new_text)
    except Exception as e:
        logging.error("网页界面保存失败: %s", e)
        return jsonify({'ok': False, 'error': '写入失败：%s' % e})

    invalidate_config()                  # 热重载：下次读取即生效，无需重启
    control, _ = load_local_config()
    new_api = '/' + str(control.get('api_path', 'api')).lstrip('/')
    logging.info("网页界面已保存配置并热重载: %s", sorted(updates))
    return jsonify({'ok': True, 'restart_hint': new_api != old_api_path})


@app.route('/ui/password', methods=['POST'])
def ui_change_password():
    """修改访问密码：`{old_password, new_password}` → 只把哈希写进 config.yaml。

    - 已经设了密码就必须先验旧密码（防他人路过改口令把自己顶出去）；
    - 新密码**只在内存里过一手**：导出 PBKDF2 摘要后立刻丢弃明文，
      配置文件、备份、日志里都不会出现它；
    - 写回前照样用 `password: None` 把旧的明文 `password:` 行删干净；
    - 改完必须给当前会话补一枚新 cookie：cookie 签名里带着密码指纹，
      旧的那枚当场失效（别人的会话也随之失效）。
    """
    need = ui_login_required()
    if need is not None:
        return need

    data = request.get_json(silent=True) or {}
    old_pw = str(data.get('old_password') or '')
    new_pw = str(data.get('new_password') or '')

    control, _ = load_local_config()
    stored = stored_password(control)
    if stored and not verify_password(old_pw, stored):
        logging.warning("修改密码失败：原密码不正确（来源 %s）", request.remote_addr)
        return jsonify({'ok': False, 'error': '原密码不正确'}), 403

    if new_pw:
        if len(new_pw) < PW_MIN_LEN:
            return jsonify({'ok': False,
                            'error': '密码至少 %d 位' % PW_MIN_LEN})
        if len(new_pw) > PW_MAX_LEN:
            return jsonify({'ok': False,
                            'error': '密码最长 %d 位' % PW_MAX_LEN})
        hashed = hash_password(new_pw)   # 明文的唯一用途：导出摘要
    else:
        if not stored:
            return jsonify({'ok': False, 'error': '当前没有密码，无需清空'})
        hashed = ''                      # 清空密码（回到默认：界面免登录）

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            text = f.read()
    except Exception as e:
        return jsonify({'ok': False, 'error': '读取配置失败：%s' % e})

    new_text = assemble_config(text, {_HASH_KEY: hashed, _LEGACY_KEY: None})
    try:
        parsed = yaml.safe_load(new_text)
        if not isinstance(parsed, dict):
            raise ValueError('解析结果不是 YAML 字典')
        # 自检：写盘前先确认解析器读回的就是这枚新哈希。
        # 单行/整段的判定一旦走错，`password_hash:` 会被写成没有键名的裸串
        # （YAML 仍然「能解析」成别的形状，但语义完全错了），这里把它挡住。
        if str(parsed.get(_HASH_KEY) or '') != hashed:
            raise ValueError('password_hash 未正确写回（读到 %r）'
                             % parsed.get(_HASH_KEY))
    except Exception as e:
        logging.error("修改密码被拒绝，新配置未通过校验: %s", e)
        return jsonify({'ok': False, 'error': '配置校验失败：%s' % e})

    try:
        write_config_file(CONFIG_FILE, new_text)
    except Exception as e:
        logging.error("修改密码写入失败: %s", e)
        return jsonify({'ok': False, 'error': '写入失败：%s' % e})

    invalidate_config()
    logging.info("访问密码已%s（来源 %s）",
                 '更新' if hashed else '清空', request.remote_addr)
    resp = jsonify({'ok': True, 'cleared': not hashed})
    # 签名里含密码指纹：旧 cookie 已失效，给当前这枚续上
    resp.set_cookie(UI_SESSION_COOKIE, _ui_make_token(), max_age=UI_SESSION_TTL,
                    httponly=True, samesite='Lax')
    return resp


if __name__ == '__main__':
    from gevent.pywsgi import WSGIServer

    http_server = WSGIServer(('0.0.0.0', 5000), app)
    http_server.serve_forever()
