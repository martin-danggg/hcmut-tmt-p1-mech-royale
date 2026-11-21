# Built by the organisers on linux/amd64. At run time: no network, read-only root
# (writable /tmp, 64 MB), 1 CPU, 512 MB, uid 65534, map mounted read-only at /arena.
FROM python:3.13-slim
WORKDIR /bot
COPY . .
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
