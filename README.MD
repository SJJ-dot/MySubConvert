### 订阅转换服务（subconverter）

拉取机场 Clash 订阅，以**本地配置为模板**、**节点与代理组订阅优先**的原则合并，返回最终 Clash 配置。

## 核心特性

- `config.yaml` 既是服务配置文件，也是 Clash 配置模板。
- 与 Clash 配置无关的控制项（密码、订阅链接、排除组等）在合并前自动剥离，不会泄漏到输出。
- `sub_url` 可为空：为空时仅返回本地模板，机场订阅拉取失败且无缓存时同样退化为本地模板，保证服务可用。
- 机场配置带内存缓存（可配置 `cache_ttl`），减少重复请求。
- Home 节点 IP/端口支持通过 `server_url` 动态刷新（持久化到 `home_cache.yaml`）。

## 合并规则（本地配置作为模板，节点与代理组以订阅优先）

| 内容 | 策略 |
| --- | --- |
| `proxies` | 按 `name` 合并去重，**订阅（机场）同名项优先**，本地仅补充机场没有的新项 |
| `proxy-groups` | 按 `name` 合并去重，**订阅同名项优先**（机场完整组覆盖本地 `[DIRECT]` 占位组），本地仅补充自定义组 |
| 其他顶层 key（`port` / `mode` / `dns` / `tun` / `external-controller` …） | 本地已有则用本地；本地没有且未在 `remove_keys` 中则用订阅的 |
| `rules` | 本地规则置于最前（Clash 自上而下匹配，本地自定义规则优先级更高），按字符串去重 |

> `remove_keys` 从最终配置中移除指定顶层 key（对本地与订阅均生效）。
> 无订阅（`sub_url` 为空或拉取失败且无缓存）时，直接返回本地模板（含占位标准组兜底）。

## 接口说明

### 1. 获取订阅

- 请求方式：`GET`
- 路径：`/api`（与 `config.yaml` 的 `api_path` 一致，修改需重启）
- 参数：
  - `password`：与 `config.yaml` 的 `password` 一致（必填，错误返回 `Hello World!`）
  - `sub_url`：可选，覆盖配置中的 `sub_url`

成功返回 `text/plain` 的 Clash 配置；若机场返回 `subscription-userinfo`，会原样透传该响应头。

### 2. 健康检查

- `GET /health` → `ok`

## 配置项（config.yaml）

控制项（不会出现在输出中）：

| 配置项 | 说明 |
| --- | --- |
| `api_path` | 订阅接口路径，默认 `/api` |
| `password` | 接口访问密码 |
| `sub_url` | 机场订阅链接，可为空 |
| `basic_auth` | 动态刷新 Home IP 的基础认证 `username:password`（可选） |
| `server_url` | 获取 Home 节点最新 IP/端口 的服务地址（可选，为空则不刷新） |
| `exclude_groups` | 合并时排除的代理组 / 规则目标组名 |
| `remove_keys` | 从最终配置中移除的顶层 key（如 `dns` / `tun`，对本地与订阅均生效） |
| `cache_ttl` | 机场配置缓存时长（秒），`0` 表示不过期 |

其下为 Clash 配置模板本体（顶层 key、`proxies`、`proxy-groups`、`rules`）。

## 运行

### Docker

```bash
docker build -t mysubconvert .
docker run -d -p 5000:5000 mysubconvert
```

### 本地

```bash
pip install -r requirements.txt
python main.py   # 监听 0.0.0.0:5000
```

## 测试

```bash
pip install pytest
pytest tests/test_convert.py -q
```
