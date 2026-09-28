"""Single-process SQLite journal. Orders and account updates commit atomically."""
import json
import sqlite3
from pathlib import Path
import fcntl


class Store:
    """원본 이벤트와 paper 상태를 저장한다. put/save_order의 커밋은 호출자가 책임진다."""
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Advisory exclusive lock prevents two engines from spending the same cash.
        self.lock = open(str(Path(path).resolve()) + ".lock", "a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("Database already in use by another ApexFlow process") from None
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        # WAL은 저널 방식, FULL은 내구성 설정이다. 이 쓰기는 여전히 동기식이다.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, received_ms INTEGER NOT NULL,
                stream TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                client_id TEXT PRIMARY KEY, request TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT NOT NULL);
        ''')

    def event(self, stream, payload, received_ms):
        """수신 이벤트 한 건을 자체 트랜잭션으로 저장한다. 원본의 중복도 보존한다."""
        with self.db:
            self.db.execute("INSERT INTO events(received_ms,stream,payload) VALUES(?,?,?)",
                            (received_ms, stream, json.dumps(payload, separators=(",", ":"))))

    def get(self, key, default=None):
        """JSON 상태를 복원한다. 키가 없으면 default를 반환한다."""
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        """상태를 갱신한다. 여러 변경의 원자성을 위해 여기서는 commit하지 않는다."""
        self.db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, default=str)))

    def order(self, client_id):
        """주문 한 건의 최신 상태. request/result는 아직 JSON 문자열이다."""
        row = self.db.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
        return dict(row) if row else None

    def save_order(self, client_id, request, status, result):
        """최신 주문 결과를 덮어쓴다. append-only 상태 전이 로그는 아직 아니다."""
        self.db.execute("INSERT INTO orders VALUES(?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET status=excluded.status,result=excluded.result",
                        (client_id, json.dumps(request, sort_keys=True), status, json.dumps(result, default=str)))

    def close(self):
        """DB 연결과 프로세스 잠금을 해제한다."""
        self.db.close()
        self.lock.close()
