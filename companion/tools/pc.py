"""PC操作（companion自身のcomputer use）。

UIAコントロール木を「目」、Invoke/キー送出を「手」として、
codex等を介さずcompanionが直接GUIを操作する。
低レベル関数(pc_windows/pc_controls/pc_click/...)と、
内部で閉じた行動ループを回す高レベル pc_task がある。
vision(スクショ→モデル)は PC_VISION で制御（モデル非対応ならUIAのみで動く）。

制約: 管理者権限で動くアプリにはUIPIで触れない。UIA非対応アプリは座標クリック頼み。
"""
import base64
import io
import json
import logging
import time

from ..config import (
    LLM_BASE_URL,
    PC_MAX_CONTROLS,
    PC_MAX_STEPS,
    PC_SHOT_DIR,
    PC_VISION,
)
from ..llm import current_params
from .actor import is_owner

log = logging.getLogger("companion")

_DENY = "PC操作はマスター専用なの"

_INTERACTIVE = {
    "Button", "Edit", "ListItem", "MenuItem", "TreeItem", "ComboBox",
    "TabItem", "Hyperlink", "CheckBox", "RadioButton", "Slider", "Spinner",
    "SplitButton", "Menu", "ToolBar", "Document", "Custom",
}
_NAMELESS_OK = {"Edit", "Slider", "Spinner", "Document"}


def _desktop():
    from pywinauto import Desktop
    return Desktop(backend="uia")


# --- win32 窓列挙 ---
# pywinauto の UIA 全窓走査は、無応答の窓（UWP停止中・ハングしたGUI）が
# 1つあると呼び出し自体が返らず tool を永久に詰まらせる。まず win32 の
# EnumWindows で hwnd だけ拾い、プロセス名/タイトルで絞った1枚だけを
# UIAWrapper に変換する。

def _enum_hwnds() -> list[int]:
    import ctypes
    from ctypes import wintypes
    hwnds: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, lparam):
        hwnds.append(int(hwnd))
        return True

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return hwnds


def _hwnd_pid(hwnd: int) -> int:
    import ctypes
    from ctypes import wintypes
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _hwnd_title(hwnd: int) -> str:
    """無応答の窓は空文字扱い（GetWindowText系がSendMessageで詰まるのを防ぐ）"""
    import ctypes
    user32 = ctypes.windll.user32
    if user32.IsHungAppWindow(hwnd):
        return ""
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _uia_wrap(hwnd: int):
    """hwnd 1枚だけを UIAWrapper に解決する（全窓走査を避ける）"""
    return _desktop().window(handle=hwnd).wrapper_object()


def _find_window(app: str):
    """トップレベル窓を探す。プロセス名一致を優先し、タイトル部分一致はフォールバック。
    （タイトルだけだと「Apple Music」等がIDEやブラウザのページタイトルに誤爆する）"""
    app_l = (app or "").lower().replace(" ", "")
    if not app_l:
        return None
    import ctypes
    import psutil
    user32 = ctypes.windll.user32
    title_hwnd = None
    for hwnd in _enum_hwnds():
        try:
            if not user32.IsWindowVisible(hwnd):
                continue
            exe = psutil.Process(_hwnd_pid(hwnd)).name().lower().replace(" ", "")
            if app_l in exe.replace(".exe", "") or app_l in exe:
                return _uia_wrap(hwnd)
            if title_hwnd is None and app_l in _hwnd_title(hwnd).lower().replace(" ", ""):
                title_hwnd = hwnd
        except Exception:
            continue
    return _uia_wrap(title_hwnd) if title_hwnd else None


def _dump_controls(win, limit: int = PC_MAX_CONTROLS) -> list[dict]:
    """対話的コントロールを列挙。[{i,type,name,auto_id,rect}] を返す"""
    out = []
    try:
        descs = win.descendants()
    except Exception:
        return out
    for d in descs:
        try:
            ei = d.element_info
            ctype = ei.control_type or ""
            if ctype not in _INTERACTIVE:
                continue
            name = (ei.name or "").strip()
            if not name and ctype not in _NAMELESS_OK:
                continue
            r = ei.rectangle
            if not r or r.width() <= 0 or r.height() <= 0:
                continue
            out.append({
                "type": ctype, "name": name[:80],
                "auto_id": (ei.automation_id or "")[:60],
                "rect": (r.left, r.top, r.right, r.bottom),
                "_w": d,
            })
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def _fmt_controls(controls: list[dict]) -> str:
    lines = []
    for i, c in enumerate(controls):
        aid = f' id={c["auto_id"]}' if c["auto_id"] else ""
        lines.append(f'[{i}] {c["type"]} "{c["name"]}"{aid}')
    return "\n".join(lines) or "(対話コントロールなし)"


