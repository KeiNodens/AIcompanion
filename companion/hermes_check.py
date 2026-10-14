"""Hermes config.yaml の設定 lint（runtime-hermes-tooling.md §8-1）。

companion をモデル provider として使う Hermes 側の設定事故を点検する。
起動時に runtime から1回走るほか、body_status が要約を出し、
`python -m companion.hermes_check` で一覧表示もできる。

秘密値（api_key・extra_headers の鍵など）は finding の文言に絶対に含めない。
"""
import ast
import dataclasses
import hmac
import logging
import os
import sys
from pathlib import Path

from . import body_plugin, config, hermes_conf

log = logging.getLogger("companion")


@dataclasses.dataclass
class Finding:
    level: str   # "error" | "warn" | "info"
    key: str     # 対象の config キー（ドット区切り）
    message: str


# config_defaults.py が読めないときの aux タスク名フォールバック
# （DEFAULT_CONFIG["auxiliary"] の dict 値キー。Hermes の更新で増える）
_AUX_FALLBACK = {
    "vision", "compression", "skills_hub", "approval", "review", "mcp",
    "title_generation", "memory_query_rewrite", "tts_audio_tags",
    "triage_specifier", "kanban_decomposer", "profile_describer",
    "goal_judge", "curator", "monitor", "background_review",
    "moa_reference", "moa_aggregator",
}


def aux_task_names(src: Path) -> set[str]:
    """src/hermes_cli/config_defaults.py を ast で読み、DEFAULT_CONFIG の
    "auxiliary" 直下で「値が Dict か Call」のキー名だけ拾う
    （transient_retries 等のスカラー設定はタスクではないので除外）。
    読み取り・パースに失敗したらフォールバック集合を返す。"""
    path = src / "hermes_cli" / "config_defaults.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return set(_AUX_FALLBACK)
    default_cfg = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIG"
            for t in node.targets
        ):
            default_cfg = node.value
            break
        # DEFAULT_CONFIG = {...} を AnnAssign で書かれる可能性も一応見る
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "DEFAULT_CONFIG"
        ):
            default_cfg = node.value
            break
    if not isinstance(default_cfg, ast.Dict):
        return set(_AUX_FALLBACK)
    for k, v in zip(default_cfg.keys, default_cfg.values):
        if (
            isinstance(k, ast.Constant)
            and k.value == "auxiliary"
            and isinstance(v, ast.Dict)
        ):
            names = {
                kk.value
                for kk, vv in zip(v.keys, v.values)
                if isinstance(kk, ast.Constant)
                and isinstance(kk.value, str)
                and isinstance(vv, (ast.Dict, ast.Call))
            }
            return names or set(_AUX_FALLBACK)
    return set(_AUX_FALLBACK)


def _aux_unpinned(entry) -> bool:
    """aux entry がメインモデル継承（=companion に流れる）かどうか"""
    if not isinstance(entry, dict):
        return True
    provider = str(entry.get("provider") or "").strip().lower()
    base = str(entry.get("base_url") or "").strip()
    if provider in ("", "auto", "main"):
        return True
    # custom は base_url が無いと解決先が不定＝実質未pin扱い
    if provider == "custom" and not base:
        return True
    return False


