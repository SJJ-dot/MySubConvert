### NAS 回家信息配置服务
接口说明：
1. 设置节点ip和端口
- 请求方式： `get` 
- 路径：`/set_config_ip_port`
- 参数：
```
{
    "ip": "192.168.1.1",
    "port": 8080,
    "name": "Home", // 节点名称
    "password": "123456" // 请求接口的密码
}
```
2. 获取订阅信息
- 请求方式： `get`
- 路径：`/api` ，和配置文件中的 `api_path` 一致
- 参数：`password` ，和配置文件中的 `password` ,请求接口的密码

### docker
```
docker pull ghcr.io/sjj-dot/mysubconvert:latest
```