def _invoke(ctrl) -> str:
    w = ctrl["_w"]
    try:
        w.invoke()
        return "invoke"
    except Exception:
        w.click_input()
        r = ctrl["rect"]
        return f"click({(r[0]+r[2])//2},{(r[1]+r[3])//2})"


_CHROME_ONLY = {"最小化", "復元", "閉じる", "Minimize", "Restore", "Close", "システム"}


def _press(app: str, labels: list[str], timeout: float = 20.0) -> tuple[bool, str]:
    """app窓が現れるのを待ち、labelsのどれかに名前/auto_idが合うコントロールを押す。
    コンテンツ描画に時間のかかるアプリ(WinUI等)向けに、chrome(タイトルバー)以外の
    コントロールが出るまでポーリングする。
    music.py の公式appバックエンドからも使う共有ヘルパ。"""
    deadline = time.time() + timeout
    win = None
    while time.time() < deadline and win is None:
        win = _find_window(app)
        if win is None:
            time.sleep(0.7)
    if win is None:
        return False, f"「{app}」のウィンドウが見つからない"
    try:
        if win.is_minimized():
            win.restore()
        win.set_focus()
    except Exception:
        pass
    labels_l = [l.lower() for l in labels]

    def _hit(c: dict) -> bool:
        name, aid = c["name"].lower(), c["auto_id"].lower()
        for label in labels_l:
            if label in name:
                return True
            # auto_idは部分一致だとSidebar_AllPlaylists等に誤爆するので
            # 先頭一致 or _区切りの語として含む場合のみ
            if aid and (aid.startswith(label) or f"_{label}" in aid):
                return True
        return False

    controls = []
    while time.time() < deadline:
        controls = _dump_controls(win)
        body = [c for c in controls if c["name"] not in _CHROME_ONLY]
        for c in body:
            if _hit(c):
                try:
                    how = _invoke(c)
                    return True, f'{how}: {c["type"]} "{c["name"]}"'
                except Exception as e:
                    return False, f'"{c["name"]}" の押下に失敗: {e}'
        if body:
            # 中身は出たが目的のボタンが無い → もう少し待ってから諦める
            time.sleep(1.0)
        else:
            time.sleep(1.2)
    names = ", ".join(f'"{c["name"]}"' for c in controls[:15] if c["name"])
    return False, f"押せるボタン({'/'.join(labels)})が見つからない。見えているのは: {names}"


def _dblclick_item(app: str, name_part: str, timeout: float = 20.0) -> tuple[bool, str]:
    """ListItem等の行をダブルクリックで再生・開く（Apple Musicの曲行等）"""
    deadline = time.time() + timeout
    win = None
    while time.time() < deadline and win is None:
        win = _find_window(app)
        if win is None:
            time.sleep(0.7)
    if win is None:
        return False, f"「{app}」のウィンドウが見つからない"
    try:
        if win.is_minimized():
            win.restore()
        win.set_focus()
    except Exception:
        pass
    needle = (name_part or "").lower()
    while time.time() < deadline:
        for c in _dump_controls(win):
            if c["type"] in ("ListItem", "TreeItem") and needle in c["name"].lower():
                try:
                    c["_w"].double_click_input()
                    return True, f'double-click: "{c["name"][:50]}"'
                except Exception as e:
                    return False, f'"{c["name"][:40]}" のダブルクリック失敗: {e}'
        time.sleep(1.2)
    return False, f"行「{name_part}」が見つからない"


def _shot_bytes(win=None) -> bytes:
    from PIL import ImageGrab
    if win is None:
        img = ImageGrab.grab()
    else:
        r = win.element_info.rectangle
        img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=60)
    return buf.getvalue()


# ---------- 公開ツール ----------

def pc_windows() -> str:
    if not is_owner():
        return _DENY
    import ctypes
    import psutil
    user32 = ctypes.windll.user32
    rows = []
    for hwnd in _enum_hwnds():
        try:
            if not user32.IsWindowVisible(hwnd):
                continue
            t = _hwnd_title(hwnd) or "(無題)"
            if not t.strip():
                continue
            exe = psutil.Process(_hwnd_pid(hwnd)).name()
            rows.append(f"- {t}  [{exe}]")
        except Exception:
            continue
    return "## 開いているウィンドウ\n" + "\n".join(sorted(set(rows))) if rows else "ウィンドウが見つからない"


