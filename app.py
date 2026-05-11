#!/usr/bin/env python3
"""
法政大学 空き教室検索 — Flask Web アプリ
"""

import os
import threading
from flask import Flask, jsonify, render_template, request, abort
import hosei_empty_rooms as backend

app = Flask(__name__)

# キャッシュ再構築中フラグ（同時実行防止）
_rebuilding = False
_rebuild_lock = threading.Lock()


# -----------------------------------------------------------------------
# ページ
# -----------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# -----------------------------------------------------------------------
# API: キャッシュ状態
# -----------------------------------------------------------------------
@app.route("/api/status")
def api_status():
    nendo = request.args.get("nendo", type=int, default=backend.CURRENT_YEAR)
    cache = backend._load_cache(nendo)
    if cache:
        return jsonify({
            "has_cache":    True,
            "updated_at":   cache.get("updated_at", "")[:10],
            "course_count": len(cache.get("courses", [])),
            "nendo":        cache.get("nendo"),
        })
    return jsonify({"has_cache": False, "nendo": nendo})


# -----------------------------------------------------------------------
# API: 空き教室検索
# -----------------------------------------------------------------------
@app.route("/api/search")
def api_search():
    youbi  = request.args.get("youbi", "")
    jigen  = request.args.get("jigen",  type=int)
    campus = request.args.get("campus", "全キャンパス")
    nendo  = request.args.get("nendo",  type=int, default=backend.CURRENT_YEAR)

    if youbi not in backend.YOUBI_LIST:
        return jsonify({"error": f"不正な曜日: {youbi}"}), 400
    if jigen not in backend.JIGEN_LIST:
        return jsonify({"error": f"不正な時限: {jigen}"}), 400

    try:
        empty, used, all_rooms = backend.find_empty_rooms(
            youbi, jigen, campus, nendo
        )
        return jsonify({
            "empty":   empty,
            "used":    used,
            "total":   len(all_rooms),
            "youbi":   youbi,
            "jigen":   jigen,
            "campus":  campus,
            "nendo":   nendo,
        })
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503


# -----------------------------------------------------------------------
# API: キャッシュ再構築（管理者用）
# -----------------------------------------------------------------------
@app.route("/api/rebuild", methods=["POST"])
def api_rebuild():
    global _rebuilding

    # 簡易認証（環境変数 ADMIN_TOKEN を設定しておく）
    token = request.headers.get("X-Admin-Token", "")
    expected = os.environ.get("ADMIN_TOKEN", "hosei-admin")
    if token != expected:
        abort(403)

    with _rebuild_lock:
        if _rebuilding:
            return jsonify({"message": "既に再構築中です"}), 409
        _rebuilding = True

    nendo = backend.CURRENT_YEAR
    if request.is_json:
        nendo = request.json.get("nendo", nendo)

    def run():
        global _rebuilding
        try:
            backend.build_cache(nendo)
        finally:
            with _rebuild_lock:
                _rebuilding = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"message": f"{nendo}年度のキャッシュ再構築を開始しました"}), 202


@app.route("/api/rebuild/status")
def api_rebuild_status():
    return jsonify({"rebuilding": _rebuilding})


# -----------------------------------------------------------------------
# エントリポイント
# -----------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
