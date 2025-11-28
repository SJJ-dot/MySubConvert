# 使用官方的 Python 镜像
FROM python:3.9-slim

# 设置时区为东八区（北京时间）
ENV TZ=Asia/Shanghai
# 安装tzdata包并设置时区（如果基础镜像不包含tzdata）
RUN apt-get update && apt-get install -y tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 在容器中设置工作目录
WORKDIR /app

# 复制 requirements.txt 文件到容器中
COPY requirements.txt .

# 安装依赖项
RUN pip install --no-cache-dir -r requirements.txt

# 复制其余的应用程序代码到容器中
COPY . .

# 暴露 Flask 应用程序运行的端口
EXPOSE 5000

# 定义运行 Flask 应用程序的命令
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "120", "--log-level", "info", "--error-logfile", "-", "--access-logfile", "-", "main:app"]

