### 订阅转换服务（subconverter）

拉取机场 Clash 订阅，以**本地配置为模板**、**节点与代理组订阅优先**的原则合并，返回最终 Clash 配置。

## 请求处理流程

每个请求按固定的四步执行，日志中会分别打印 `第 1 步` ~ `第 4 步`，可直接据此判断走了哪条分支：

| 步骤 | 条件 | 行为 |
| --- | --- | --- |
| ① 拉取 | 有 `sub_url` | 每次请求都**实时拉取**机场订阅（网络优先） |
| ② 回退缓存 | ① 失败，且存在 `cache_ttl` 有效期内的缓存 | 使用内存缓存里的节点配置 |
| ③ 生成最小配置 | ① 失败，且缓存未命中（超期或从未拉取过）；或 `sub_url` 为空 | 根据本地配置生成最小配置文件（仅本地节点 + 本地代理组 / 规则） |
| ④ 转换 | 总是执行 | 按本地配置转换订阅文件：补充本地节点、修正代理规则与代理组 |

> ①②③ 的产物都是第 ④ 步的输入，第 ④ 步始终执行，保证任一分支下输出都是完整可用的 Clash 配置。

## 核心特性

- `config.yaml` 既是服务配置文件，也是 Clash 配置模板。
- 与 Clash 配置无关的控制项（密码、订阅链接、排除组等）在合并前自动剥离，不会泄漏到输出。
- 机场「节点配置」**每次请求都实时拉取**（网络优先）；拉取失败才回退内存缓存（有效期由 `cache_ttl` 控制，超期视同未命中）。流量用量（`subscription-userinfo`）来自当次拉取，因此始终最新。
- Home 节点 IP/端口支持通过 `server_url` 动态刷新（持久化到 `home_cache.yaml`），每次请求都刷新，不参与上述缓存。

## 合并规则（本地配置作为模板，节点与代理组以订阅优先）

| 内容 | 策略 |
| --- | --- |
| `proxies` | 按 `name` 合并去重，**订阅（机场）同名项优先**，本地仅补充机场没有的新项 |
| `proxy-groups` | 按 `name` 合并去重，**订阅同名项优先**（机场完整组覆盖本地 `[DIRECT]` 占位组），本地仅补充自定义组 |
| 其他顶层 key（`port` / `mode` / `dns` / `tun` / `external-controller` …） | 本地已有则用本地；本地没有且未在 `remove_keys` 中则用订阅的 |
| `rules` | 本地规则置于最前（Clash 自上而下匹配，本地自定义规则优先级更高），按字符串去重 |
| 指定组合并（`merge_groups`） | 把 sources 组列出的**节点**去重并入 target 组；默认删除源组，并将指向源组的规则自动改指 target |
| 顶层 key 顺序 | 订阅加载成功时按**订阅配置的 key 顺序**输出（本地同名覆盖值，本地独有 key 追加末尾）；无订阅时按本地模板顺序 |

> `remove_keys` 从最终配置中移除指定顶层 key（对本地与订阅均生效）。
> 无订阅（`sub_url` 为空，或拉取失败且缓存未命中）时，第 ③ 步生成的最小配置文件就是本地模板本身（含占位标准组兜底），再经第 ④ 步合并输出。

### merge_groups：把指定代理组合并到目标组

将订阅源中若干个代理组（如「国外媒体」「电报」）列出的**节点**去重并入目标组（如「🚀 节点选择」），适合把多组重复节点收敛到一个组、精简订阅。

配置格式（位于 `config.yaml` 控制项区，可配置多条）：

```yaml
merge_groups:                  # 支持多条合并规则
  - target: 🚀 节点选择        # 合并目标组（订阅或本地中存在即可）
    sources:                  # 源组列表：这些组列出的节点会并入 target
      - 国外媒体
      - 电报
    remove_sources: true      # 合并后删除源组（默认 true）
    redirect_rules: true      # 规则中指向源组的自动改指 target（默认 true，防规则失效）
  # - target: 其他目标组        # 第二条合并规则示例
  #   sources:
  #     - 组A
  #     - 组B
```

合并行为说明：