def pc_controls(app: str) -> str:
    if not is_owner():
        return _DENY
    win = _find_window(app)
    if win is None:
        return f"「{app}」のウィンドウが見つからない（起動してから試して）"
    try:
        win.set_focus()
    except Exception:
        pass
    controls = _dump_controls(win)
    return f"## {win.window_text()} のコントロール ({len(controls)}件)\n" + _fmt_controls(controls)


def pc_click(app: str, target: str) -> str:
    if not is_owner():
        return _DENY
    win = _find_window(app)
    if win is None:
        return f"「{app}」のウィンドウが見つからない"
    try:
        win.set_focus()
    except Exception:
        pass
    controls = _dump_controls(win)
    tgt = (target or "").strip()
    c = None
    if tgt.isdigit() and int(tgt) < len(controls):
        c = controls[int(tgt)]
    else:
        for cc in controls:
            if tgt.lower() in cc["name"].lower() or tgt.lower() in cc["auto_id"].lower():
                c = cc
                break
    if c is None:
        return f"「{target}」が見つからない\n" + _fmt_controls(controls[:20])
    try:
        how = _invoke(c)
        return f'{how} → {c["type"]} "{c["name"]}" を操作しました'
    except Exception as e:
        return f"操作失敗: {e}"


def pc_type(app: str, target: str, text: str) -> str:
    if not is_owner():
        return _DENY
    win = _find_window(app)
    if win is None:
        return f"「{app}」のウィンドウが見つからない"
    try:
        win.set_focus()
    except Exception:
        pass
    controls = _dump_controls(win)
    tgt = (target or "").strip()
    c = None
    if tgt.isdigit() and int(tgt) < len(controls):
        c = controls[int(tgt)]
    else:
        for cc in controls:
            if tgt.lower() in cc["name"].lower() or tgt.lower() in cc["auto_id"].lower():
                c = cc
                break
    try:
        if c is not None and c["type"] == "Edit":
            c["_w"].set_edit_text(text)
            return f'"{c["name"] or "Edit"}" に入力しました'
        if c is not None:
            c["_w"].click_input()
        from pywinauto.keyboard import send_keys
        send_keys(text, with_spaces=True, with_tabs=True, with_newlines=True)
        return "キー入力しました"
    except Exception as e:
        return f"入力失敗: {e}"


def pc_key(keys: str) -> str:
    if not is_owner():
        return _DENY
    try:
        from pywinauto.keyboard import send_keys
        send_keys(keys, with_spaces=True)
        return f"キー送出: {keys}"
    except Exception as e:
        return f"キー送出失敗: {e}"


def pc_screenshot(app: str = "") -> str:
    if not is_owner():
        return _DENY
    win = _find_window(app) if app else None
    if app and win is None:
        return f"「{app}」のウィンドウが見つからない"
    try:
        data = _shot_bytes(win)
        p = PC_SHOT_DIR / f"shot_{int(time.time())}.jpg"
        p.write_bytes(data)
        return f"保存: {p}"  # 診断用。LLMへの画像入力はpc_task内で直接やる
    except Exception as e:
        return f"スクショ失敗: {e}"


# ---------- pc_task: 閉じた行動ループ ----------

_TASK_SYS = """あなたはWindows PCをUI Automationで操作するエージェント「companion」です。
コントロール一覧(index付き)を見て、ゴール達成に必要な次の1アクションだけをJSONで返してください。

{"action":"invoke|type|key|wait|done|fail","index":<番号>,"text":"...","reason":"短い一言"}

- invoke: indexのコントロールを押す/開く
- type: indexの入力欄にtextを入れる（indexはEdit等。省略時はフォーカス中の場所にキー入力）
- key: textにsend_keys形式でキー送出（"{ENTER}","{VK_SPACE}","^f"等）
- wait: textに秒数（読み込み待ちに使う）
- done: ゴール達成。reasonに成果を一言
- fail: これ以上進めない。reasonに理由

注意:
- 一覧に無い操作はできない。無理なら新しい一覧を待って次ステップで判断
- 日本語UIのボタン名は日本語（「再生」「シャッフル」等）
- 迷ったらkeyの"{ENTER}"やwaitも使ってよい
- 出力はJSONのみ。前後に文章を付けない

例: 検索ボックス(index 4)に「猫」と入れたい → {"action":"type","index":4,"text":"猫","reason":"検索語を入力"}"""


