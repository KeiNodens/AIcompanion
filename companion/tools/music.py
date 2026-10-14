"""音楽再生（music_*ツール）。2系統バックエンド:

- cider: Cider(Apple Music非公式クライアント)の localhost RPC API を直接叩く。
  検索→曲ID指定再生→キュー→シャッフル→再生中取得まで完全自動化。
- apple: 公式Apple Music app（ロスレス向け）。musics: URIでページを開き、
  pc.py のUIA押下で「再生/シャッフル」を試す。押せなければ半自動と正直に返す。

出力デバイス名でバックエンドを自動選択（music_route でルール管理、state.json永続化。
例: ヘッドホンA-50→apple=ロスレス、その他→cider）。
検索は認証不要の iTunes Search API で trackId/artistId を解決して両バックエンドで共用。
再生リクエストは毎回 mem0(user)＋self note に保存して「好みでかけて」に効かせる。
"""
import logging
import os
import random
import subprocess
import time
from datetime import datetime

import requests
from ..config import (
    CIDER_API_BASE,
    CIDER_APP_ID,
    CIDER_TOKEN,
    ITUNES_COUNTRY,
    MUSIC_DEFAULT_BACKEND,
    MUSIC_URI_SCHEME,
)
from ..db import load_state, update_state
from ..memory import add_self_note, add_user_fact, search_user
from .actor import is_owner
from . import pc

log = logging.getLogger("companion")

_DENY = "音楽の操作はマスター専用なの"

_BACKENDS = ("cider", "apple")


# ---------- Cider RPC ----------

def _cider(path: str, method: str = "GET", body=None, timeout: float = 8):
    url = CIDER_API_BASE.rstrip("/") + path
    headers = {"apptoken": CIDER_TOKEN} if CIDER_TOKEN else {}
    r = requests.request(method, url, json=body, headers=headers, timeout=timeout)
    if r.status_code == 401:
        raise RuntimeError(
            "CiderのAPI認証に失敗。Settings→Connectivityで外部アクセスを有効化し、"
            "トークンを.envのCIDER_TOKENに設定して")
    if r.status_code == 204:
        return {}
    if r.status_code >= 400:
        raise RuntimeError(f"cider {path} -> {r.status_code}: {r.text[:150]}")
    try:
        return r.json() or {}
    except Exception:
        return {}


def _cider_alive() -> bool:
    try:
        _cider("/playback/active", timeout=3)
        return True
    except Exception:
        return False


def _ensure_cider() -> bool:
    """APIが死んでたらアプリ起動を試みてAPIを待つ"""
    if _cider_alive():
        return True
    try:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{CIDER_APP_ID}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    for _ in range(25):
        if _cider_alive():
            return True
        time.sleep(1)
    return False


# ---------- 検索（iTunes Search API。認証不要・両バックエンド共用） ----------

def _search(term: str, entity: str = "song", limit: int = 8) -> list[dict]:
    r = requests.get(
        "https://itunes.apple.com/search",
        params={
            "term": term, "entity": entity, "country": ITUNES_COUNTRY,
            "media": "music", "limit": limit, "lang": "ja_jp",
        },
        timeout=10,
    )
    return r.json().get("results", [])


def _artist_top_songs(artist_id, limit: int = 25) -> list[dict]:
    r = requests.get(
        "https://itunes.apple.com/lookup",
        params={"id": artist_id, "entity": "song", "limit": limit,
                "country": ITUNES_COUNTRY, "lang": "ja_jp"},
        timeout=10,
    )
    return [x for x in r.json().get("results", []) if x.get("wrapperType") == "track"]


def _name_close(query: str, name: str) -> bool:
    q, n = query.lower().strip(), (name or "").lower().strip()
    if not q or not n:
        return False
    if q in n or n in q:
        return True
    import difflib
    return difflib.SequenceMatcher(None, q, n).ratio() >= 0.55


# ---------- 出力デバイス → バックエンド選択 ----------

def _default_output_device() -> str:
    try:
        from pycaw.pycaw import AudioUtilities
        return AudioUtilities.GetSpeakers().FriendlyName or ""
    except Exception:
        return ""


