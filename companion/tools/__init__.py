import asyncio
import dataclasses
import logging

from . import (
    body,
    delegate,
    docs,
    files,
    history,
    inner,
    lists,
    media,
    memory_ops,
    modelctl,
    music,
    pc,
    reminders,
    selfops,
    skills,
    tasks,
)
from .actor import is_owner

log = logging.getLogger("companion")

_DENY = "それはマスター専用かな"


def _tool(name, description, properties, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                **({"required": required} if required else {}),
            },
        },
    }


_s = lambda d: {"type": "string", "description": d}
_i = lambda d: {"type": "integer", "description": d}
_b = lambda d: {"type": "boolean", "description": d}

TOOLS = [
    _tool(
        "tasks_add",
        "ユーザーのタスク・やること・課題を管理リストに追加する",
        {
            "title": _s("タスク名"),
            "due": _s("期限（YYYY-MM-DD等）。不明なら省略"),
            "note": _s("補足メモ。任意"),
        },
        ["title"],
    ),
    _tool("tasks_list", "タスク一覧を取得する", {"include_done": _b("完了済みも含めるか")}),
    _tool(
        "tasks_complete",
        "タスクを完了にする。idか名前の一部で指定",
        {"task_id": _i("タスクID"), "query": _s("タスク名の一部")},
    ),
    _tool(
        "tasks_delete",
        "タスクを削除する。idか名前の一部で指定",
        {"task_id": _i("タスクID"), "query": _s("タスク名の一部")},
    ),
    _tool(
        "list_add",
        "「行きたい場所」「買い物」などのリストに項目を追加する。リスト名は自由",
        {
            "list_name": _s("リスト名（例: 行きたい場所, 買い物, 読みたい本）"),
            "item": _s("追加する項目"),
            "note": _s("補足（URL、値段、場所など）。任意"),
        },
        ["list_name", "item"],
    ),
    _tool(
        "list_show",
        "指定したリストの中身を取得する",
        {"list_name": _s("リスト名の全部または一部")},
        ["list_name"],
    ),
    _tool(
        "list_remove",
        "リストから項目を削除する",
        {"item_id": _i("項目ID"), "query": _s("項目名の一部")},
    ),
    _tool("list_names", "存在するリスト名と件数を取得する", {}),
    _tool(
        "remind_at",
        "指定時刻にリマインドを投稿する。「明日9時に教えて」等で使う。時刻は絶対日時に変換して渡す",
        {
            "when": _s("発火時刻 YYYY-MM-DD HH:MM"),
            "text": _s("リマインド内容"),
        },
        ["when", "text"],
    ),
    _tool("reminders_list", "設定済みリマインダー一覧を取得する", {}),
    _tool(
        "reminder_cancel",
        "リマインダーを削除する",
        {"reminder_id": _i("リマインダーID"), "query": _s("内容の一部")},
    ),
    _tool(
        "media_log",
        "読書・映画・アニメ・ゲーム等の作品を観た/読んだ記録をObsidianに残す。「〜読み終わった」「〜観た」で使う",
        {
            "title": _s("作品タイトル"),
            "kind": _s("種別: 本/映画/アニメ/ゲーム/ドラマ等"),
            "note": _s("感想メモ。任意"),
            "rating": _i("1-5の評価。任意"),
        },
        ["title"],
    ),
    _tool("media_list", "Obsidianに記録した作品タイトル一覧", {}),
    _tool(
        "save_memory",
        "「覚えておいて」と言われた事実をそのまま長期記憶に保存する",
        {"fact": _s("保存する事実")},
        ["fact"],
    ),
    _tool(
        "recall_memory",
        "長期記憶を検索する。過去の出来事・約束・贈り物などを聞かれた時に使う",
        {"query": _s("検索クエリ")},
        ["query"],
    ),
    _tool(
        "history_search",
        "会話履歴の発言原文を文字列検索する。「あの話何て言ったっけ」"
        "固有名詞・正確な文言を掘り起こすときに使う（意味での曖昧検索は recall_memory）。"
        "bot経由の会話では既定でそのbotの履歴だけを見る",
        {
            "query": _s("検索語（発言原文の一部）"),
            "profile": _s("bot profile名。'all'で全会話。省略時はこの会話のprofile"),
            "limit": _i("最大件数（既定10、上限50）"),
        },
        ["query"],
    ),
    _tool(
        "update_persona_notes",
        "自分自身についてのメモ（口癖・好み・マスターとの関係で気づいたこと等）を更新する。自分の人格メモに追記する",
        {"note": _s("残しておきたい自分についての気づき・変化（簡潔に）")},
        ["note"],
    ),
    _tool(
        "doc_ingest",
        "ローカルのPDFファイルを読めるように索引化してライブラリに登録する。ファイルパスかdata/docs内のファイル名を渡す",
        {
            "path": _s("PDFファイルのパスまたはファイル名"),
            "source": _s("入手元メモ（例: discord）。任意"),
        },
        ["path"],
    ),
    _tool(
        "doc_ask",
        "取り込み済みの文書（PDF）に質問して内容を調べる。docには文書名の一部を指定",
        {
            "query": _s("文書への質問"),
            "doc": _s("文書名の全部または一部。省略時は登録が1件なら自動選択"),
        },
        ["query"],
    ),
    _tool("doc_list", "取り込み済み文書の一覧を取得する", {}),
    _tool(
        "doc_toc",
        "文書の目次・構成（索引ツリー）を取得する",
        {"doc": _s("文書名の全部または一部")},
        ["doc"],
    ),
    _tool(
        "doc_forget",
        "文書の索引を削除する。docには文書名の一部を指定",
        {"doc": _s("文書名の全部または一部")},
        ["doc"],
    ),
    _tool(
        "file_read",
        "ローカルのファイルを行番号付きで読む。「このファイル読んで」で使う。"
        "テキストはそのまま、docx/xlsx/ipynb/db/sqliteは自動でテキスト化、"
        "PDFはテキスト層を抽出する。大きいファイルはoffsetで続きを読む。"
        "秘密系ファイル（.env等）と画像は読めない",
        {
            "path": _s("ファイルパス（絶対パス推奨。~可）"),
            "offset": _i("開始行（1から。続きを読むときは前回表示の続きの行）"),
            "limit": _i("最大行数（既定2000・上限2000）"),
        },
        ["path"],
    ),
    _tool(
        "set_mood",
        "今の自分の気分・状態を記録する。会話の流れで気分が変わった時に使う",
        {
            "mood": _s("気分のラベル（例: 穏やか, 元気, しんみり, 集中）"),
            "valence": _i("気分の明るさ -2(暗い)〜+2(明るい)"),
            "energy": _i("元気度 0-100"),
        },
        ["mood"],
    ),
    _tool(
        "delegate_prepare",
        "コーディング作業をcodexエージェントに依頼する準備をする。依頼文を組み立ててマスターに提示する。"
        "実行にはマスター本人の承認（/ui の承認ボタンまたは「承認 NNNN」発言）が必要で、このツール群からは起動できない。"
        "機能追加・修正・調査などコード作業全般に使う（マスター専用）",
        {
            "request": _s("依頼内容（やってほしいことを具体的に）"),
            "workdir": _s("作業ディレクトリ。省略時はcompanion本体"),
            "model": _s("codexに使わせるモデル。省略時は既定"),
            "effort": _s("codexの思考量 none/minimal/low/medium/high/xhigh"),
        },
        ["request"],
    ),
    _tool(
        "delegate_confirm",
        "delegate_prepareで用意した依頼の状態を確認し、承認方法を案内する。"
        "実際の起動は本人の明示承認（/ui の承認ボタンまたは「承認 NNNN」発言）だけが行う。"
        "マスターが実行したいと言ったときに呼ぶ（マスター専用）",
        {"job_id": _i("依頼ID。省略時は最新の承認待ち")},
    ),
    _tool(
        "delegate_status",
        "codexへの依頼ジョブの状態を確認する（マスター専用）",
        {"job_id": _i("依頼ID。省略時は直近5件")},
    ),
    _tool(
        "delegate_log",
        "実行中/完了した依頼のログ末尾を見る（マスター専用）",
        {"job_id": _i("依頼ID"), "lines": _i("行数（既定40）")},
    ),
    _tool(
        "delegate_cancel",
        "承認待ちの依頼をキャンセル、または実行中の依頼を停止する（マスター専用）",
        {"job_id": _i("依頼ID。省略時は最新")},
    ),
    _tool(
        "model_status",
        "会話に使っているモデルと思考量、利用可能なモデル一覧を表示する",
        {},
    ),
    _tool(
        "model_set",
        "会話モデルと思考量を切り替える。「賢いモデルにして」「もっと考えて」等で使う（マスター専用）",
        {
            "model": _s("モデル名（model_statusで一覧確認）"),
            "effort": _s("思考量 none/minimal/low/medium/high/xhigh"),
        },
        ["model"],
    ),
    _tool("model_reset", "会話モデルをenv既定に戻す（マスター専用）", {}),
    _tool(
        "music_play",
        "音楽を再生する。「好きなアーティストをかけて」「〇〇の曲をシャッフルで」「好みで音楽かけて」で使う。"
        "出力デバイスに応じてCiderまたは公式Apple Musicで再生（マスター専用）",
        {
            "query": _s("曲名/アルバム名/アーティスト名。省略時は好みの記憶から選ぶ"),
            "kind": _s("auto/song/album/artist。アーティスト指定ならアーティストradio等で流す"),
            "backend": _s("auto/cider/apple。省略時は出力デバイスルールで自動選択"),
        },
    ),
    _tool(
        "music_search",
        "曲・アルバム・アーティストを検索して候補一覧を返す。再生前の曖昧解消用（マスター専用）",
        {
            "query": _s("検索語"),
            "kind": _s("song/album/artist。省略時は曲"),
        },
        ["query"],
    ),
    _tool(
        "music_control",
        "再生中の音楽を制御する。「止めて」「次の曲」「シャッフルして」「音量下げて」等（マスター専用）",
        {
            "action": _s("play/pause/playpause/next/previous/stop/shuffle/repeat/autoplay/"
                         "volume/volume_up/volume_down/mute"),
            "volume": _i("action=volume時の音量0-100"),
        },
        ["action"],
    ),
    _tool(
        "music_now",
        "今再生中の曲名・アーティスト・アルバムを返す。「何の曲かけてる？」（マスター専用）",
        {},
    ),
    _tool(
        "music_route",
        "出力デバイス別の再生バックエンドルールを管理する。"
        "「A-50のときは公式アプリで流して」等を覚える（マスター専用）",
        {
            "action": _s("list/add/remove"),
            "device_pattern": _s("出力デバイス名の一部（例: A-50）"),
            "backend": _s("cider または apple"),
        },
        ["action"],
    ),
    _tool(
        "music_taste",
        "音楽の好み（好きなアーティスト・曲・ジャンル）を記憶から引き出す（マスター専用）",
        {},
    ),
    _tool(
        "music_like",
        "今鳴っている曲をライブラリに追加して好みとして記憶する。「この曲いいね」（マスター専用）",
        {},
    ),
    _tool(
        "pc_windows",
        "開いているウィンドウの一覧を取得する。PC操作の第一歩（マスター専用）",
        {},
    ),
    _tool(
        "pc_controls",
        "指定アプリのUIコントロール一覧（ボタン・入力欄等）を取得する。GUI操作の「目」（マスター専用）",
        {"app": _s("ウィンドウタイトルまたはexe名の一部（例: Apple Music, notepad）")},
        ["app"],
    ),
    _tool(
        "pc_click",
        "アプリのコントロールを押す。名前かindexで指定（マスター専用）",
        {
            "app": _s("対象アプリ"),
            "target": _s("コントロール名の一部またはpc_controlsのindex番号"),
        },
        ["app", "target"],
    ),
    _tool(
        "pc_type",
        "アプリの入力欄にテキストを入力する（マスター専用）",
        {
            "app": _s("対象アプリ"),
            "target": _s("入力欄の名前/index。省略時はフォーカス中にキー入力"),
            "text": _s("入力する文字列"),
        },
        ["app", "target", "text"],
    ),
    _tool(
        "pc_key",
        "キーボード入力を送る（Enter, Ctrl+F 等。send_keys記法）（マスター専用）",
        {"keys": _s("send_keys形式（例: {ENTER}, ^f, {VK_SPACE}）")},
        ["keys"],
    ),
    _tool(
        "pc_task",
        "複数ステップのGUI操作を自分で遂行する。UIコントロールを見て次の操作を自分で選んで実行"
        "していく。「Apple Musicで〇〇して」「メモ帳に書いて」等のPC操作依頼に使う（マスター専用）",
        {
            "goal": _s("達成したいことを具体的に"),
            "app": _s("対象アプリのウィンドウ名（起動済みのもの）"),
            "max_steps": _i("最大操作ステップ数（省略時8）"),
        },
        ["goal"],
    ),
    _tool(
        "self_restart",
        "companionプロセスを再起動する。delegate変更の反映や「再起動して」で使う（マスター専用）",
        {},
    ),
    _tool(
        "skill_list",
        "自分のスキル一覧を見る。staged=承認待ち / active=使える / disabled=停止中",
        {"status": _s("絞り込み: staged/active/disabled/rejected。省略時は全部")},
    ),
    _tool(
        "skill_read",
        "スキルの手順書（SKILL.md）と同梱ファイルを読む。「〜のやり方」系の作業や"
        "承認前の中身確認に使う。file 省略で本文＋同梱ファイル一覧、file 指定でその中身",
        {
            "name": _s("スキル名"),
            "file": _s("パッケージ内のファイル相対パス（省略時は SKILL.md 本文）"),
        },
        ["name"],
    ),
    _tool(
        "skill_search",
        "スキルを名前・説明・用途で検索する。索引が名前だけに縮まっているときの詳細取得用",
        {"query": _s("検索語")},
        ["query"],
    ),
    _tool(
        "skill_draft",
        "自分のスキル（手順書・知識パッケージ）を起票して承認待ちの inbox に投函する。"
        "「スキル作って」「このやり方を覚えて」等で使う。起票してもすぐ使えるわけではなく、"
        "検査後にマスターが承認して初めて有効になる。同名スキルがあるときは改訂版として投函",
        {
            "name": _s("スキル名。英字・数字・_・-、64文字以内（例: companion-guide）"),
            "description": _s("スキルの一文説明。索引に出るので用途が分かる文に"),
            "body": _s("SKILL.md の本文（Markdown）。手順・知識・注意点など。"
                       "秘密の値や会話固有の個人情報は書かない"),
            "scopes": _s("用途タグ（カンマ区切り）。任意"),
            "requires_tools": _s("このスキルが前提とする tool 名（カンマ区切り）。任意"),
        },
        ["name", "description", "body"],
    ),
    _tool(
        "skill_approve",
        "承認待ち(staged)のスキルを有効にする。複数名（カンマ区切り）や 'all-staged' で"
        "まとめて承認できる。マスターが承認したときだけ呼ぶ（マスター専用）",
        {"name": _s("スキル名。複数名や 'all-staged' も可")},
        ["name"],
    ),
    _tool(
        "skill_disable",
        "スキルを停止する。有効なスキルを止めたいとき・承認待ちを却下するときに使う（マスター専用）",
        {"name": _s("スキル名")},
        ["name"],
    ),
    _tool(
        "body_status",
        "身体（body側ランタイム）との接続状態を確認する。body系が動かない時の切り分けに（マスター専用）",
        {},
    ),
    _tool(
        "body_task",
        "身体側の実行エージェントに作業を依頼する。browser/terminal等のbody側ツールが要る仕事向け。"
        "依頼はrunを作ってすぐ返り、短い作業は結果まで待って報告し、長い作業は終わったら"
        "身体からの報告として知らせる。sessionで作業ラベルを付けると同ラベルへの追加指示や"
        "並行作業の区別ができる（'ops'は身体側の継続作業セッション）。"
        "action: start=新規依頼（既定）/ check=run_idの状態確認 / list=進行中と直近の一覧 /"
        " steer=run_id+taskで追加指示 / stop=run_idを止める。"
        "backendで実行する身体を選ぶ（既定hermes。codexはCodex app-server身体）（マスター専用）",
        {
            "task": _s("依頼内容（steer時は追加指示）"),
            "session": _s("作業ラベル。同じラベルの進行中runには新規を作らず追加指示になる"),
            "run_id": _s("check/steer/stop対象のrun ID"),
            "action": _s("start/check/list/steer/stop（省略時=start。run_idのみの指定はcheck相当）"),
            "backend": _s("hermes/codex（省略時=hermes）"),
        },
    ),
    _tool(
        "body_schedule",
        "身体側の定期ジョブ（cron）を管理する。毎朝ニュースを拾う等の定常作業向け（マスター専用）",
        {
            "action": _s("list/add/remove/pause/resume/run"),
            "name": _s("add時のジョブ名"),
            "schedule": _s("add時のスケジュール（'every 1d'・'0 9 * * *'・'30m'等）"),
            "prompt": _s("add時に身体へ実行させる内容"),
            "deliver": _s("配送先。discord=ホームch・discord:<chID>=指定ch・local=配送なし（既定discord）"),
            "repeat": _i("add時の繰り返し回数。省略で無期限"),
            "job_id": _s("remove/pause/resume/run対象のジョブID"),
        },
        ["action"],
    ),
]