def _parse_action(text: str) -> dict | None:
    """応答中の最初の平衡したJSONオブジェクトを取り出す（{ENTER}等の中括弧を含んでもOK）"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if "action" in obj else None
                except Exception:
                    return None
    return None


_vision_cache: dict[str, bool] = {}


def _vision_ok(client, model: str) -> bool:
    """小さい画像を投げてモデルがvision対応か確認（結果はモデル毎にキャッシュ）"""
    if model in _vision_cache:
        return _vision_cache[model]
    ok = False
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (0, 0, 0)).save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "この画像は何色?"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
        )
        ok = True
    except Exception:
        log.info("pc vision probe failed; UIAのみで動作", exc_info=True)
    _vision_cache[model] = ok
    return ok


def pc_task(goal: str, app: str = "", max_steps: int | None = None) -> str:
    """goalを達成するまでUIA木→LLM→実行のループを内部で回す"""
    if not is_owner():
        return _DENY
    from openai import OpenAI
    client = OpenAI(base_url=LLM_BASE_URL, api_key="unused")
    model, _ = current_params()
    if PC_VISION not in ("auto", "off"):
        model = PC_VISION
    steps = max_steps or PC_MAX_STEPS
    vision = PC_VISION != "off" and _vision_ok(client, model)

    history: list[str] = []
    for i in range(steps):
        win = _find_window(app) if app else None
        if app and win is None:
            return f"「{app}」のウィンドウが見つからない（起動してから試して）"
        if win is not None:
            try:
                if win.is_minimized():
                    win.restore()
                win.set_focus()
            except Exception:
                pass
        controls = _dump_controls(win) if win is not None else []
        body = (
            f"ゴール: {goal}\n\n"
            f"コントロール一覧:\n{_fmt_controls(controls)}\n\n"
            f"これまでの行動:\n" + ("\n".join(history) or "(まだ何もしていない)")
        )
        content = [{"type": "text", "text": body}]
        if vision:
            try:
                b64 = base64.b64encode(_shot_bytes(win)).decode()
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            except Exception:
                pass
        try:
            res = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": _TASK_SYS},
                          {"role": "user", "content": content}],
                reasoning_effort="low",
            )
            raw = res.choices[0].message.content or ""
        except Exception as e:
            return f"LLM呼出し失敗: {e}\n" + "\n".join(history)
        act = _parse_action(raw)
        if act is None:
            history.append(f"step{i}: 応答を解釈できない: {raw[:80]}")
            continue
        a = str(act.get("action", "")).lower()
        idx = act.get("index")
        txt = str(act.get("text") or "")
        why = str(act.get("reason") or "")
        if a in ("done", "fail"):
            tail = f"step{i}: {a} — {why}"
            history.append(tail)
            mark = "達成" if a == "done" else "未達"
            return f"[{mark}] {why}\n--- 操作ログ ---\n" + "\n".join(history)
        try:
            if a == "invoke":
                if not isinstance(idx, int) or not (0 <= idx < len(controls)):
                    history.append(f"step{i}: invoke index {idx} は範囲外")
                    continue
                c = controls[idx]
                how = _invoke(c)
                history.append(f'step{i}: invoke [{idx}] {c["type"]} "{c["name"]}" ({how}) — {why}')
            elif a == "type":
                if isinstance(idx, int) and 0 <= idx < len(controls) and controls[idx]["type"] == "Edit":
                    controls[idx]["_w"].set_edit_text(txt)
                else:
                    if isinstance(idx, int) and 0 <= idx < len(controls):
                        controls[idx]["_w"].click_input()
                    from pywinauto.keyboard import send_keys
                    send_keys(txt, with_spaces=True, with_tabs=True, with_newlines=True)
                history.append(f"step{i}: type [{idx}] {txt[:30]!r} — {why}")
            elif a == "key":
                from pywinauto.keyboard import send_keys
                send_keys(txt or "{ENTER}", with_spaces=True)
                history.append(f"step{i}: key {txt!r} — {why}")
            elif a == "wait":
                time.sleep(min(float(txt or 1), 5.0))
                history.append(f"step{i}: wait {txt}s — {why}")
            else:
                history.append(f"step{i}: 未知のaction {a!r}")
        except Exception as e:
            history.append(f"step{i}: {a} 実行失敗: {e}")
    return f"ステップ上限({steps})に達しました\n--- 操作ログ ---\n" + "\n".join(history)