def _routes() -> list[dict]:
    return list(load_state().get("music_routes") or [])


def _pick_backend(backend=None) -> str:
    if backend in _BACKENDS:
        return backend
    dev = _default_output_device().lower()
    for r in _routes():
        if (r.get("match") or "").lower() in dev:
            return r.get("backend") or "cider"
    if MUSIC_DEFAULT_BACKEND in _BACKENDS:
        return MUSIC_DEFAULT_BACKEND
    return "cider" if _cider_alive() else "apple"


# ---------- 再生実装 ----------

def _cider_play_now() -> None:
    """play-itemはロードされるが再生開始しないことがあるので/playで確実に鳴らす"""
    try:
        time.sleep(1.5)
        _cider("/playback/play", "POST")
    except Exception:
        pass


def _cider_play_artist(artist_id, artist_name: str) -> str:
    """アーティストradio(ra.<id>)で流す。駄目ならトップ曲をキューしてシャッフル"""
    try:
        _cider("/playback/play-item", "POST", {"type": "stations", "id": f"ra.{artist_id}"})
        _cider_play_now()
        return "アーティストradioを開始"
    except Exception as e:
        log.info("station play failed, fallback to top-songs queue: %s", e)
    songs = _artist_top_songs(artist_id, 20)
    if not songs:
        raise RuntimeError(f"{artist_name} の曲が見つからない")
    first, rest = songs[0], songs[1:]
    _cider("/playback/play-item", "POST", {"type": "songs", "id": str(first["trackId"])})
    _cider_play_now()
    random.shuffle(rest)
    for s in rest[:15]:
        try:
            _cider("/playback/play-later", "POST", {"type": "songs", "id": str(s["trackId"])})
        except Exception:
            pass
    return f"トップ曲{len(rest[:15]) + 1}曲をキューして再生"


def _apple_open(url: str, want_shuffle: bool, track_name: str | None = None) -> str:
    """公式appを musics: URIで開き、UIAで再生/シャッフル押下を試す。
    station(ra.*)は自動再生する。album/songはページ側ボタン
    （PlayButtonElement/ShuffleButtonElement）を押す。曲指定があれば行をダブルクリック。"""
    uri = MUSIC_URI_SCHEME + url[len("https:"):]
    try:
        os.startfile(uri)
    except Exception as e:
        return f"URIを開けない: {e}（{uri}）"
    if track_name:
        ok, detail = pc._dblclick_item("Apple Music", track_name, timeout=20)
        if ok:
            return f"アプリで開いて曲を再生開始（{detail}）"
    # ページ側ボタンをauto_idで直指定（トランスポートの同名ボタンと区別）
    primary = "ShuffleButtonElement" if want_shuffle else "PlayButtonElement"
    ok, detail = pc._press("Apple Music", [primary], timeout=20)
    if not ok:
        ok, detail = pc._press(
            "Apple Music",
            ["シャッフル", "Shuffle"] if want_shuffle else ["再生", "Play", "シャッフル"],
            timeout=6)
    if ok:
        return f"アプリで開いて再生開始（{detail}）"
    return f"アプリでページを開いた（再生は手動で押して。{detail}）"


def _media_key(name: str) -> None:
    import win32api
    import win32con
    vk = {"next": 0xB0, "previous": 0xB1, "stop": 0xB2, "playpause": 0xB3,
          "mute": 0xAD, "vol_down": 0xAE, "vol_up": 0xAF}[name]
    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def _smtc_now():
    """全SMTCセッションから再生中を優先して (title, artist, app名) を返す"""
    try:
        import asyncio
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as M,
        )

        async def go():
            mgr = await M.request_async()
            rows = []
            for s in mgr.get_sessions():
                try:
                    p = await s.try_get_media_properties_async()
                    st = s.get_playback_info().playback_status
                    rows.append((st, p.title, p.artist, s.source_app_user_model_id))
                except Exception:
                    continue
            rows.sort(key=lambda r: 0 if r[0] == 4 else 1)  # Playing優先
            return rows[0][1:] if rows else None

        return asyncio.run(go())
    except Exception:
        return None