def check(cfg: dict | None = None, src: Path | None = None) -> list[Finding]:
    """設定を点検して Finding の一覧を返す。cfg 省略時は実 config.yaml。
    src は Hermes ソースのルート（aux タスク一覧の抽出用）"""
    cfg = hermes_conf.load_config() if cfg is None else (cfg or {})
    src = src or config.HERMES_SRC_DIR
    out: list[Finding] = []

    def add(level, key, message):
        out.append(Finding(level, key, message))

    model = cfg.get("model") or {}
    main_to_companion = hermes_conf.targets_companion(model.get("base_url"))

    # 1. companion を向くメインモデルは chat_completions 必須
    if main_to_companion and model.get("api_mode") != "chat_completions":
        add(
            "error", "model.api_mode",
            "model.base_url が companion を向いているのに api_mode が "
            f"{model.get('api_mode') or '(未設定)'}（chat_completions 以外だと "
            "/v1/responses を叩いて 404 になる）",
        )

    # 2. メインが companion のとき、裏方タスクは別モデルに pin されていること
    if main_to_companion:
        aux = cfg.get("auxiliary") or {}
        tasks = aux_task_names(src) | {
            k for k, v in aux.items() if isinstance(v, dict)
        }
        for task in sorted(tasks):
            entry = aux.get(task)
            if isinstance(entry, dict) and hermes_conf.targets_companion(
                entry.get("base_url")
            ):
                add(
                    "error", f"auxiliary.{task}",
                    f"auxiliary.{task} の base_url が companion を向いている"
                    "（裏方呼出しが会話窓に逆流する）",
                )
            elif _aux_unpinned(entry):
                add(
                    "warn", f"auxiliary.{task}",
                    f"auxiliary.{task} はメインモデル(companion)を継承する"
                    "（provider/base_url を別モデルに pin する）",
                )

    # 3. delegation が companion を向かないこと
    delegation = cfg.get("delegation") or {}
    if isinstance(delegation, dict):
        if hermes_conf.targets_companion(delegation.get("base_url")):
            add(
                "warn", "delegation.base_url",
                "delegation.base_url が companion を向いている"
                "（サブエージェントが会話窓を叩く）",
            )
        elif main_to_companion and not any(
            str(delegation.get(k) or "").strip()
            for k in ("provider", "model", "base_url")
        ):
            add(
                "warn", "delegation",
                "delegation が未pinなのでメインモデル(companion)を継承する",
            )

    # 4-5. api_server（body 側 REST 制御面）の鍵と model_routes
    platforms = cfg.get("platforms") or {}
    api_server = platforms.get("api_server") or {}
    if isinstance(api_server, dict) and api_server.get("enabled"):
        if not hermes_conf.api_server_key()[0]:
            add(
                "error", "platforms.api_server.extra.key",
                "api_server が有効だが API 鍵が見つからない"
                "（body 系ツールが使えない）",
            )
        # companion 側 env の鍵と Hermes config の鍵が両方あって不一致だと
        # api_server_key() は env を優先するので Hermes 側が拒否して 401 になる
        env_key = str(config.HERMES_API_KEY or "")
        cfg_key = hermes_conf.resolve(
            (api_server.get("extra") or {}).get("key") or ""
        )
        if env_key and cfg_key and not hmac.compare_digest(
            env_key.encode(), cfg_key.encode()
        ):
            add(
                "error", "platforms.api_server.extra.key",
                "companion の HERMES_API_KEY と Hermes の api_server 鍵が"
                "食い違っている（401 になる）",
            )
        routes = (api_server.get("extra") or {}).get("model_routes") or {}
        if config.BODY_RUN_MODEL not in routes:
            add(
                "warn", "platforms.api_server.extra.model_routes",
                f"model_routes に '{config.BODY_RUN_MODEL}'（BODY_RUN_MODEL）の"
                "ルートが無い（body_task が既定モデルへ流れる）",
            )

    # 6. companion を向く provider は session_affinity_header が要る
    providers = cfg.get("providers") or {}
    if isinstance(providers, dict):
        for name, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            if not hermes_conf.targets_companion(entry.get("base_url")):
                continue
            header = str(entry.get("session_affinity_header") or "").lower()
            if header != "x-companion-session":
                add(
                    "warn", f"providers.{name}.session_affinity_header",
                    f"providers.{name} は companion を向くが "
                    "session_affinity_header: x-companion-session が無い"
                    "（入口ごとのセッションがまとまらない）",
                )

    # 7. :8788 入口鍵の有無（情報として。認証は COMPANION_API_AUTH 側の設定）
    if not hermes_conf.companion_key()[0]:
        add(
            "info", "companion_key",
            ":8788 の入口鍵が未設定（COMPANION_API_KEY か Hermes config の "
            "providers.<name>.extra_headers.x-companion-key で設定できる）",
        )

    # 8. 技能面の dir 往復（runtime-hermes-tooling.md §7.1-7.2）:
    #    create_dir が companion の skills-inbox を、external_dirs が
    #    skills-export を向いていないと双方向が片方向に欠ける
    def _norm(entry) -> str:
        p = Path(hermes_conf._expand(str(entry))).expanduser()
        if not p.is_absolute():
            p = config.HERMES_HOME / p
        return os.path.normcase(str(p.resolve()))

    skills_cfg = cfg.get("skills") or {}
    if isinstance(skills_cfg, dict):
        create_dir = skills_cfg.get("create_dir")
        if not create_dir or (
            _norm(create_dir)
            != os.path.normcase(str(config.SKILLS_INBOX_DIR.resolve()))
        ):
            add(
                "warn", "skills.create_dir",
                "skills.create_dir が companion の skills-inbox を向いていない"
                "（Curator 生成物が staged import に乗らない）",
            )
        if config.SKILLS_EXPORT:
            ext = skills_cfg.get("external_dirs")
            if isinstance(ext, str):
                ext = [ext]
            want = os.path.normcase(str(config.SKILLS_EXPORT_DIR.resolve()))
            if want not in {_norm(e) for e in (ext or [])}:
                add(
                    "warn", "skills.external_dirs",
                    "skills.external_dirs に data/skills-export が登録されて"
                    "いない（§7.2: active 技能が身体側の技能一覧に出ない）",
                )

    # 9.5 profile_policy.yaml（案A）。companion 側ファイルだが
    #     Hermes profile 名との整合はここで見る
    try:
        from . import profile_check, profile_policy

        policy_path = profile_policy.POLICY_PATH
        policy = profile_policy.load()
        if policy_path.exists() and not policy:
            add(
                "warn", "profile_policy.yaml",
                "profile_policy.yaml が読めない/空（全 profile が"
                "環境既定に fallback している）",
            )
        for key in profile_check._policy_unknown_keys():
            add(
                "warn", f"profile_policy.yaml:{key}",
                "profile_policy.yaml に未定義のキー（typo の可能性。"
                "有効なのは bridge_tools / bridge_dm_tools / skills）",
            )
        known = profile_check._known_profile_names()
        profs = policy.get("profiles") if isinstance(policy, dict) else {}
        if isinstance(profs, dict):
            for name in profs:
                if name not in known:
                    add(
                        "warn", f"profile_policy.yaml:profiles.{name}",
                        f"profile '{name}' は profiles/ にも default にも"
                        "無い（ポリシーがどの呼出しにも効かない）",
                    )
    except Exception:
        log.exception("profile_policy lint failed")

    # 9. approval-audit plugin（runtime-hermes-tooling-impl.md R5b）。
    #    橋経由の Hermes 承認を Core の監査写しにする observer。
    #    HERMES_HOME/plugins/ への導入と plugins.enabled 登録が要る
    if config.BODY_BRIDGE:
        st = body_plugin.status(cfg)
        if not st["installed"]:
            add(
                "warn", "plugins.enabled",
                f"{body_plugin.PLUGIN_NAME} が未導入（橋経由の承認が Core の"
                "監査に残らない。`python -m companion.body_plugin` で導入）",
            )
        elif st["drift"]:
            add(
                "warn", "plugins.enabled",
                f"{body_plugin.PLUGIN_NAME} の写しが出荷版とずれている"
                "（`python -m companion.body_plugin` で更新）",
            )
        elif st["disabled"]:
            add(
                "warn", "plugins.disabled",
                f"{body_plugin.PLUGIN_NAME} が plugins.disabled に入っている",
            )
        elif not st["enabled"]:
            add(
                "warn", "plugins.enabled",
                f"{body_plugin.PLUGIN_NAME} が plugins.enabled に無い"
                "（`hermes plugins enable companion-approval-audit`）",
            )

    return out


def main() -> None:
    """`python -m companion.hermes_check` — findings を一覧表示する"""
    findings = check()
    counts = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        counts[f.level] = counts.get(f.level, 0) + 1
        print(f"[{f.level}] {f.key}: {f.message}")
    if not findings:
        print("findings なし")
    print(
        f"--- error {counts['error']} / warn {counts['warn']} "
        f"/ info {counts['info']}"
    )
    sys.exit(1 if counts["error"] else 0)


if __name__ == "__main__":
    main()
