"""kernel manifest pin の更新・検査ユーティリティ。

kernel ファイルを正しく変更したら `write` で manifest.json を再生成する。
`check` は verify_manifest と同じ照合を行う（起動 sanity・CI 相当検査で使う）。

  python scripts/kernel_manifest.py write
  python scripts/kernel_manifest.py check
"""
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# companion/__init__.py（config→data/ 副作用）を経由せず kernel だけ import する
_pkg = types.ModuleType("companion")
_pkg.__path__ = [str(ROOT / "companion")]
sys.modules.setdefault("companion", _pkg)

from companion.kernel import writer  # noqa: E402


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "write":
        manifest = writer.compute_manifest()
        out = writer.MANIFEST_PATH
        out.write_text(json.dumps(manifest, ensure_ascii=False,
                                  indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"wrote {out} ({len(manifest['files'])} files)")
    elif cmd == "check":
        res = writer.verify_manifest()
        print(("OK" if res["ok"] else "FAIL") + f": {res['detail']}")
        sys.exit(0 if res["ok"] else 1)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