HANDLERS = {
    "tasks_add": lambda a: tasks.tasks_add(a.get("title", ""), a.get("due"), a.get("note")),
    "tasks_list": lambda a: tasks.tasks_list(bool(a.get("include_done"))),
    "tasks_complete": lambda a: tasks.tasks_complete(a.get("task_id"), a.get("query")),
    "tasks_delete": lambda a: tasks.tasks_delete(a.get("task_id"), a.get("query")),
    "list_add": lambda a: lists.list_add(a.get("list_name", "メモ"), a.get("item", ""), a.get("note")),
    "list_show": lambda a: lists.list_show(a.get("list_name", "")),
    "list_remove": lambda a: lists.list_remove(a.get("query"), a.get("item_id")),
    "list_names": lambda a: lists.list_names(),
    "remind_at": reminders.remind_handler,
    "reminders_list": lambda a: reminders.reminders_list(),
    "reminder_cancel": lambda a: reminders.reminder_cancel(a.get("reminder_id"), a.get("query")),
    "media_log": lambda a: media.media_log(
        a.get("title", ""), a.get("kind", "作品"), a.get("note", ""), a.get("rating")
    ),
    "media_list": lambda a: media.media_list(),
    "save_memory": lambda a: memory_ops.mem_save(a.get("fact", "")),
    "recall_memory": lambda a: memory_ops.mem_search(a.get("query", "")),
    "history_search": lambda a: history.history_search(
        a.get("query", ""), a.get("profile"), a.get("limit")
    ),
    "update_persona_notes": lambda a: inner.update_persona_notes(a.get("note", "")),
    "doc_ingest": lambda a: docs.doc_ingest(a.get("path", ""), a.get("source", "")),
    "doc_ask": lambda a: docs.doc_ask(a.get("query", ""), a.get("doc")),
    "doc_list": lambda a: docs.doc_list(),
    "doc_toc": lambda a: docs.doc_toc(a.get("doc", "")),
    "doc_forget": lambda a: docs.doc_forget(a.get("doc", "")),
    "file_read": lambda a: files.file_read(
        a.get("path", ""), a.get("offset"), a.get("limit")
    ),
    "set_mood": lambda a: inner.set_mood(a.get("mood", ""), a.get("valence"), a.get("energy")),
    "delegate_prepare": lambda a: delegate.delegate_prepare(
        a.get("request", ""), a.get("workdir"), a.get("model"), a.get("effort")
    ),
    "delegate_confirm": lambda a: delegate.delegate_confirm(a.get("job_id")),
    "delegate_status": lambda a: delegate.delegate_status(a.get("job_id")),
    "delegate_log": lambda a: delegate.delegate_log(a.get("job_id"), a.get("lines") or 40),
    "delegate_cancel": lambda a: delegate.delegate_cancel(a.get("job_id")),
    "model_status": lambda a: modelctl.model_status(),
    "model_set": lambda a: modelctl.model_set(a.get("model", ""), a.get("effort")),
    "model_reset": lambda a: modelctl.model_reset(),
    "music_play": lambda a: music.music_play(
        a.get("query", ""), a.get("kind", "auto"), a.get("backend")
    ),
    "music_search": lambda a: music.music_search(a.get("query", ""), a.get("kind", "auto")),
    "music_control": lambda a: music.music_control(a.get("action", ""), a.get("volume")),
    "music_now": lambda a: music.music_now(),
    "music_route": lambda a: music.music_route(
        a.get("action", "list"), a.get("device_pattern"), a.get("backend")
    ),
    "music_taste": lambda a: music.music_taste(),
    "music_like": lambda a: music.music_like(),
    "pc_windows": lambda a: pc.pc_windows(),
    "pc_controls": lambda a: pc.pc_controls(a.get("app", "")),
    "pc_click": lambda a: pc.pc_click(a.get("app", ""), a.get("target", "")),
    "pc_type": lambda a: pc.pc_type(a.get("app", ""), a.get("target", ""), a.get("text", "")),
    "pc_key": lambda a: pc.pc_key(a.get("keys", "")),
    "pc_task": lambda a: pc.pc_task(
        a.get("goal", ""), a.get("app", ""), a.get("max_steps")
    ),
    "self_restart": lambda a: selfops.self_restart(),
    "skill_list": lambda a: skills.skill_list(a.get("status")),
    "skill_read": lambda a: skills.skill_read(a.get("name", ""), a.get("file", "")),
    "skill_search": lambda a: skills.skill_search(a.get("query", "")),
    "skill_draft": lambda a: skills.skill_draft(
        a.get("name", ""), a.get("description", ""), a.get("body", ""),
        a.get("scopes", ""), a.get("requires_tools", ""),
    ),
    "skill_approve": lambda a: skills.skill_approve(a.get("name", "")),
    "skill_disable": lambda a: skills.skill_disable(a.get("name", "")),
    "body_status": lambda a: body.body_status(),
    "body_task": lambda a: body.body_task(
        a.get("task", ""), a.get("session", ""), a.get("run_id", ""),
        a.get("action", ""), a.get("backend", ""),
    ),
    "body_schedule": lambda a: body.body_schedule(
        a.get("action", "list"), a.get("name", ""), a.get("schedule", ""),
        a.get("prompt", ""), a.get("deliver", ""), a.get("repeat"),
        a.get("job_id", ""),
    ),
}


