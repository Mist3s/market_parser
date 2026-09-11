"""Проверка образа: приватный порт, SQLite переживает пересоздание, API отвечает без сети."""

import json
import subprocess
import sys
import time
import uuid


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


def main(image: str) -> None:
    name = f"mktlink-smoke-{uuid.uuid4().hex[:12]}"
    volume = f"{name}-data"
    network = f"{name}-net"
    docker("volume", "create", volume)
    docker("network", "create", "--internal", network)
    try:
        docker("run", "--rm", "--network", "none", "--user", "0:0",
               "-v", f"{volume}:/app/data", image, "chown", "10002:10002", "/app/data")
        docker("run", "--rm", "--network", "none", "-v", f"{volume}:/app/data",
               image, "mkt", "init-db")
        for _ in range(2):
            docker("run", "-d", "--name", name, "--network", network,
                   "--network-alias", "mktlink", "--read-only", "--tmpfs", "/tmp",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                   "-v", f"{volume}:/app/data", image)
            config = json.loads(docker("inspect", name))[0]
            assert not config["HostConfig"]["PortBindings"], "published port"
            assert config["Config"]["User"] == "10002:10002"
            for attempt in range(30):
                result = subprocess.run(
                    ["docker", "exec", name, "python", "/app/deploy/healthcheck.py"],
                    capture_output=True, check=False,
                )
                if result.returncode == 0:
                    break
                if attempt == 29:
                    raise RuntimeError("API did not become ready")
                time.sleep(1)
            # Отдельный клиент использует DNS Docker. Невалидная ссылка не вызывает магазин.
            docker("run", "--rm", "--network", network, image, "python", "-c", '''
import json, urllib.request, urllib.error
assert json.load(urllib.request.urlopen("http://mktlink:8000/readyz"))["ok"]
req = urllib.request.Request("http://mktlink:8000/v1/product",
    data=b'{"url":"https://example.org/"}', headers={"Content-Type":"application/json"})
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as exc:
    assert exc.code == 422
    assert json.load(exc)["status"] == "host_not_allowed"
else:
    raise AssertionError("invalid host accepted")
''')
            docker("rm", "-f", name)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        docker("network", "rm", network)
        docker("volume", "rm", volume)
    print("Image smoke test passed")


if __name__ == "__main__":
    main(sys.argv[1])