def _smtc_sessions() -> list[tuple]:
    """全セッションの (app_id, title, artist, status) 一覧"""
    try:
        import asyncio
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as M,
        )

        async def go():
            mgr = await M.request_async()
            rows = []
            for s in mgr.get_sessions():
                try:
                    p = await s.try_get_media_properties_async()
                    st = s.get_playback_info().playback_status
                    rows.append((s.source_app_user_model_id, p.title, p.artist, int(st)))
                except Exception:
                    continue
            return rows

        return asyncio.run(go())
    except Exception:
        return []


_APP_LABELS = [
    ("AppleMusicWin", "Apple Music"), ("Cider", "Cider"),
    ("MSEdge", "Edge"), ("chrome", "Chrome"), ("ZuneMusic", "Media Player"),
]


def _app_label(app_id: str) -> str:
    for k, v in _APP_LABELS:
        if k.lower() in app_id.lower():
            return v
    return app_id.split("!")[0].split(".")[-1][:20]


def _remember_play(desc: str, backend: str) -> None:
    try:
        add_user_fact(
            f"[{datetime.now():%Y-%m-%d}] ユーザーは{desc}の再生をリクエストし、{backend}で再生した")
    except Exception:
        log.debug("music fact write failed", exc_info=True)
    try:
        add_self_note(f"マスターのために{desc}を{backend}で流した", importance=2)
    except Exception:
        log.debug("music add_self_note failed", exc_info=True)


# ---------- 公開ツール ----------

def music_play(query: str = "", kind: str = "auto", backend: str | None = None) -> str:
    if not is_owner():
        return _DENY
    query = (query or "").strip()
    if not query:
        return "再生するものを指定してください。参考・好みの記憶:\n" + music_taste()

    be = _pick_backend(backend)
    dev = _default_output_device()

    # --- 解決: kind決定 + ID取得 ---
    if kind not in ("song", "album", "artist"):
        artists = _search(query, "musicArtist", 3)
        if artists and _name_close(query, artists[0].get("artistName", "")):
            kind = "artist"
        else:
            kind = "song"

    try:
        if kind == "artist":
            artists = _search(query, "musicArtist", 3)
            if not artists:
                return f"アーティスト「{query}」が見つからない"
            a = artists[0]
            aid, aname = a["artistId"], a.get("artistName", query)
            if be == "cider":
                if not _ensure_cider():
                    return "Ciderが起動できない。Ciderを起動するか backend=apple で試して"
                msg = _cider_play_artist(aid, aname)
            else:
                url = f"https://music.apple.com/{ITUNES_COUNTRY}/station/ra.{aid}"
                msg = _apple_open(url, want_shuffle=True)
            _remember_play(f"アーティスト「{aname}」の曲（シャッフル）", be)
            return f"[{be}/{dev}] {aname} — {msg}"

        if kind == "album":
            albums = _search(query, "album", 5)
            if not albums:
                return f"アルバム「{query}」が見つからない"
            al = albums[0]
            cid = al["collectionId"]
            title = f"{al.get('collectionName')} — {al.get('artistName')}"
            if be == "cider":
                if not _ensure_cider():
                    return "Ciderが起動できない"
                _cider("/playback/play-item", "POST", {"type": "albums", "id": str(cid)})
                _cider_play_now()
                msg = "アルバム再生を開始"
            else:
                msg = _apple_open(f"https://music.apple.com/{ITUNES_COUNTRY}/album/{cid}",
                                  want_shuffle=False)
            _remember_play(f"アルバム「{al.get('collectionName')}」", be)
            return f"[{be}/{dev}] {title} — {msg}"

        # kind == song
        songs = _search(query, "song", 5)
        if not songs:
            return f"曲「{query}」が見つからない"
        s = songs[0]
        tid, cid = s["trackId"], s["collectionId"]
        title = f"{s.get('trackName')} — {s.get('artistName')}"
        if be == "cider":
            if not _ensure_cider():
                return "Ciderが起動できない"
            _cider("/playback/play-item", "POST", {"type": "songs", "id": str(tid)})
            _cider_play_now()
            msg = "再生を開始"
        else:
            msg = _apple_open(
                f"https://music.apple.com/{ITUNES_COUNTRY}/album/{cid}?i={tid}",
                want_shuffle=False, track_name=s.get("trackName"))
        _remember_play(f"曲「{s.get('trackName')}」({s.get('artistName')})", be)
        return f"[{be}/{dev}] {title} — {msg}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        log.exception("music_play failed")
        return f"再生に失敗: {e}"


