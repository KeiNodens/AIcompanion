"""kernel.pack —— L-EXP pack 形式の純粋規則（core-api §6・migration-m5 §5.3）

pack = canonical の自己記述的な交換形式（唯一の交換形式・REQ-X1）。
このモジュールは直列化・digest・allowlist・manifest 検査の決定的部分のみ
担う。ファイル I/O は adapters/packio.py 側。

pack ディレクトリ構成:
  manifest.json        format/mode/exported_seq/counts/digests/replay_digest
  events.jsonl         event 行の完全写し（seq 昇順・1行1 canon JSON）
  tables/<t>.jsonl     canonical/registry 各表の行 dict 写し
  blobs/<hash>         permanent blob 実体（存在する場合のみ）
  watermark_hint.json  任意同梱の水位ヒント（operational・SD-37）
"""

from . import canon, schema

FORMAT_VERSION = "pcore.pack.v1"
MODE_FULL = "full"

# export 対象表（TABLE_CLASS から導出。operational は除外 —— SD-37）
CANONICAL_TABLES = tuple(
    t for t, c in schema.TABLE_CLASS.items() if c == "canonical")
LOG_TABLES = tuple(
    t for t, c in schema.TABLE_CLASS.items() if c == "canonical_log")
REGISTRY_TABLES = tuple(
    t for t, c in schema.TABLE_CLASS.items() if c == "registry")
EXPORT_TABLES = CANONICAL_TABLES + LOG_TABLES + REGISTRY_TABLES

TAG_TABLE = "pcore.pack.table.v1"
TAG_MANIFEST = "pcore.pack.manifest.v1"


def canon_line(obj) -> bytes:
    """jsonl の1行（canon 直列化＋改行）。byte 同一性は canon が担保。"""
    return canon.dumps_bytes(obj) + b"\n"


def parse_line(line: bytes):
    return canon.loads(line)


def table_digest(table: str, rows) -> str:
    """表の内容 digest（行順序込み・export は宣言順で吐くので順序も契約）。"""
    return canon.hash_obj(TAG_TABLE, [table, list(rows)])


def manifest_digest(manifest: dict) -> str:
    """manifest の pack 完全性 digest（pack_digest フィールド自身を除く）。"""
    m = {k: v for k, v in manifest.items() if k != "pack_digest"}
    return canon.hash_obj(TAG_MANIFEST, m)


def build_manifest(*, exported_seq: int, exported_at: str,
                   counts: dict, digests: dict, replay_digest: str,
                   blob_files: list, watermark_hint: bool = False) -> dict:
    m = {
        "format": FORMAT_VERSION,
        "mode": MODE_FULL,
        "exported_seq": exported_seq,
        "exported_at": exported_at,
        "serializer": canon.SERIALIZER_VERSION,
        "counts": dict(counts),
        "digests": dict(digests),
        "replay_digest": replay_digest,
        "blob_files": list(blob_files),
        "watermark_hint": bool(watermark_hint),
    }
    m["pack_digest"] = manifest_digest(m)
    return m


def verify_manifest(manifest: dict) -> list:
    """manifest 自身の構造的検査。違反文字列のリスト（空=合格）。
    ファイル実在性・中身との照合は adapter 側（I/O）。"""
    errs = []
    if manifest.get("format") != FORMAT_VERSION:
        errs.append(f"format mismatch: {manifest.get('format')}")
    if manifest.get("mode") != MODE_FULL:
        errs.append(f"unsupported mode: {manifest.get('mode')}")
    if not isinstance(manifest.get("exported_seq"), int) \
            or manifest["exported_seq"] < 0:
        errs.append("exported_seq must be non-negative int")
    if manifest.get("pack_digest") != manifest_digest(manifest):
        errs.append("pack_digest mismatch")
    return errs
