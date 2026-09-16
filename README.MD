### 订阅转换服务（subconverter）

拉取机场 Clash 订阅，以**本地配置为唯一的配置来源**合并，返回最终 Clash 配置。

> 机场只贡献**代理节点**。除此之外的一切（代理组、规则、`dns`、端口、客户端行为……）
> **只使用本地配置**，机场的对应内容一律丢弃——不同机场的配置结构与规则集差异极大，
> 统一由本地决定后，换机场只影响节点列表，客户端里的行为完全不变。

## 请求处理流程

每个请求按固定的四步执行，日志中会分别打印 `第 1 步` ~ `第 4 步`，可直接据此判断走了哪条分支：

| 步骤 | 条件 | 行为 |
| --- | --- | --- |
| ① 拉取 | 有 `sub_url` | 每次请求都**实时拉取**机场订阅（网络优先） |
| ② 回退缓存 | ① 失败，且存在 `cache_ttl` 有效期内的缓存 | 使用内存缓存里的节点配置 |
| ③ 生成最小配置 | ① 失败，且缓存未命中（超期或从未拉取过）；或 `sub_url` 为空 | 根据本地配置生成最小配置文件（仅本地节点 + 本地代理组 / 规则） |
| ④ 转换 | 总是执行 | 除代理节点外全部取本地配置；把机场节点插入 `"*"` 表达式位置 |

> ①②③ 的产物都是第 ④ 步的输入，第 ④ 步始终执行，保证任一分支下输出都是完整可用的 Clash 配置。

## 核心特性

- `config.yaml` 既是服务配置文件，也是 Clash 配置本体。
- **除代理节点外只使用本地配置**：机场的 `proxy-groups` / `rules` / `dns` / 端口 / 客户端行为等一律不参与输出。换机场不会改变客户端的任何行为，只有节点列表变。
- 与 Clash 配置无关的控制项（密码、订阅链接等）在合并前自动剥离，不会泄漏到输出。
- 机场「节点配置」**每次请求都实时拉取**（网络优先）；拉取失败才回退内存缓存（有效期由 `cache_ttl` 控制，超期视同未命中）。流量用量（`subscription-userinfo`）来自当次拉取，因此始终最新。
- Home 节点 IP/端口支持通过 `server_url` 动态刷新（持久化到 `home_cache.yaml`），每次请求都刷新，不参与上述缓存。

## 转换规则（本地配置为准）

| 内容 | 策略 |
| --- | --- |
| 顶层 key 与顺序 | **完全按本地配置**：机场的顶层 key 一个都不会进入输出 |
| `proxy-groups` | **只使用本地配置**，机场的代理组整体丢弃（同名组也不会被机场覆盖） |
| `rules` | **只使用本地配置**，机场的规则整体丢弃 |
| `proxies` | 本地节点 + 机场节点，按 `name` 去重，**本地同名优先**；机场节点插入 `"*"` 表达式所在位置，未使用表达式时追加到末尾 |

> 本地配置里没有的项，输出里也不会有（不会从机场「继承」）。原先依赖机场提供的 `dns`、
> `unified-delay` 等，现在需要在本文件里显式声明——`config.yaml` 已给出可直接用的写法。

### 表达式 `"*"`：插入机场节点

写在代理组的 `proxies` 列表（或顶层 `proxies` 列表）中，代表「此处插入机场带来的全部节点」：

```yaml
proxy-groups:
  - name: 🚀 节点选择
    type: select
    proxies:
      - ♻️ 自动选择
      - "*"                    # ← 展开为机场的全部节点
      - DIRECT
  - name: ♻️ 自动选择
    type: url-test
    url: http://www.gstatic.com/generate_204
    interval: 300
    proxies:
      - "*"                    # ← 同样展开为机场的全部节点
```

> ⚠️ YAML 中**必须加引号**写作 `- "*"`。裸写 `- *` 是 YAML 的别名（alias）语法，
> 解析会直接失败（日志报 `while scanning an alias`），此时接口返回 `Hello World!`。

行为说明：