# --- ツールごとの実行情報（スキーマ・handlerとは別の運用メタ） ---
# kind: read=参照のみ / write=状態や保存を変更 / exec=外部プロセスやGUIを動かす
# timeout: asyncio.wait_for の秒数。None=制限なし（長い操作の既定）
_TOOL_META_DEFAULT = {"owner": True, "timeout": None, "kind": "write"}

TOOL_META: dict[str, dict] = {
    # 参照系
    "tasks_list": {"kind": "read"},
    "list_show": {"kind": "read"},
    "list_names": {"kind": "read"},
    "reminders_list": {"kind": "read"},
    "media_list": {"kind": "read"},
    "recall_memory": {"kind": "read"},
    "history_search": {"kind": "read", "timeout": 15},
    "doc_ask": {"kind": "read"},
    "doc_list": {"kind": "read"},
    "doc_toc": {"kind": "read"},
    "file_read": {"kind": "read", "timeout": 15},
    "delegate_status": {"kind": "read", "timeout": 15},
    "delegate_log": {"kind": "read", "timeout": 15},
    "model_status": {"kind": "read"},
    "music_search": {"kind": "read", "timeout": 20},
    "music_now": {"kind": "read", "timeout": 15},
    "music_taste": {"kind": "read"},
    "pc_windows": {"kind": "read", "timeout": 15},
    "pc_controls": {"kind": "read", "timeout": 15},
    "skill_list": {"kind": "read"},
    "skill_read": {"kind": "read"},
    "skill_search": {"kind": "read"},
    # 外部動作系（外部プロセス・GUIを動かす）
    "delegate_prepare": {"kind": "exec"},
    "delegate_confirm": {"kind": "exec"},
    "delegate_cancel": {"kind": "exec"},
    "pc_click": {"kind": "exec", "timeout": 60},
    "pc_type": {"kind": "exec", "timeout": 60},
    "pc_key": {"kind": "exec", "timeout": 30},
    # UIA/pycaw 等のCOM呼出しは相手側の無応答で返らないことがある
    # （2026-10-12 music_control→pc._find_window が UIA 全走査で詰まり
    # turn が宙に浮いた）。GUI系は必ず timeout を持たせる
    "music_play": {"kind": "exec", "timeout": 60},
    "music_control": {"kind": "exec", "timeout": 30},
    "music_like": {"kind": "exec", "timeout": 20},
    "music_route": {"kind": "exec", "timeout": 15},
    "pc_task": {"kind": "exec"},   # 複数ステップの自律ループ。上限は max_steps 側
    "self_restart": {"kind": "exec", "timeout": 15},
    "body_status": {"kind": "read", "timeout": 20},
    "body_task": {"kind": "exec", "timeout": 60},  # startは猶予内しか待たず長時間runはwatcherが追う
    "body_schedule": {"kind": "exec", "timeout": 30},
    # 残りは state/保存を変更する write 既定値
}