def music_search(query: str, kind: str = "auto") -> str:
    if not is_owner():
        return _DENY
    query = (query or "").strip()
    if not query:
        return "検索語を指定してください"
    entity = {"artist": "musicArtist", "album": "album"}.get(kind, "song")
    items = _search(query, entity, 6)
    if not items:
        return "見つからない"
    lines = []
    for x in items:
        if entity == "musicArtist":
            lines.append(f"- {x.get('artistName')} (artistId={x.get('artistId')})")
        elif entity == "album":
            lines.append(f"- {x.get('collectionName')} — {x.get('artistName')} (albumId={x.get('collectionId')})")
        else:
            lines.append(f"- {x.get('trackName')} — {x.get('artistName')} [{x.get('collectionName')}]")
    return "\n".join(lines)


def _active_backend() -> str:
    """実際に鳴っている側を推定。ciderが再生中ならcider、それ以外はapple/メディアキー"""
    try:
        if _cider_alive() and _cider("/playback/is-playing").get("is_playing"):
            return "cider"
    except Exception:
        pass
    return "apple"


def music_control(action: str, volume: int | None = None) -> str:
    if not is_owner():
        return _DENY
    a = (action or "").lower().strip()
    # システム音量系はどのバックエンドにも効くメディアキーで
    if a in ("volume_up", "vol_up"):
        _media_key("vol_up")
        return "音量を上げた"
    if a in ("volume_down", "vol_down"):
        _media_key("vol_down")
        return "音量を下げた"
    if a == "mute":
        _media_key("mute")
        return "消音を切替えた"

    if _active_backend() == "cider":
        table = {
            "play": "/playback/play", "pause": "/playback/pause",
            "resume": "/playback/play", "playpause": "/playback/playpause",
            "next": "/playback/next", "previous": "/playback/previous",
            "stop": "/playback/stop", "repeat": "/playback/toggle-repeat",
            "shuffle": "/playback/toggle-shuffle",
            "autoplay": "/playback/toggle-autoplay",
        }
        if a == "volume":
            try:
                _cider("/playback/volume", "POST", {"volume": max(0, min(100, int(volume))) / 100})
                return f"Ciderの音量を {volume}% にした"
            except Exception as e:
                return f"音量変更失敗: {e}"
        if a in table:
            try:
                _cider(table[a], "POST")
                return f"Cider: {a} 実行"
            except Exception as e:
                return f"失敗: {e}"
    else:
        # 公式app等: UIAトランスポートボタン（auto_id直指定で確実）。駄目ならメディアキー
        uia_table = {
            "play": "TransportControl_PlayPauseStop",
            "pause": "TransportControl_PlayPauseStop",
            "resume": "TransportControl_PlayPauseStop",
            "playpause": "TransportControl_PlayPauseStop",
            "stop": "TransportControl_PlayPauseStop",
            "next": "TransportControl_SkipForward",
            "previous": "TransportControl_SkipBack",
            "shuffle": "ShuffleButton",
            "repeat": "RepeatButton",
        }
        if a in uia_table and pc._find_window("Apple Music") is not None:
            ok, detail = pc._press("Apple Music", [uia_table[a]], timeout=5)
            if ok:
                return f"Apple Music: {a} 実行（{detail}）"
        # UIA失敗/別アプリ → メディアキー（最後のメディアセッションに効く）
        if a in ("pause", "play", "resume", "playpause", "stop"):
            _media_key("playpause" if a != "stop" else "stop")
            return "再生/一時停止を切替えた（メディアキー）"
        if a in ("next", "previous"):
            _media_key(a)
            return f"{a} を実行（メディアキー）"
        return f"「{action}」はCider起動中のみ対応（Ciderを起動して）"
    return f"不明な操作: {action}（play/pause/next/previous/stop/shuffle/repeat/autoplay/volume/mute）"