- 只并入**真正的节点**（出现在 `proxies` 列表中的名字）；`DIRECT` / `REJECT` 等内置策略、代理组名、目标组自身均不会并入，避免产生循环引用；
- 并入节点自动去重（目标组已有的成员不会重复添加）；
- `remove_sources: true`（默认）：合并后从输出中删除源组，并把规则中指向源组的条目自动改指 `target`，防止规则指向不存在的组；
- `remove_sources: false`：保留源组，规则不做改写；
- 订阅中不存在的源组或目标组会被跳过，不影响其余配置。

示例效果（合并前 → 合并后）：

| 代理组 | 合并前 | 合并后 |
| --- | --- | --- |
| `国外媒体`（url-test） | HK, JP, US | （已删除） |
| `电报`（select） | JP, US, DIRECT | （已删除） |
| `🚀 节点选择` | HK, DIRECT | HK, DIRECT, JP, US |

同时规则改写：`DOMAIN-SUFFIX,netflix.com,国外媒体` → `DOMAIN-SUFFIX,netflix.com,🚀 节点选择`，`DOMAIN-SUFFIX,t.me,电报` → `DOMAIN-SUFFIX,t.me,🚀 节点选择`。

## 接口说明

### 1. 获取订阅

- 请求方式：`GET`
- 路径：`/api`（与 `config.yaml` 的 `api_path` 一致，修改需重启）
- 参数：
  - `password`：与 `config.yaml` 的 `password` 一致（必填，错误返回 `Hello World!`）
  - `sub_url`：可选，选择用哪个订阅地址。规则见下方「多订阅地址」

#### 多订阅地址

`config.yaml` 中可配置 `sub_url`（默认）与 `sub_url1` ~ `sub_url5`（备用），通过请求参数选择：

| 请求 | 使用的地址 |
| --- | --- |
| 不传 `sub_url` | `sub_url` |
| `?sub_url=1` | `sub_url1` |
| `?sub_url=2` … `?sub_url=5` | `sub_url2` … `sub_url5` |
| `?sub_url=https://...` | 直接使用该地址（兼容旧用法） |
| `?sub_url=9`（超出 1~5） | 忽略，使用 `sub_url` |
| `?sub_url=N` 但 `sub_urlN` 为空 | 回退 `sub_url` |

各个地址**独立缓存**（缓存的 key 是最终解析出的完整地址），因此可以在客户端建多个订阅配置分别指向不同机场，互不影响。选择与回退过程会打印 `按请求选择订阅地址` / `sub_urlN 未配置，回退默认 sub_url` 等日志。

成功返回 `text/plain` 的 Clash 配置；若机场返回 `subscription-userinfo`，会原样透传该响应头。

> 该响应头来自当次实时拉取，因此总是最新的。仅当拉取失败并回退缓存时，它才是缓存时的旧值；完全没有可用数据时不带该响应头，客户端会保留上次的显示值。

### 2. 健康检查

- `GET /health` → `ok`

## 配置项（config.yaml）

控制项（不会出现在输出中）：

| 配置项 | 说明 |
| --- | --- |
| `api_path` | 订阅接口路径，默认 `/api` |
| `password` | 接口访问密码 |
| `sub_url` | 默认机场订阅链接，可为空 |
| `sub_url1` ~ `sub_url5` | 备用订阅链接，客户端请求 `?sub_url=N` 时使用；为空则回退 `sub_url`（详见「多订阅地址」） |
| `basic_auth` | 动态刷新 Home IP 的基础认证 `username:password`（可选） |
| `server_url` | 获取 Home 节点最新 IP/端口 的服务地址（可选，为空则不刷新） |
| `exclude_groups` | 合并时排除的代理组 / 规则目标组名 |
| `remove_keys` | 从最终配置中移除的顶层 key（如 `dns` / `tun`，对本地与订阅均生效） |
| `merge_groups` | 指定代理组合并：把 sources 组列出的节点并入 target 组（默认删除源组、规则改指 target，详见「合并规则」） |
| `cache_ttl` | 拉取失败时用于回退的缓存有效期（秒），`0` 表示永不过期。机场配置每次请求都会实时拉取，缓存仅作失败回退 |

其下为 Clash 配置模板本体（顶层 key、`proxies`、`proxy-groups`、`rules`）。

## 配置文件示例（config.yaml）

> ⚠️ 以下示例中的 IP、密码、域名均为**占位符**，请替换为你的真实值。
> `config.yaml` 包含真实订阅地址与密码等敏感信息，**切勿提交到公开仓库**。

