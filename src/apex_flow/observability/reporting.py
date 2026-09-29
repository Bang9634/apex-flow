"""JSONL 보고서는 실행별 새 파일에 기록한다. 스냅샷 쓰기는 이벤트 루프 밖에서 수행한다."""
import asyncio
import json
from pathlib import Path


class JsonlReporter:
    def __init__(self, path, metadata):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 기존 결과/DB 경로를 실수로 덮어쓰지 않는다.
        with self.path.open("x", encoding="utf-8") as file:
            file.write(json.dumps(dict(type="run", **metadata), default=str) + "\n")

    def _append(self, record):
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, allow_nan=False) + "\n")

    async def write(self, record):
        # 취소 시 이미 시작된 thread 쓰기가 끝나야 final 보고서와 순서가 뒤섞이지 않는다.
        task = asyncio.create_task(asyncio.to_thread(self._append, record))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