def music_now() -> str:
    if not is_owner():
        return _DENY
    # 鳴っている側優先。ciderが再生中なら詳細情報を、それ以外はSMTC全セッションから
    try:
        if _cider_alive() and _cider("/playback/is-playing").get("is_playing"):
            info = _cider("/playback/now-playing").get("info") or {}
            if info.get("name"):
                return (
                    f"♪ {info['name']} — {info.get('artistName','?')} "
                    f"（{info.get('albumName','?')}）[Cider shuffle={info.get('shuffleMode')}]"
                )
    except Exception:
        pass
    rows = _smtc_sessions()
    if rows:
        rows.sort(key=lambda r: 0 if r[3] == 4 else 1)
        lines = [f"♪ {t} — {a}（{_app_label(app)}）" for app, t, a, st in rows if t]
        if lines:
            return "\n".join(lines[:3])
    return "今は何も鳴っていない（または取得できない）"


def music_route(action: str = "list", device_pattern: str | None = None,
                backend: str | None = None) -> str:
    if not is_owner():
        return _DENY
    dev = _default_output_device()
    routes = _routes()
    a = (action or "list").lower()
    if a == "list":
        lines = [f"現在の出力デバイス: {dev or '(不明)'}"]
        if routes:
            lines += [f"- 「{r['match']}」を含む → {r['backend']}" for r in routes]
        else:
            lines.append("ルールなし（既定: ciderが生きていればcider、なければapple）")
        return "\n".join(lines)
    if a == "add":
        pat = (device_pattern or "").strip()
        be = (backend or "").lower()
        if not pat:
            return "device_pattern（出力デバイス名の一部、例: A-50）を指定してください"
        if be not in _BACKENDS:
            return "backend は cider か apple です"

        def apply_add(state):
            rs = [r for r in list(state.get("music_routes") or []) if r.get("match") != pat]
            rs.append({"match": pat, "backend": be})
            state["music_routes"] = rs

        update_state(apply_add)
        try:
            add_user_fact(
                f"音楽再生ルール: 出力デバイス名に「{pat}」を含むときは{be}で再生する")
        except Exception:
            pass
        return f"ルール追加: 「{pat}」→ {be}（現在の出力: {dev}）"
    if a == "remove":
        pat = (device_pattern or "").strip()

        def apply_remove(state):
            state["music_routes"] = [
                r for r in list(state.get("music_routes") or []) if r.get("match") != pat
            ]

        update_state(apply_remove)
        return f"ルール削除: 「{pat}」"
    return "action は list/add/remove です"


def music_taste() -> str:
    if not is_owner():
        return _DENY
    try:
        res = search_user(
            "音楽の好み 好きなアーティスト 曲 ジャンル 歌手 BGM",
            top_k=10)
        items = res.get("results", []) if isinstance(res, dict) else (res or [])
        lines = [f"- {m.get('memory', m)}" for m in items]
        if not lines:
            return "音楽の好みはまだ記憶していない"
        return "音楽の好みとして記憶しているもの:\n" + "\n".join(lines)
    except Exception as e:
        return f"記憶の検索に失敗: {e}"


def music_like() -> str:
    if not is_owner():
        return _DENY
    if not _cider_alive():
        return "この機能はCiderで再生中のみ使える（公式appのライブラリ追加は自動化できない）"
    try:
        info = _cider("/playback/now-playing").get("info") or {}
        name, artist = info.get("name"), info.get("artistName")
        pid = (info.get("playParams") or {}).get("id")
        if not pid:
            return "再生中の曲が取得できない"
        try:
            _cider("/playback/add-to-library", "POST", {"type": "songs", "id": str(pid)})
            msg = "ライブラリに追加した"
        except Exception:
            _cider("/playback/add-to-library", "POST", {})
            msg = "ライブラリ追加を要求した"
        try:
            add_user_fact(
                f"ユーザーは曲「{name}」({artist})を気に入った（ライブラリ追加）")
        except Exception:
            pass
        return f"♪ {name} — {artist}: {msg}"
    except Exception as e:
        return f"失敗: {e}"