```yaml
# ===================== 服务控制配置 =====================
api_path: /api            # 订阅接口路径（修改需重启进程）
password: change-me       # 接口访问密码，务必改成强密码
sub_url: ""               # 默认机场订阅链接，可为空；为空时根据下方本地配置生成最小配置文件
sub_url1: ""              # 备用订阅 1：客户端请求 ?sub_url=1 时使用（为空则回退 sub_url）
sub_url2: ""              # 备用订阅 2：?sub_url=2
sub_url3: ""              # 备用订阅 3：?sub_url=3
sub_url4: ""              # 备用订阅 4：?sub_url=4
sub_url5: ""              # 备用订阅 5：?sub_url=5
basic_auth: ""            # 动态刷新 Home 节点 IP/端口 的基础认证 username:password（可选）
server_url: ""            # 获取 Home 节点最新 IP/端口 的服务地址（可选，为空则不刷新）
exclude_groups:           # 合并时排除的「代理组名 / 规则目标组名」
  - 🛑 全球拦截
remove_keys:              # 从最终配置中移除的顶层 key（取消注释即生效）
  # - dns
  # - tun
cache_ttl: 0           # 拉取失败时回退缓存的有效期（秒），0 表示永不过期

# ===================== Clash 配置模板 =====================
dns:
  enable: true
  enhanced-mode: redir-host        # 建议 redir-host；用 fake-ip(TUN) 也可
  nameserver:
    - 119.29.29.29
    - 223.5.5.5
  nameserver-policy:
    "nas.example.com": "192.168.80.1"   # 改为你路由器的局域网 IP

port: 7890              # HTTP 代理端口
socks-port: 7891        # SOCKS5 代理端口
allow-lan: true         # 是否允许局域网访问
mode: Rule              # 运行模式：Rule(规则) / Global(全局) / Direct(直连)
log-level: info
external-controller: 127.0.0.1:9090

# 代理节点配置
proxies:
  - name: Home
    type: ss
    server: 11.11.11.11    # 改为你的 Home 节点地址
    port: 11
    cipher: aes-256-gcm
    password: change-me
    udp: true

# 代理组配置
proxy-groups:
  - name: 🏠 Nas           # 自定义组：回家走 Home 节点
    type: select
    proxies:
      - Home
      - DIRECT
  - name: 🚀 节点选择       # 标准组（占位）：有订阅时被机场完整组覆盖，无订阅时兜底
    type: select
    proxies:
      - DIRECT

# 规则配置（本地规则在前，优先级更高）
rules:
  - IP-CIDR,192.168.80.0/24,🏠 Nas   # 改为你的局域网网段
  - DOMAIN-SUFFIX,nas.example.com,🏠 Nas
  - MATCH,🐟 漏网之鱼
```

## 运行

### Docker（推荐：外部挂载 config.yaml）

构建镜像：

```bash
docker build -t mysubconvert .
```

运行（将宿主机上的 `config.yaml` 挂载进容器，改配置无需重新构建镜像）：

```bash
docker run -d --name mysubconvert \
  -p 5000:5000 \
  -v /path/to/config.yaml:/app/config.yaml \
  mysubconvert
```

或使用 docker-compose（`docker-compose.yml`）：

```yaml
services:
  mysubconvert:
    image: ghcr.io/sjj-dot/mysubconvert:latest   # 或本地构建的 mysubconvert
    container_name: mysubconvert
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./config.yaml:/app/config.yaml          # 外部配置，改配置无需重建镜像
```

> 宿主机修改 `config.yaml` 后，执行 `docker restart mysubconvert` 生效（`api_path` 修改需重启进程）。

### 本地

```bash
pip install -r requirements.txt
python main.py   # 监听 0.0.0.0:5000
```

## 发布（打 tag 自动构建镜像）

push 代码后打版本 tag，触发 GitHub Actions 自动构建并推送 `latest` 与版本号两个镜像标签：

```bash
git add . && git commit -m "v1.0.0"
git push
git tag v1.0.0 && git push origin v1.0.0
```

服务器更新：

```bash
docker pull ghcr.io/sjj-dot/mysubconvert:latest
docker compose up -d --force-recreate   # 或：docker rm -f mysubconvert && docker run ...（同上）
```

## 测试

```bash
pip install pytest
pytest tests/test_convert.py -q
```
