"""看板 Web 服务：标准库 http.server，零新增依赖。

路由表：
    GET /                          主看板 HTML
    GET /yearly                    年度排行页 HTML
    GET /api/summary?from=&to=&days=     KPI 数字
    GET /api/repos?from=&to=&days=       各仓库计数
    GET /api/contributors?from=&to=&days=&limit=  贡献者排行（PR/Issue 作者）
    GET /api/timeline?from=&to=&days=    按天聚合，喂热力图
    GET /api/events?from=&to=&days=&kind=&repo=&limit=  事件流明细
    GET /api/repo-list             仓库下拉选项（全表 distinct，与时间无关）
    GET /api/meta                  数据更新日期（页脚说明用）
    GET /api/yearly?repo=          年度全量排行（读 CSV，无时间参数）

除 /api/repo-list 和 /api/yearly 外，所有 /api/* 都接受 repo= 参数按仓库筛选。

events.db 只读连接，每请求新建。数据库路径、CSV 路径通过命令行参数或
环境变量传入，默认相对当前工作目录（跟 contributors 包同级运行）。
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import queries

STATIC_DIR = Path(__file__).parent / "static"

DB_PATH = os.environ.get("EVENTS_DB", "./data/events.db")
YEARLY_CSV = os.environ.get("YEARLY_CSV", "./output/daily/summary.csv")
# 年度排行的仓库筛选要逐仓库明细，summary.csv 只有跨仓库汇总行，拆不开
YEARLY_BY_REPO_CSV = os.environ.get(
    "YEARLY_BY_REPO_CSV", "./output/daily/by_repo.csv")


def _events_for_request(qs: dict) -> list:
    """取时间窗内的事件，并统一应用 repo 筛选。

    过滤放在这里而不是各聚合函数里：summary/repos/contributors/timeline 全都
    经过这个入口，一处过滤就能让整页数据跟着下拉框走，不用每个 API 各写一遍。
    """
    days = qs.get("days", [None])[0]
    from_ = qs.get("from", [None])[0]
    to_ = qs.get("to", [None])[0]
    repo = qs.get("repo", [None])[0]
    start, end = queries.parse_range(days, from_, to_)
    conn = queries.open_readonly(DB_PATH)
    try:
        events = queries.query_events(conn, start, end)
    finally:
        conn.close()
    return queries.filter_by_repo(events, repo)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 默认把每条请求打到 stderr，看板是内部工具，日志噪音没意义

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        path = parsed.path

        try:
            if path == "/" or path == "/index.html":
                self._serve_static("index.html", "text/html; charset=utf-8")
            elif path == "/yearly":
                self._serve_static("yearly.html", "text/html; charset=utf-8")
            elif path == "/api/summary":
                self._json(queries.summary(_events_for_request(qs)))
            elif path == "/api/repos":
                self._json(queries.repo_breakdown(_events_for_request(qs)))
            elif path == "/api/contributors":
                limit = int(qs.get("limit", [10])[0])
                self._json(queries.contributor_ranking(_events_for_request(qs), limit))
            elif path == "/api/timeline":
                self._json(queries.timeline(_events_for_request(qs)))
            elif path == "/api/events":
                kind = qs.get("kind", [None])[0]
                repo = qs.get("repo", [None])[0]
                limit = int(qs.get("limit", [50])[0])
                events = _events_for_request(qs)
                self._json(queries.event_stream(events, kind, repo, limit))
            elif path == "/api/meta":
                self._json({
                    "updated_at": queries.data_updated_at(
                        YEARLY_BY_REPO_CSV, DB_PATH),
                })
            elif path == "/api/repo-list":
                conn = queries.open_readonly(DB_PATH)
                try:
                    self._json(queries.repo_list(conn))
                finally:
                    conn.close()
            elif path == "/api/yearly":
                repo = qs.get("repo", [None])[0]
                rows = queries.read_yearly_by_repo(YEARLY_BY_REPO_CSV)
                if not rows:
                    # by_repo.csv 缺失时退回旧数据源，至少「全部仓库」还能看
                    self._json(queries.read_yearly_csv(YEARLY_CSV))
                else:
                    self._json({
                        "repos": queries.yearly_repo_options(rows),
                        "rows": queries.aggregate_yearly(rows, repo),
                    })
            else:
                self._not_found()
        except FileNotFoundError:
            self._error(500, "数据库或数据文件不存在，检查 EVENTS_DB/YEARLY_CSV 路径")
        except Exception as exc:  # noqa: BLE001 - 看板对外只暴露简短错误
            self._error(500, str(exc))

    def _serve_static(self, name: str, content_type: str):
        p = STATIC_DIR / name
        if not p.exists():
            self._not_found()
            return
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        self._error(404, "not found")

    def _error(self, code: int, message: str):
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global DB_PATH, YEARLY_CSV, YEARLY_BY_REPO_CSV
    p = argparse.ArgumentParser(description="DeepModeling 贡献看板")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--events-db", default=DB_PATH)
    p.add_argument("--yearly-csv", default=YEARLY_CSV)
    p.add_argument("--yearly-by-repo-csv", default=YEARLY_BY_REPO_CSV)
    args = p.parse_args()
    DB_PATH = args.events_db
    YEARLY_CSV = args.yearly_csv
    YEARLY_BY_REPO_CSV = args.yearly_by_repo_csv

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"dashboard listening on :{args.port}  db={DB_PATH}  yearly_csv={YEARLY_CSV}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
