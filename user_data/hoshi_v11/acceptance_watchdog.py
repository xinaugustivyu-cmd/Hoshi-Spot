"""Standalone, bounded watchdog for one named acceptance run; never removes data."""

from __future__ import annotations

import json
import logging
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


log = logging.getLogger(__name__)
LABEL = "com.hoshi.acceptance_run_id"
MAX_SECONDS = 30 * 60
ID_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_RE = re.compile(r"^v11-acceptance-[a-z0-9-]{8,64}$")


@dataclass(frozen=True)
class Manifest:
    run_id: str
    started_at: datetime
    container_ids: tuple[str, ...]

    @classmethod
    def read(cls, path: Path) -> "Manifest":
        data = json.loads(path.read_text(encoding="utf-8"))
        started = datetime.fromisoformat(data["started_at"])
        ids = tuple(data["container_ids"])
        if (not RUN_RE.fullmatch(data["run_id"]) or started.tzinfo is None
                or not 1 <= len(ids) <= 4 or len(set(ids)) != len(ids)
                or any(not ID_RE.fullmatch(value) for value in ids)):
            raise ValueError("验收看门狗清单无效")
        return cls(data["run_id"], started.astimezone(timezone.utc), ids)


class DockerControl:
    def inspect(self, container_id: str) -> dict:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .}}", container_id],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return json.loads(result.stdout)

    def stop(self, container_id: str) -> None:
        subprocess.run(["docker", "stop", "--time", "5", container_id],
                       check=True, capture_output=True, text=True, timeout=15)


class AcceptanceWatchdog:
    def __init__(self, manifest: Manifest, docker: DockerControl,
                 now=None, sleep=None):
        self.manifest = manifest
        self.docker = docker
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time.sleep
        self.owned: set[str] = set()

    def _inspect_owned(self, container_id: str) -> bool:
        info = self.docker.inspect(container_id)
        if (info.get("Id") != container_id
                or info.get("Config", {}).get("Labels", {}).get(LABEL) != self.manifest.run_id):
            raise RuntimeError("验收容器身份或归属标签不符")
        self.owned.add(container_id)
        return info.get("State", {}).get("Running") is True

    def stop_owned(self) -> None:
        # Exact preverified IDs only. No compose down, rm, volume or host-wide action.
        errors = []
        for container_id in sorted(self.owned):
            try:
                self.docker.stop(container_id)
                log.info("验收容器已停止：%s", container_id)
            except Exception as exc:
                log.exception("验收容器停止失败：%s", container_id)
                errors.append(str(exc))
        if errors:
            raise RuntimeError("部分验收容器无法停止；保留数据并需人工检查")

    def run(self) -> str:
        deadline = self.manifest.started_at + timedelta(seconds=MAX_SECONDS)
        try:
            if self.now() < self.manifest.started_at - timedelta(seconds=30):
                raise RuntimeError("验收启动时间来自未来")
            # Verify every ID before entering the monitoring loop.
            for container_id in self.manifest.container_ids:
                self._inspect_owned(container_id)
            while True:
                if self.now() >= deadline:
                    return "timeout"
                if not all(self._inspect_owned(cid) for cid in self.manifest.container_ids):
                    return "component_exit"
                self.sleep(min(2.0, max(0.0, (deadline - self.now()).total_seconds())))
        finally:
            self.stop_owned()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        raise SystemExit("用法：python -m hoshi_v11.acceptance_watchdog <manifest.json> <logfile>")
    manifest = Manifest.read(Path(args[0]))
    log_path = Path(args[1])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_path, level=logging.INFO,
                        format="%(asctime)sZ %(levelname)s %(message)s")
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt("验收看门狗收到停止信号")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        log.info("看门狗启动：%s，最迟 30 分钟停止", manifest.run_id)
        result = AcceptanceWatchdog(manifest, DockerControl()).run()
        log.info("看门狗结束：%s", result)
        return 0
    except BaseException:
        log.exception("看门狗异常，验收必须人工核实停止状态")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