- 展开的是**机场带来的节点**：与本地模板同名的节点不算（本地定义优先），因此不会重复插入；
- 表达式在输出前一定被展开，不会残留到最终配置里；
- 同一组内自动去重，保留成员首次出现的位置；
- **组内只有一个表达式且没有机场节点时**，该组会兜底为 `DIRECT`（Clash 要求代理组至少有一个成员），
  避免机场不可用时输出非法配置；
- 本地配置里**完全没有**这个表达式时，机场节点仍会进入顶层 `proxies`，
  但不会被任何代理组引用（日志会给出 `本地配置未使用 * 表达式` 警告）。

> 想从输出中临时去掉某个顶层 key，直接注释掉配置文件里对应的那段即可。
> 无机场节点（`sub_url` 为空，或拉取失败且缓存未命中）时，第 ③ 步生成的最小配置文件就是本地模板本身，
> 表达式展开为空、组内兜底 `DIRECT`，再经第 ④ 步输出。

> 输出前有两项自证日志（仅在异常时出现）：`规则的目标不存在`（规则指向未定义的组或节点）、
> `代理组成员不存在`（组里写了不存在的节点名，含旧版表达式 `__REMOTE_PROXIES__` 的残留）。

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
| `cache_ttl` | 拉取失败时用于回退的缓存有效期（秒），`0` 表示永不过期。机场配置每次请求都会实时拉取，缓存仅作失败回退 |

> `remove_keys` / `exclude_groups` / `merge_groups` 已**废弃**：输出的顶层 key、代理组与规则现在全部只取本地配置，
> 无需再移除或排除任何内容（想临时去掉某个 key，直接注释掉配置文件里对应的那段）。
> 配置里若还留着这几项，不会生效（但会被当作控制项剥离，不会泄漏到输出配置中），可自行删除。

其下为 Clash 配置本体（顶层 key、`proxies`、`proxy-groups`、`rules`）。除代理节点外，
输出内容全部取自这里，所以要改客户端行为直接改本文件即可。

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
cache_ttl: 0           # 拉取失败时回退缓存的有效期（秒），0 表示永不过期

# ===================== Clash 配置模板 =====================
dns:
  enable: true
  enhanced-mode: fake-ip           # fake-ip 分流更准；需要真实解析可改 redir-host
  fake-ip-range: 198.18.0.1/16
  fake-ip-filter:                  # 命中这些域名时返回真实 IP
    - '*.lan'
    - '*.local'
    - '*.arpa'
    - '+.stun.*'
    - localhost.ptlogin2.qq.com
    # - nas.example.com            # 让 NAS 域名走真实解析（需本地 DNS 能解析它）
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

# 客户端行为（原先继承自机场，现改为本地声明；不需要就整行删掉）
ipv6: false
unified-delay: true
keep-alive-interval: 360
global-client-fingerprint: chrome

# 代理节点配置（机场节点会自动补充进来，无需在此列出）
proxies:
  - name: Home
    type: ss
    server: 11.11.11.11    # 改为你的 Home 节点地址
    port: 11
    cipher: aes-256-gcm
    password: change-me
    udp: true

# 代理组配置（只使用本文件中的配置，机场的代理组一律丢弃）
proxy-groups:
  - name: 🏠 Nas           # 自定义组：回家走 Home 节点
    type: select
    proxies:
      - Home
      - DIRECT
  - name: 🚀 节点选择       # 主选择组：手动挑选机场节点
    type: select
    proxies:
      - ♻️ 自动选择
      - "*"                # 展开为机场的全部节点
      - DIRECT
  - name: ♻️ 自动选择       # 自动测速：延迟最低的机场节点
    type: url-test
    url: http://www.gstatic.com/generate_204
    interval: 300
    proxies:
      - "*"                # 展开为机场的全部节点
  - name: 🐟 漏网之鱼       # 未匹配任何规则的流量
    type: select
    proxies:
      - 🚀 节点选择
      - DIRECT

# 规则配置（只使用本文件中的配置，机场的规则一律丢弃）
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