# 登録漏れ検査: TOOLSスキーマとHANDLERSは必ず1:1でなければならない
_SCHEMA_NAMES = {t["function"]["name"] for t in TOOLS}
_missing_handler = _SCHEMA_NAMES - set(HANDLERS)
_extra_handler = set(HANDLERS) - _SCHEMA_NAMES
if _missing_handler or _extra_handler:
    raise RuntimeError(
        f"tool schema/handler mismatch: missing={_missing_handler} extra={_extra_handler}"
    )


@dataclasses.dataclass
class ToolResult:
    """ツール実行結果。status: ok / error / pending_approval / unknown。

    ハンドラが ToolResult を返せばそのまま伝え、str 返しは ok として扱う
    （既存ツールはすべてstr互換のまま動く）。
    """

    status: str
    text: str

    @classmethod
    def of(cls, value) -> "ToolResult":
        if isinstance(value, cls):
            return value
        return cls("ok", "" if value is None else str(value))


async def dispatch_tool_result(name: str, args: dict) -> ToolResult:
    """結果の状態を保持するディスパッチ。engine以外の呼び出し元はこちらを使う"""
    if not is_owner():
        return ToolResult("error", _DENY)
    handler = HANDLERS.get(name)
    if not handler:
        return ToolResult("unknown", f"unknown tool: {name}")
    meta = {**_TOOL_META_DEFAULT, **TOOL_META.get(name, {})}
    timeout = meta.get("timeout")
    try:
        call = asyncio.to_thread(handler, args)
        value = await asyncio.wait_for(call, timeout) if timeout else await call
        return ToolResult.of(value)
    except asyncio.TimeoutError:
        log.warning("tool %s timed out after %ss", name, timeout)
        return ToolResult("error", f"ツール {name} が時間内に応答しませんでした")
    except Exception:
        log.exception("tool %s failed", name)
        return ToolResult("error", f"ツール {name} でエラーが発生しました")


async def dispatch_tool(name: str, args: dict) -> str:
    """従来の文字列返却インターフェース（engineのtool message用）"""
    return (await dispatch_tool_result(name, args)).text
