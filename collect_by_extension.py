import hashlib
import json
import logging
import os
from pathlib import Path

# 収集対象の拡張子をここで指定します（先頭のドットを含む小文字）。
INPUT_DIR = Path("./assets")
EXTENSIONS = frozenset({".png", ".jpg", ".hca", ".plist", ".ExportJson", ".usm"})
OUTPUT_DIR = Path("./out/assets")
OUTPUT_FILENAME = "assets_dict.json"
HASH_READ_SIZE = 1024 * 1024
JSON_INDENT = 2


def calculate_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as source:
        for chunk in iter(lambda: source.read(HASH_READ_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_files(root: Path, output_dir: Path) -> dict[str, dict[str, list[Path]]]:
    collected: dict[str, dict[str, list[Path]]] = {
        extension: {} for extension in EXTENSIONS
    }
    # 入力の拡張子は大文字・小文字を区別せず照合し、設定された表記を維持します。
    extension_lookup = {extension.lower(): extension for extension in EXTENSIONS}

    def on_walk_error(error: OSError) -> None:
        logging.warning("ディレクトリを探索できません: %s", error)

    for current_dir, dir_names, file_names in os.walk(
        root, topdown=True, followlinks=False, onerror=on_walk_error
    ):
        current_path = Path(current_dir)

        # 出力ディレクトリを再探索せず、ディレクトリのシンボリックリンクも除外します。
        dir_names[:] = sorted(
            name
            for name in dir_names
            if not (current_path / name).is_symlink()
            and (current_path / name).resolve() != output_dir
        )

        for filename in sorted(file_names):
            file_path = current_path / filename
            if file_path.is_symlink() or not file_path.is_file():
                continue

            extension = extension_lookup.get(file_path.suffix.lower())
            if extension is not None:
                collected[extension].setdefault(filename, []).append(
                    file_path.resolve()
                )

    return collected


def make_json_records(
    files_by_name: dict[str, list[Path]], root: Path
) -> dict[str, dict]:
    records: dict[str, dict] = {}

    for filename, paths in sorted(files_by_name.items()):
        if len(paths) == 1:
            records[filename] = {
                "path": paths[0].relative_to(root).as_posix(),
                "paths": {},
            }
            continue

        hashes: dict[str, list[str]] = {}
        for path in paths:
            try:
                file_hash = calculate_sha256(path)
            except OSError as error:
                logging.warning("ハッシュ計算に失敗: %s (%s)", path, error)
                continue

            relative_path = path.relative_to(root).as_posix()
            hashes.setdefault(file_hash, []).append(relative_path)

        records[filename] = {"path": "", "paths": hashes}

    return records


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    root = INPUT_DIR.resolve()
    output_dir = OUTPUT_DIR.resolve()

    if not root.is_dir():
        raise SystemExit(f"探索対象ディレクトリが存在しません: {root}")
    if root == output_dir:
        raise SystemExit("探索対象と出力先に同じディレクトリは指定できません")

    collected = collect_files(root, output_dir)

    for extension in sorted(EXTENSIONS):
        records = make_json_records(collected[extension], root)
        target = output_dir / extension.lstrip(".") / OUTPUT_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)

        with target.open("w", encoding="utf-8") as output:
            json.dump(records, output, ensure_ascii=False, indent=JSON_INDENT)
            output.write("\n")

        print(f"出力: {target} ({len(records)}ファイル名)")


if __name__ == "__main__":
    main()
