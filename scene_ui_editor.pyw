#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""シーンUIエディター - 1ファイル版 (Windows: .pyw ダブルクリック起動)。
Cocos Studio の armature_data と widgetTree/actionlist 両形式に対応。
アセットは本ファイルの隣の ./assets 配下のみから読み込み、JSON参照もツール基準。
シーンJSONは本ファイルの隣の ./out/scene/<シーン名>.json に固定保存。
BGM / SE の試聴と、プレビュー音量の独立調整（JSONには非保存）・アニメーション開始時のSE連動に対応。
PNG/JPGの範囲選択(正規化UV)・再編集・JSONへの保存/復元に対応。
編集画面では端・中心へのスナップと等間隔スナップ（独立切替）、アセットの編集時のみ非表示に対応。
PNG/JPGのUVボタンに加え、plistの2スプライトからトグルボタンを配置可能。JSONにも保存。
HCAは同じフォルダー内の vgmstream-win64/vgmstream-cli.exe でWAVに変換。

Python 3.9+ / tkinter / Pillow。初回起動で Pillow がなければ
ユーザーの同意を得て pip によるインストールを案内します。
添付された plist_texture_viewer_v26_single(1).py の解析・描画部分と
既存の scene_ui_editor.py を統合。追加の .py ファイルは不要です。

保存するJSONから参照されるPNG/plist/ExportJson等のゲーム素材は
本ファイルと同階層の assets フォルダー内に格納してください。
"""
from __future__ import annotations

import sys


def _check_pillow_dependency() -> None:
    """Windows の .pyw 起動でも依存ライブラリ不足を可視化する。"""
    try:
        import PIL.Image
        import PIL.ImageTk
        return
    except ImportError:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        window = tk.Tk()
        window.withdraw()
    except Exception as exc:
        raise SystemExit(f"Pillow および tkinter が必要です: {exc}") from exc
    try:
        approved = messagebox.askyesno(
            "シーン UI エディター - 初回セットアップ",
            "画像処理ライブラリ Pillow が見つかりません。\n"
            "このツールを起動するには Pillow が必要です。\n\n"
            "インターネットを使用して Pillow をインストールしますか？\n"
            "（pip によるインストールを実行します）",
            parent=window,
        )
        if not approved:
            raise SystemExit(1)
        import subprocess
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "Pillow"],
                capture_output=True, text=True, timeout=180, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror("シーン UI エディター", f"Pillow のインストールに失敗しました。\n{exc}", parent=window)
            raise SystemExit(1) from exc
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "詳細不明")[-1800:]
            messagebox.showerror("シーン UI エディター", "Pillow のインストールに失敗しました。\n\n" + details, parent=window)
            raise SystemExit(1)
        import importlib
        importlib.invalidate_caches()
        try:
            import PIL.Image
            import PIL.ImageTk
        except ImportError as exc:
            messagebox.showerror("シーン UI エディター", "Pillow を読み込めませんでした。Python の環境を確認してください。", parent=window)
            raise SystemExit(1) from exc
        messagebox.showinfo("シーン UI エディター", "Pillow をインストールしました。エディターを起動します。", parent=window)
    finally:
        window.destroy()


if __name__ == "__main__":
    _check_pillow_dependency()


# ==== 添付ビューアーの plist / Cocos Studio ExportJson 解析処理 ====

import argparse
import base64
import io
import zlib
import math
import plistlib
import re
import sys
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Mapping, Optional, Sequence

from PIL import Image, ImageTk, UnidentifiedImageError


def wheel_steps(event: Any) -> int:
    """Normalize Windows/macOS MouseWheel and X11 Button-4/5 to one tick."""
    number = getattr(event, "num", None)
    if number == 4:
        return 1
    if number == 5:
        return -1
    delta = getattr(event, "delta", 0)
    if not delta:
        return 0
    # Windows sends ±120 per notch. Some touchpads may send finer deltas.
    return (1 if delta > 0 else -1) * max(1, round(abs(delta) / 120))


APP_TITLE = "PLIST / ExportJson テクスチャビューアー"
WINDOW_SIZE = "1280x820"
MIN_ZOOM = 0.05
MAX_ZOOM = 8.0
MAX_DISPLAY_SIDE = 8192
MAX_TEXTURE_PIXELS = 40_000_000
MAX_FRAMES = 100_000
MAX_NESTING = 3
RECT_KEYS = ("textureRect", "frame", "rect", "frameRect")
PREVIEW_SIDE = 320
ZOOM_STEP = 1.25
HIT_TOLERANCE = 0.0
OVERLAY_COLOR = "#00d8ff"
SELECTED_COLOR = "#ffb000"
TRANSPARENT = (0, 0, 0, 0)
NUMBER_PATTERN = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")


def _pillow_constant(group: str, name: str, image_module: Any = Image) -> Any:
    """Use Pillow 9.1+ enums, with fallback for previous versions."""
    enum_group = getattr(image_module, group, None)
    return getattr(enum_group if enum_group is not None else image_module, name)


RESAMPLE_NEAREST = _pillow_constant("Resampling", "NEAREST")
TRANSPOSE_ROTATE_90 = _pillow_constant("Transpose", "ROTATE_90")
TRANSPOSE_ROTATE_270 = _pillow_constant("Transpose", "ROTATE_270")


@dataclass(frozen=True)
class Frame:
    name: str
    x: int
    y: int
    width: int                # Size after restoring the 90-degree rotation.
    height: int
    source_width: int         # Original, untrimmed sprite size.
    source_height: int
    trim_x: int               # Location in the untrimmed sprite (top-left origin).
    trim_y: int
    rotated: bool

    @property
    def packed_width(self) -> int:
        return self.height if self.rotated else self.width

    @property
    def packed_height(self) -> int:
        return self.width if self.rotated else self.height

    @property
    def packed_box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.packed_width, self.y + self.packed_height


@dataclass(frozen=True)
class Atlas:
    plist_path: Path
    texture_path: Path
    frames: tuple[Frame, ...]
    image: Image.Image
    warnings: tuple[str, ...] = ()


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field}: 数値が必要です")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field}: 数値に変換できません: {value!r}") from exc
    if not number.is_integer() or not (-2**31 <= number < 2**31):
        raise ValueError(f"{field}: 有効な整数ではありません: {value!r}")
    return int(number)


def _as_real(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field}: 数値が必要です")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field}: 数値に変換できません") from exc
    if not math.isfinite(number) or not (-2**31 <= number < 2**31):
        raise ValueError(f"{field}: 数値が有効範囲外です")
    return number


def _numbers(value: Any, expected: int, field: str, allow_float: bool = False) -> tuple[Any, ...]:
    if isinstance(value, str):
        # Only characters used by plist's CGRect/CGPoint representation are allowed.
        if re.sub(NUMBER_PATTERN, "", value).strip(" {}(),\t\r\n"):
            raise ValueError(f"{field}: 座標の形式が不正です: {value!r}")
        parts: Sequence[Any] = NUMBER_PATTERN.findall(value)
    elif isinstance(value, (tuple, list)):
        def flatten(sequence: Sequence[Any]) -> list[Any]:
            result: list[Any] = []
            for item in sequence:
                if isinstance(item, (tuple, list)):
                    result.extend(flatten(item))
                else:
                    result.append(item)
            return result
        parts = flatten(value)
    else:
        raise ValueError(f"{field}: 文字列または配列が必要です")
    if len(parts) != expected:
        raise ValueError(f"{field}: {expected} 個の数値が必要です: {value!r}")
    parse_number = _as_real if allow_float else _as_int
    return tuple(parse_number(item, field) for item in parts)


def _pair(value: Any, field: str) -> tuple[int, int]:
    if isinstance(value, Mapping):
        if "w" in value and "h" in value:
            return _as_int(value["w"], field), _as_int(value["h"], field)
        if "width" in value and "height" in value:
            return _as_int(value["width"], field), _as_int(value["height"], field)
        if "x" in value and "y" in value:
            return _as_int(value["x"], field), _as_int(value["y"], field)
        raise ValueError(f"{field}: 未対応の辞書形式です")
    x, y = _numbers(value, 2, field)
    return x, y


def _offset_pair(value: Any, field: str) -> tuple[float, float]:
    if isinstance(value, Mapping):
        return _as_real(value.get("x"), field), _as_real(value.get("y"), field)
    x, y = _numbers(value, 2, field, allow_float=True)
    return x, y


def _rect(value: Any, field: str) -> tuple[int, int, int, int]:
    if isinstance(value, Mapping):
        if "x" not in value or "y" not in value:
            raise ValueError(f"{field}: x/y がありません")
        width_key = "w" if "w" in value else "width"
        height_key = "h" if "h" in value else "height"
        return (
            _as_int(value["x"], field), _as_int(value["y"], field),
            _as_int(value[width_key], field), _as_int(value[height_key], field),
        )
    x, y, w, h = _numbers(value, 4, field)
    return x, y, w, h


def _bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ValueError(f"{field}: 真偽値が必要です")


def parse_frame(name: str, item: Mapping[str, Any]) -> Frame:
    """Parse common cocos2d/TexturePacker rectangle-based frame variants."""
    rect_key = next((key for key in RECT_KEYS if key in item), None)
    if rect_key is not None:
        x, y, rect_width, rect_height = _rect(item[rect_key], f"{name}.{rect_key}")
    elif all(key in item for key in ("x", "y")) and (
        all(key in item for key in ("w", "h"))
        or all(key in item for key in ("width", "height"))
    ):
        rect_key = "xywh"
        x, y, rect_width, rect_height = _rect(item, f"{name}.xywh")
    elif "position" in item and "size" in item:
        rect_key = "position+size"
        x, y = _pair(item["position"], f"{name}.position")
        rect_width, rect_height = _pair(item["size"], f"{name}.size")
    else:
        raise ValueError(f"{name}: 配置矩形 (textureRect / frame / rect など) がありません")

    rotated = _bool(item.get("textureRotated", item.get("rotated", False)), f"{name}.rotated")
    # frameRect is usually the packed (already rotated) bounding box.
    default_trimmed = (rect_height, rect_width) if rect_key == "frameRect" and rotated else (rect_width, rect_height)
    trimmed_width, trimmed_height = _pair(
        item.get("spriteSize", default_trimmed), f"{name}.spriteSize"
    )
    # Cocos SpriteFrameCache plist format 0 stores the untrimmed size in
    # originalWidth/originalHeight.  Do not use the packed width/height as the
    # source size: doing so loses the transparent margins required for pivot
    # positioning and makes every trimmed component drift away from its bone.
    if "spriteSourceSize" in item:
        source_width, source_height = _pair(item["spriteSourceSize"], f"{name}.spriteSourceSize")
    elif "sourceSize" in item:
        source_width, source_height = _pair(item["sourceSize"], f"{name}.sourceSize")
    elif "originalWidth" in item and "originalHeight" in item:
        # Older Cocos implementations use abs(originalWidth/Height).
        source_width = abs(_as_int(item["originalWidth"], f"{name}.originalWidth"))
        source_height = abs(_as_int(item["originalHeight"], f"{name}.originalHeight"))
    else:
        source_width, source_height = trimmed_width, trimmed_height

    color_rect = next((key for key in ("spriteColorRect", "sourceColorRect", "sourceRect") if key in item), None)
    if color_rect is not None:
        trim_x, trim_y, _, _ = _rect(item[color_rect], f"{name}.{color_rect}")
    elif "cornerOffset" in item:
        trim_x, trim_y = _pair(item["cornerOffset"], f"{name}.cornerOffset")
    else:
        if "spriteOffset" in item or "offset" in item:
            offset_x, offset_y = _offset_pair(
                item.get("spriteOffset", item.get("offset")), f"{name}.offset"
            )
        else:
            # plist format 0 records the center shift in two scalar keys.
            offset_x = _as_real(item.get("offsetX", 0), f"{name}.offsetX")
            offset_y = _as_real(item.get("offsetY", 0), f"{name}.offsetY")
        trim_x = _as_int((source_width - trimmed_width) / 2 + offset_x, f"{name}.trimX")
        # cocos2d offsets use a bottom-left (Y-up) coordinate system.
        trim_y = _as_int((source_height - trimmed_height) / 2 - offset_y, f"{name}.trimY")

    if x < 0 or y < 0 or min(trimmed_width, trimmed_height, source_width, source_height) <= 0:
        raise ValueError(f"{name}: 座標とサイズは正の範囲である必要があります")
    if rect_width <= 0 or rect_height <= 0:
        raise ValueError(f"{name}: textureRect/frame のサイズが不正です")
    if trim_x < 0 or trim_y < 0 or trim_x + trimmed_width > source_width or trim_y + trimmed_height > source_height:
        raise ValueError(f"{name}: トリミング領域が元画像サイズを超えています")
    # cocos2d textureRect/frame are logical sizes. frameRect uses packed sizes.
    expected_rect = (trimmed_height, trimmed_width) if rect_key == "frameRect" and rotated else (trimmed_width, trimmed_height)
    if (rect_width, rect_height) != expected_rect:
        raise ValueError(f"{name}: 配置矩形と spriteSize の寸法が一致しません")
    return Frame(name, x, y, trimmed_width, trimmed_height,
                 source_width, source_height, trim_x, trim_y, rotated)


def _relative_texture_path(plist_path: Path, filename: str) -> Optional[Path]:
    """Restrict untrusted metadata paths to the plist's directory tree."""
    if not filename or "\\" in filename:
        return None
    candidate_name = Path(filename)
    if candidate_name.is_absolute():
        return None
    root = plist_path.parent.resolve()
    candidate = (root / candidate_name).resolve()
    if candidate != root and root in candidate.parents and candidate.is_file():
        return candidate
    return None


def _has_rect(item: Mapping[str, Any]) -> bool:
    """Whether an object describes a sprite's packed rectangle."""
    return (any(key in item for key in RECT_KEYS)
            or ("x" in item and "y" in item and
                (("w" in item and "h" in item) or ("width" in item and "height" in item)))
            or ("position" in item and "size" in item))


def _frame_entries(raw_frames: Any) -> tuple[list[tuple[str, Mapping[str, Any]]], list[str], list[str]]:
    """Flatten optional texture-page groups, leaving unknown entries as warnings.

    Nested groups are nonstandard; detection is deliberately limited to dictionary
    children that themselves have coordinate records. This never invents bounds.
    """
    entries: list[tuple[str, Mapping[str, Any]]] = []
    warnings: list[str] = []
    image_names: list[str] = []

    def visit(mapping: Mapping[str, Any], prefix: str, depth: int) -> None:
        if depth > MAX_NESTING:
            warnings.append(f"{prefix}: ネストが深すぎるため省略しました")
            return
        for key, item in mapping.items():
            if len(entries) >= MAX_FRAMES:
                raise ValueError(f"配置情報が多すぎます (上限: {MAX_FRAMES:,} 件)")
            name = f"{prefix}/{key}" if prefix else str(key)
            if (depth == 0 and isinstance(key, str)
                    and key.lower().endswith((".png", ".webp", ".jpg", ".jpeg"))
                    and isinstance(item, Mapping) and not _has_rect(item)):
                # A group named after an atlas page is a plausible image hint;
                # normal sprite names must not be used to guess the atlas image.
                image_names.append(key)
            if not isinstance(item, Mapping):
                warnings.append(f"{name}: スプライトの辞書データではありません")
                continue
            if _has_rect(item):
                entries.append((name, item))
                continue
            nested: Optional[Mapping[str, Any]] = None
            for field in ("frames", "sprites"):
                candidate = item.get(field)
                if isinstance(candidate, Mapping) and any(isinstance(v, Mapping) for v in candidate.values()):
                    nested = candidate
                    break
            if nested is None:
                direct_children = {k: v for k, v in item.items() if isinstance(v, Mapping) and _has_rect(v)}
                if direct_children:
                    nested = direct_children
            if nested is not None:
                visit(nested, name, depth + 1)
            else:
                warnings.append(f"{name}: 配置矩形 (textureRect / frame / rect など) がありません")

    if isinstance(raw_frames, Mapping):
        visit(raw_frames, "", 0)
    elif isinstance(raw_frames, list):
        # plist's array-based frame collections are not the standard cocos2d format.
        for index, item in enumerate(raw_frames):
            if not isinstance(item, Mapping):
                warnings.append(f"frames[{index}]: スプライトの辞書データではありません")
                continue
            name = str(item.get("filename", item.get("name", f"frame_{index}")))
            if _has_rect(item):
                entries.append((name, item))
            else:
                warnings.append(f"{name}: 配置矩形 (textureRect / frame / rect など) がありません")
    else:
        raise ValueError("frames は辞書または配列である必要があります")
    return entries, warnings, image_names


def choose_texture(plist_path: Path, metadata: Mapping[str, Any],
                   explicit: Optional[Path] = None,
                   image_names: Sequence[str] = ()) -> Path:
    if explicit is not None:
        chosen = explicit.expanduser().resolve()
        if not chosen.is_file():
            raise FileNotFoundError(f"テクスチャ画像がありません: {chosen}")
        return chosen
    for key in ("realTextureFileName", "textureFileName"):
        value = metadata.get(key)
        if isinstance(value, str):
            found = _relative_texture_path(plist_path, value)
            if found is not None:
                return found
    for suffix in (".png", ".webp", ".jpg", ".jpeg"):
        candidate = plist_path.with_suffix(suffix)
        if candidate.is_file():
            return candidate.resolve()
    # Some nonstandard sheets use their image's filename as a frames grouping key.
    for name in image_names:
        found = _relative_texture_path(plist_path, name)
        if found is not None:
            return found
    raise FileNotFoundError("テクスチャ画像がありません。--image で画像を指定してください")


def load_atlas(plist_path: Path, image_path: Optional[Path] = None) -> Atlas:
    plist_path = plist_path.expanduser().resolve()
    with plist_path.open("rb") as source:
        root = plistlib.load(source)
    if not isinstance(root, dict) or "frames" not in root:
        if classify_plist(root) == 'particle':
            raise ValueError('これはパーティクル設定 plist です。対応する ExportJson から開いてください')
        raise ValueError("frames キーを持つスプライトアトラス形式の plist が必要です")
    raw_frames = root["frames"]
    metadata = root.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("metadata は辞書である必要があります")
    entries, warnings, image_names = _frame_entries(raw_frames)
    texture_path = choose_texture(plist_path, metadata, image_path, image_names)
    with Image.open(texture_path) as source:
        width, height = source.size
        if width * height > MAX_TEXTURE_PIXELS:
            raise ValueError(f"画像の画素数が上限を超えています ({MAX_TEXTURE_PIXELS:,} pixels)")
        image = source.convert("RGBA")
    frames: list[Frame] = []
    for name, item in entries:
        try:
            frame = parse_frame(name, item)
            if frame.x + frame.packed_width > width or frame.y + frame.packed_height > height:
                raise ValueError(f"{name}: 参照領域がテクスチャの範囲を超えています")
        except (ValueError, TypeError, KeyError) as exc:
            warnings.append(str(exc))
            continue
        frames.append(frame)
    if not frames:
        warnings.append("有効なスプライト配置情報はありません。テクスチャ画像のみ表示します")
    return Atlas(plist_path, texture_path,
                 tuple(sorted(frames, key=lambda f: f.name.casefold())),
                 image, tuple(warnings))


def extract_sprite(image: Image.Image, frame: Frame, packed_rotation: str = "cw") -> Image.Image:
    if packed_rotation not in ("cw", "ccw"):
        raise ValueError("packed_rotation は 'cw' または 'ccw' を指定してください")
    cropped = image.crop(frame.packed_box)
    if frame.rotated:
        # Undo the rotation used to pack the sprite.
        transpose = TRANSPOSE_ROTATE_90 if packed_rotation == "cw" else TRANSPOSE_ROTATE_270
        cropped = cropped.transpose(transpose)
    if cropped.size != (frame.width, frame.height):
        raise ValueError(f"{frame.name}: 復元後の画像サイズが一致しません")
    restored = Image.new("RGBA", (frame.source_width, frame.source_height), TRANSPARENT)
    restored.paste(cropped, (frame.trim_x, frame.trim_y))
    return restored


# ---------------------------------------------------------------------------
# Cocos Studio 1.x ExportJson armature viewer
# ---------------------------------------------------------------------------
import bisect
import json
from PIL import ImageDraw, ImageChops

JSON_SIZE_LIMIT = 20_000_000
ARMATURE_BONE_LIMIT = 2048
MOVEMENT_FRAME_LIMIT = 50000
RENDER_PIXEL_LIMIT = 8_000_000
APP_FPS_DEFAULT = 60
APP_FPS_MIN = 1
APP_FPS_MAX = 120
CANVAS_PADDING = 32
MIN_CANVAS_ZOOM = 0.04
MAX_CANVAS_ZOOM = 5.0
CORNERS_COLOR = (255, 180, 75, 240)
MISSING_COLOR = (237, 154, 70, 220)
ENGINE_SELECTION_COLOR = (69, 227, 255, 255)
IMAGE_TRANSFORM_AFFINE = _pillow_constant("Transform", "AFFINE")
RESAMPLE_BICUBIC = _pillow_constant("Resampling", "BICUBIC")

# The left and right exports can contain entirely different armatures and
# atlas pages. They must be switched as data sets, not by flipping pixels.
SIDE_PATTERN = re.compile(r"^(?P<prefix>.+)_(?P<side>[lr])(?:\(\d+\))?$", re.IGNORECASE)


def export_orientation(path: Path) -> Optional[str]:
    match = SIDE_PATTERN.fullmatch(path.stem)
    return match.group('side').lower() if match else None


def find_other_orientation_export(path: Path) -> Optional[Path]:
    """Find a corresponding ExportJson based on its _l/_r file name.

    A numeric suffix such as ``(1)`` is allowed, but arbitrary files are not.
    """
    match = SIDE_PATTERN.fullmatch(path.stem)
    if match is None:
        return None
    other = 'r' if match.group('side').lower() == 'l' else 'l'
    counterpart_stem = (match.group('prefix') + '_' + other).casefold()
    candidates = []
    for candidate in path.parent.iterdir():
        if not candidate.is_file() or candidate.suffix.casefold() not in ('.exportjson', '.json'):
            continue
        alt = SIDE_PATTERN.fullmatch(candidate.stem)
        if alt is not None and (alt.group('prefix') + '_' + alt.group('side')).casefold() == counterpart_stem:
            candidates.append(candidate)
    # Prefer the exact file rather than a numbered duplicate.
    return min(candidates, key=lambda c: (c.stem.casefold() != counterpart_stem, len(c.name), c.name.casefold())) if candidates else None



def _numeric(obj: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    return _as_real(obj.get(key, default), key)


@dataclass(frozen=True)
class TextureDef:
    name: str
    width: int
    height: int
    pivot_x: float
    pivot_y: float


@dataclass(frozen=True)
class DisplayDef:
    name: str
    kind: int
    skin: Mapping[str, Any]


@dataclass(frozen=True)
class ParticleDef:
    plist_path: Path
    settings: Mapping[str, Any]
    image: Image.Image


@dataclass(frozen=True)
class BoneDef:
    name: str
    parent: str
    rest: Mapping[str, Any]
    z: float
    display_index: int
    displays: tuple[DisplayDef, ...]


@dataclass(frozen=True)
class MovementDef:
    name: str
    duration: int
    loop: bool
    timelines: Mapping[str, tuple[Mapping[str, Any], ...]]


@dataclass
class ArmatureModel:
    json_path: Path
    name: str
    bones: tuple[BoneDef, ...]
    textures: dict[str, TextureDef]
    movements: tuple[MovementDef, ...]
    sprites: dict[str, Image.Image]
    warnings: list[str]
    config_plists: tuple[str, ...]
    config_images: tuple[str, ...]
    loaded_plists: tuple[Path, ...] = ()
    loaded_images: tuple[Path, ...] = ()
    particle_effects: dict[str, ParticleDef] = field(default_factory=dict)
    loaded_particle_plists: tuple[Path, ...] = ()
    # Per-(bone,display) sprite-space overrides, independent of global UV flags.
    # Keys are "<bone name>::<display index>".  See SKIN_OVERRIDE_OPTIONS.
    skin_overrides: dict[str, str] = field(default_factory=dict)
    # movement identity -> integer frame number -> immutable, authored-order pose.
    # Cached poses share sprite textures; they do not duplicate image pixels.
    pose_cache: dict[int, dict[int, tuple['PlacedSprite', ...]]] = field(default_factory=dict)
    pose_cache_frames: int = 0
    pose_cache_sprites: int = 0
    pose_cache_seconds: float = 0.0


# Affine (a, b, c, d, tx, ty): (x,y)->(ax+cy+tx, bx+dy+ty)
# Hair sorting modes affect only bone order. No atlas, transform, UV, or
# animation timing is altered. "swap" compares the hair/headgear relationship
# against source Z order; the user can restore the authored ordering at any time.
HAIR_LAYER_LABELS = {
    '元データの順序': 'original',
    '髪と頭飾りを入替': 'swap',
    '髪を手前に': 'front',
    '髪を奥に': 'back',
}
HAIR_LAYER_DEFAULT = '髪と頭飾りを入替'

AFFINE_ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def matrix_mul(m: tuple[float, ...], n: tuple[float, ...]) -> tuple[float, ...]:
    a,b,c,d,x,y = m
    e,f,g,h,u,v = n
    return (a*e+c*f, b*e+d*f, a*g+c*h, b*g+d*h,
            a*u+c*v+x, b*u+d*v+y)


def matrix_apply(m: tuple[float, ...], x: float, y: float) -> tuple[float, float]:
    return (m[0]*x+m[2]*y+m[4], m[1]*x+m[3]*y+m[5])


def node_matrix(node: Mapping[str, Any]) -> tuple[float, ...]:
    """Cocos Studio TransformHelp::nodeToMatrix (column-vector affine).

    X-axis = scaleX * (cos(skewY), sin(skewY));
    Y-axis = scaleY * (sin(skewX), cos(skewX)).
    Crucially, kX controls the *second column* and kY the first column.
    Interchanging these is invisible for a pure rotation (kY == -kX),
    but changes reflected/sheared skins, explaining the v21 left-side bug.
    """
    x = _numeric(node, 'x')
    y = _numeric(node, 'y')
    sx = _numeric(node, 'cX', 1.0)
    sy = _numeric(node, 'cY', 1.0)
    kx = _numeric(node, 'kX')
    ky = _numeric(node, 'kY')
    return (sx * math.cos(ky), sx * math.sin(ky),
            sy * math.sin(kx), sy * math.cos(kx), x, y)


def _normal_name(filename: str) -> str:
    return Path(filename.replace('\\', '/')).stem.casefold()


def _asset_file(base: Path, value: str) -> Optional[Path]:
    """Only local paths under ExportJson's directory are trusted."""
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.replace('\\', '/')
    candidate = Path(cleaned)
    if candidate.is_absolute() or '..' in candidate.parts:
        return None
    root = base.resolve()
    absolute = (root / candidate).resolve()
    if absolute != root and root in absolute.parents and absolute.is_file():
        return absolute
    return None


EXPORT_ASSET_DIR_HINTS = frozenset((
    "export", "exportjson", "json", "armature", "armatures", "animation",
    "animations", "res", "resources", "texture", "textures", "atlas", "atlases",
    "image", "images", "sprites", "assets",
))


def _find_export_asset(base: Path, reference: str) -> Optional[Path]:
    """Locate a file named by ExportJson without interpreting arbitrary paths.

    Search order is deterministic: ExportJson directory, its immediate subdirs,
    then a limited sibling search if the JSON itself is inside a conventional
    resource directory. No unrestricted recursive scan or arbitrary '..' access.
    """
    if not isinstance(reference, str) or not reference.strip():
        return None
    cleaned = reference.strip().replace('\\', '/')
    rel = Path(cleaned)
    if rel.is_absolute() or '..' in rel.parts or rel.name in ('', '.', '..'):
        return None

    base = base.resolve()
    paths = [base]
    try:
        paths.extend(sorted((d for d in base.iterdir() if d.is_dir() and not d.is_symlink()),
                            key=lambda d: d.name.casefold())[:128])
    except OSError:
        pass
    if base.name.casefold() in EXPORT_ASSET_DIR_HINTS:
        parent = base.parent
        paths.append(parent)
        try:
            paths.extend(sorted((d for d in parent.iterdir() if d.is_dir() and not d.is_symlink()),
                                key=lambda d: d.name.casefold())[:128])
        except OSError:
            pass
    seen = set()
    # Search every exact relative path before trying any basename fallback.
    # This avoids accidentally selecting an unrelated file with the same name.
    for part in (rel, Path(rel.name)):
        seen.clear()
        for folder in paths:
            folder = folder.resolve()
            if folder in seen:
                continue
            seen.add(folder)
            result = (folder / part).resolve()
            if result != folder and folder in result.parents and result.is_file():
                try:
                    ensure_asset_path(result)
                except ValueError:
                    continue
                return result
    return None


def _load_limited_image(path: Path) -> Image.Image:
    with Image.open(path) as img:
        if img.width * img.height > MAX_TEXTURE_PIXELS:
            raise ValueError(f"画像が大きすぎます: {path.name}")
        return img.convert('RGBA')


PARTICLE_EMBEDDED_MAX_BYTES = 16 * 1024 * 1024
PARTICLE_MAX_ACTIVE = 128  # Preview safety limit, independent of authored maxParticles
PARTICLE_TIMEBASE_FPS = 60.0


def classify_plist(root: Any) -> str:
    """A particle emitter plist is NOT a sprite-frame atlas, even if referenced in config_file_path."""
    if not isinstance(root, dict):
        return 'unknown'
    if isinstance(root.get('frames'), (dict, list)):
        return 'atlas'
    if all(key in root for key in ('emitterType', 'maxParticles', 'particleLifespan')):
        return 'particle'
    return 'unknown'


def decode_particle_embedded_image(encoded: str) -> Image.Image:
    """Decode embedded base64(gzip(PNG/TIFF)) without writing an external file."""
    if not isinstance(encoded, str) or len(encoded) > PARTICLE_EMBEDDED_MAX_BYTES * 2:
        raise ValueError('パーティクル画像のデータサイズが不正です')
    try:
        compressed = base64.b64decode(encoded, validate=True)
        if len(compressed) > PARTICLE_EMBEDDED_MAX_BYTES:
            raise ValueError('圧縮パーティクル画像が大きすぎます')
        if compressed[:2] == b'\x1f\x8b':
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            content = inflater.decompress(compressed, PARTICLE_EMBEDDED_MAX_BYTES + 1)
            if not inflater.eof or len(content) > PARTICLE_EMBEDDED_MAX_BYTES:
                raise ValueError('展開後のパーティクル画像が大きすぎます')
        else:
            content = compressed
        with Image.open(io.BytesIO(content)) as image:
            if image.width * image.height > MAX_TEXTURE_PIXELS:
                raise ValueError('パーティクル画像が大きすぎます')
            # Particle Designer and older Cocos Studio emitters may embed
            # TIFF pixel data while declaring a .png textureFileName.
            # Check the decoded image's actual container, not the filename.
            if image.format not in ('PNG', 'TIFF'):
                raise ValueError(f'未対応のパーティクル内蔵画像形式: {image.format}')
            return image.convert('RGBA')
    except (ValueError, OSError, TypeError, base64.binascii.Error, zlib.error,
            UnidentifiedImageError) as exc:
        raise ValueError(f'パーティクル内蔵画像を復元できません: {exc}') from exc


def load_particle_definition(plist_path: Path, sheet: Mapping[str, Any],
                             external_png: Optional[Path] = None) -> ParticleDef:
    """Load particle parameters and an external or embedded texture image."""
    if classify_plist(sheet) != 'particle':
        raise ValueError('パーティクル設定ファイルではありません')
    if external_png is not None and external_png.is_file():
        image = _load_limited_image(external_png)
    elif isinstance(sheet.get('textureImageData'), str) and sheet['textureImageData']:
        image = decode_particle_embedded_image(sheet['textureImageData'])
    else:
        reference = sheet.get('textureFileName')
        png = _relative_texture_path(plist_path, reference) if isinstance(reference, str) else None
        if png is None:
            raise FileNotFoundError('パーティクル画像がありません (textureFileName / textureImageData)')
        image = _load_limited_image(png)
    return ParticleDef(plist_path, sheet, image)


def _particle_number(settings: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    value = settings.get(key, default)
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _particle_random(index: int, salt: int) -> float:
    """Stable pseudo-random value in [-1,1], regardless of hash randomization."""
    value = ((index + 1) * 747796405 + (salt + 1) * 2891336453) & 0xffffffff
    value = (value ^ (value >> 16)) * 2246822519 & 0xffffffff
    value ^= value >> 13
    return (value / 0xffffffff) * 2.0 - 1.0


def _particle_emission_start(frames: tuple[Mapping[str, Any], ...],
                             index: float, default_index: int) -> Optional[float]:
    """Return the start of the current visible interval, or None if invisible."""
    initial = int(frames[0].get('dI', default_index)) if frames else default_index
    visible = initial >= 0
    start = 0.0 if visible else None
    previous = visible
    for frame in frames:
        fi = _numeric(frame, 'fi')
        if fi > index:
            break
        visible = int(frame.get('dI', default_index)) >= 0
        if visible and not previous:
            start = fi
        elif not visible:
            start = None
        previous = visible
    return start if visible else None


def particle_placements(bone: BoneDef, effect: ParticleDef,
                        transform: tuple[float, ...], world_frame: float,
                        visible_start: float, z: float) -> list['PlacedSprite']:
    """Deterministic GRAVITY-mode particle preview, keyed by animation frame.

    Uses source life/rate/velocity/size/rotation and the plist blend factors.
    This is a simplified deterministic preview, not an exact particle engine.
    """
    settings = effect.settings
    if int(_particle_number(settings, 'emitterType')) != 0:
        return []  # Radius mode is deliberately not approximated as gravity.
    max_count = min(PARTICLE_MAX_ACTIVE, max(0, int(_particle_number(settings, 'maxParticles'))))
    life_base = _particle_number(settings, 'particleLifespan', 1.0)
    life_var = abs(_particle_number(settings, 'particleLifespanVariance'))
    if not max_count or life_base <= 0:
        return []
    rate = max_count / life_base
    elapsed = max(0.0, (world_frame - visible_start) / PARTICLE_TIMEBASE_FPS)
    duration = _particle_number(settings, 'duration', -1)
    last_birth = elapsed if duration < 0 else min(duration, elapsed)
    if last_birth <= 0:
        return []
    first_idx = max(0, math.floor((elapsed - life_base - life_var) * rate) - 1)
    last_idx = min(math.ceil(last_birth * rate), first_idx + PARTICLE_MAX_ACTIVE * 4)
    particle_image = effect.image
    texture = TextureDef(effect.plist_path.stem, particle_image.width,
                         particle_image.height, .5, .5)
    result = []
    particle_blend = _blend_for_state({
        'bd_src': int(_particle_number(settings, 'blendFuncSource', 770)),
        'bd_dst': int(_particle_number(settings, 'blendFuncDestination', 771)),
    })
    for idx in range(first_idx, last_idx):
        birth = (idx + 0.5) / rate
        age = elapsed - birth
        life = max(0.01, life_base + life_var * _particle_random(idx, 17))
        if age < 0 or age >= life:
            continue
        angle = math.radians(_particle_number(settings, 'angle') +
                             _particle_random(idx, 1) * _particle_number(settings, 'angleVariance'))
        speed = (_particle_number(settings, 'speed') +
                 _particle_random(idx, 2) * _particle_number(settings, 'speedVariance'))
        ox = _particle_random(idx, 3) * _particle_number(settings, 'sourcePositionVariancex')
        oy = _particle_random(idx, 4) * _particle_number(settings, 'sourcePositionVariancey')
        px = ox + math.cos(angle) * speed * age + .5 * _particle_number(settings, 'gravityx') * age * age
        py = oy + math.sin(angle) * speed * age + .5 * _particle_number(settings, 'gravityy') * age * age
        start_size = max(0.1, _particle_number(settings, 'startParticleSize', particle_image.width) +
                         _particle_random(idx, 5) * _particle_number(settings, 'startParticleSizeVariance'))
        finish_size = _particle_number(settings, 'finishParticleSize', -1)
        if finish_size < 0:
            finish_size = start_size
        else:
            finish_size = max(0.1, finish_size + _particle_random(idx, 6) *
                              _particle_number(settings, 'finishParticleSizeVariance'))
        size = start_size + (finish_size - start_size) * min(1.0, age / life)
        scale = size / max(particle_image.width, particle_image.height)
        rotation_start = (_particle_number(settings, 'rotationStart') +
                          _particle_random(idx, 7) * _particle_number(settings, 'rotationStartVariance'))
        rotation_finish = (_particle_number(settings, 'rotationEnd') +
                           _particle_random(idx, 8) * _particle_number(settings, 'rotationEndVariance'))
        rotation = math.radians(rotation_start + (rotation_finish - rotation_start) * min(1.0, age / life))
        co, si = math.cos(rotation) * scale, math.sin(rotation) * scale
        local = (co, si, -si, co, px, py)
        # Particle Designer color curve (linear; deterministic preview).
        progress = min(1.0, age / life)
        channels = []
        for component in ('Red', 'Green', 'Blue', 'Alpha'):
            name = component.lower()
            start_color = _particle_number(settings, 'startColor' + component, 1.0)
            end_color = _particle_number(settings, 'finishColor' + component, 1.0)
            start_color += _particle_random(idx, 19 + len(channels)) * _particle_number(
                settings, 'startColorVariance' + component)
            end_color += _particle_random(idx, 23 + len(channels)) * _particle_number(
                settings, 'finishColorVariance' + component)
            channels.append(max(0, min(255, round(255 * (
                start_color + (end_color - start_color) * progress)))))
        result.append(PlacedSprite(bone.name, 'particle:' + effect.plist_path.name,
                                   texture, particle_image, matrix_mul(transform, local), z,
                                   particle_blend, tuple(channels)))
    return result


def _list_names(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(s for s in value if isinstance(s, str))
    if isinstance(value, str):
        return (value,)
    return ()


def parse_exportjson(path: Path) -> ArmatureModel:
    path = path.expanduser().resolve()
    if path.stat().st_size > JSON_SIZE_LIMIT:
        raise ValueError('ExportJson のサイズが上限を超えています')
    with path.open('r', encoding='utf-8-sig') as f:
        root = json.load(f)
    if not isinstance(root, dict) or not isinstance(root.get('armature_data'), list):
        raise ValueError('armature_data 配列を持つ ExportJson が必要です')
    armlist = root['armature_data']
    if not armlist or not isinstance(armlist[0], dict):
        raise ValueError('armature_data にアーマチュアがありません')
    arm = armlist[0]
    raw_bones = arm.get('bone_data', [])
    if not isinstance(raw_bones, list) or len(raw_bones) > ARMATURE_BONE_LIMIT:
        raise ValueError('bone_data の形式または件数が不正です')
    warnings = []
    bones = []
    names = set()
    for raw in raw_bones:
        if not isinstance(raw, dict) or not isinstance(raw.get('name'), str):
            warnings.append('不正な bone_data を省略しました')
            continue
        name = raw['name']
        if name in names:
            warnings.append(f'重複ボーンを省略: {name}')
            continue
        names.add(name)
        displays = []
        for item in raw.get('display_data', []):
            if not isinstance(item, dict):
                continue
            skins = item.get('skin_data', [])
            skin = skins[0] if isinstance(skins, list) and skins and isinstance(skins[0], dict) else {}
            kind = int(item.get('displayType', 0))
            # Particle displays refer to their emitter plist, not a sprite name.
            reference = item.get('plist', '') if kind == 2 else item.get('name', '')
            displays.append(DisplayDef(str(reference), kind, skin))
        bones.append(BoneDef(name, str(raw.get('parent', '')), raw,
                             _numeric(raw, 'z'), int(raw.get('dI', 0)), tuple(displays)))
    if not bones:
        raise ValueError('有効なボーンがありません')
    textures = {}
    for item in root.get('texture_data', []):
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            continue
        name = _normal_name(item['name'])
        w = round(_numeric(item, 'width'))
        h = round(_numeric(item, 'height'))
        if w <= 0 or h <= 0 or w*h > MAX_TEXTURE_PIXELS:
            warnings.append(f'画像寸法を省略: {name}')
            continue
        textures[name] = TextureDef(name, w, h, _numeric(item, 'pX', 0.5), _numeric(item, 'pY', 0.5))
    movements = []
    raw_anims = root.get('animation_data', [])
    for anim in raw_anims if isinstance(raw_anims, list) else []:
        if not isinstance(anim, dict) or anim.get('name') != arm.get('name'):
            continue
        for mov in anim.get('mov_data', []):
            if not isinstance(mov, dict):
                continue
            duration = int(mov.get('dr', 1))
            timelines = {}
            for timeline in mov.get('mov_bone_data', []):
                if not isinstance(timeline, dict) or not isinstance(timeline.get('name'), str):
                    continue
                frames = [f for f in timeline.get('frame_data', []) if isinstance(f, dict)]
                frames.sort(key=lambda f: _numeric(f, 'fi'))
                timelines[timeline['name']] = tuple(frames)
            if duration <= 0:
                # Some exports contain zero/negative-duration movements. They
                # remain valid named actions (notably static/transition poses).
                # Do not silently discard them: use the last authored keyframe
                # when one exists, otherwise make a one-frame static action.
                latest = max((_numeric(frame, 'fi')
                              for frames in timelines.values() for frame in frames),
                             default=0.0)
                duration = max(1, int(math.floor(latest)) + 1)
            if duration > MOVEMENT_FRAME_LIMIT:
                warnings.append(f'動作の時間長が上限を超えています: {mov.get("name", "?")}')
                continue
            movements.append(MovementDef(str(mov.get('name', 'unnamed')), duration,
                                         bool(mov.get('lp', False)), timelines))
    if not movements:
        movements.append(MovementDef('静止姿勢', 1, False, {}))
    return ArmatureModel(path, str(arm.get('name', path.stem)), tuple(bones), textures,
                         tuple(movements), {}, warnings,
                         _list_names(root.get('config_file_path')),
                         _list_names(root.get('config_png_path')))


def resolve_armature_images(model: ArmatureModel,
                            explicit_plist: Optional[Path] = None,
                            explicit_image: Optional[Path] = None,
                            rotation: str = 'cw') -> None:
    """Automatically load ExportJson companion atlas + texture and sprite regions.

    An explicit path takes priority. The ExportJson's config_file_path and
    config_png_path entries are next; matching basename is required for
    multi-page atlases to prevent pairing an unrelated image with a plist.
    Missing assets leave visible placeholders and a diagnostic warning.
    """
    model.sprites.clear()
    model.loaded_plists = ()
    model.loaded_images = ()
    model.particle_effects.clear()
    model.loaded_particle_plists = ()
    model.warnings[:] = [w for w in model.warnings if not w.startswith(
        ('画像がありません:', 'アトラス:', 'パーティクル:', '未解決画像:', '参照ファイル:',
         '寸法差:', 'アトラス未収録:'))]
    base = model.json_path.parent

    def validated_override(path: Path) -> Path:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f'指定されたファイルが存在しません: {resolved}')
        return resolved

    if explicit_plist is not None:
        plist_paths = [validated_override(explicit_plist)]
        requested = {Path(p).name.casefold() for p in model.config_plists}
        if requested and plist_paths[0].name.casefold() not in requested:
            model.warnings.append('参照 plist が ExportJson の指定と異なります: '
                                  + plist_paths[0].name + ' / ' + ', '.join(sorted(requested)))
    else:
        plist_paths = [found for name in model.config_plists
                       if (found := _find_export_asset(base, name)) is not None]
    if explicit_image is not None:
        image_paths = [validated_override(explicit_image)]
    else:
        image_paths = [found for name in model.config_images
                       if (found := _find_export_asset(base, name)) is not None]
    # Metadata may have omitted one or both companion entries. Only try the
    # ExportJson's exact basename, never the first arbitrary image in a folder.
    prefix = model.json_path.stem
    if not plist_paths and not model.config_plists:
        match = _find_export_asset(base, f'{prefix}0.plist')
        if match is not None:
            plist_paths.append(match)
    if not image_paths and not model.config_images:
        match = _find_export_asset(base, f'{prefix}0.png')
        if match is not None:
            image_paths.append(match)

    # Classify each referenced plist before trying to interpret it as a sprite
    # atlas. Cocos Studio includes Particle Designer emitters in the same
    # config_file_path array as sprite atlases.
    sheets: dict[Path, Mapping[str, Any]] = {}
    atlas_paths: list[Path] = []
    particle_paths: list[Path] = []
    packed_sprite_names: set[str] = set()
    inspected_plists = 0
    for candidate in plist_paths:
        try:
            with candidate.open('rb') as stream:
                sheet = plistlib.load(stream)
            kind = classify_plist(sheet)
            if kind == 'atlas':
                sheets[candidate] = sheet
                atlas_paths.append(candidate)
                records, _, _ = _frame_entries(sheet['frames'])
                packed_sprite_names.update(_normal_name(name) for name, _ in records)
                inspected_plists += 1
            elif kind == 'particle':
                sheets[candidate] = sheet
                particle_paths.append(candidate)
            else:
                model.warnings.append(f'参照ファイル: {candidate.name}: 未対応の plist 構造です')
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException) as exc:
            model.warnings.append(f'参照ファイル: {candidate.name}: {exc}')
    if inspected_plists:
        orphan_names = [name for name in model.textures
                        if name not in packed_sprite_names
                        and not any(_find_export_asset(base, name + ext)
                                    for ext in ('.png', '.webp', '.jpg', '.jpeg'))]
        if orphan_names:
            model.warnings.append('アトラス未収録: ' + ', '.join(orphan_names[:35]) +
                                  (f' (ほか {len(orphan_names)-35} 件)' if len(orphan_names)>35 else '') +
                                  '。該当する元画像を別途用意する必要があります。')

    loaded_plists: list[Path] = []
    loaded_images: list[Path] = []
    loaded_particles: list[Path] = []
    for plist_path in particle_paths:
        sheet = sheets[plist_path]
        match = next((p for p in image_paths if p.stem.casefold() == plist_path.stem.casefold()), None)
        try:
            effect = load_particle_definition(plist_path, sheet, match)
            model.particle_effects[plist_path.name.casefold()] = effect
            loaded_particles.append(plist_path)
            if match is not None and match.is_file() and match not in loaded_images:
                loaded_images.append(match)
        except (OSError, ValueError, TypeError, UnidentifiedImageError) as exc:
            model.warnings.append(f'パーティクル: {plist_path.name}: {exc}')
    model.loaded_particle_plists = tuple(loaded_particles)
    for plist_path in atlas_paths:
        # The corresponding page image uses the same stem as the plist.
        match = next((p for p in image_paths if p.stem.casefold() == plist_path.stem.casefold()), None)
        if match is None and explicit_image is not None:
            match = image_paths[0]
        if match is None and len(plist_paths) == 1 and len(image_paths) == 1:
            match = image_paths[0]
        try:
            atlas = load_atlas(plist_path, match)
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException,
                UnidentifiedImageError) as exc:
            model.warnings.append(f'アトラス: {plist_path.name}: {exc}')
            continue
        loaded_plists.append(atlas.plist_path)
        if atlas.texture_path not in loaded_images:
            loaded_images.append(atlas.texture_path)
        model.warnings.extend(f'アトラス: {w}' for w in atlas.warnings[:20])
        for f in atlas.frames:
            key = _normal_name(f.name)
            if key not in model.sprites:
                try:
                    model.sprites[key] = extract_sprite(atlas.image, f, rotation)
                except ValueError as exc:
                    model.warnings.append(f'アトラス: {exc}')

    for tex_name in model.textures:
        if tex_name in model.sprites:
            continue
        match = next((p for filename in (f'{tex_name}.png', f'{tex_name}.webp', f'{tex_name}.jpg')
                      if (p := _find_export_asset(base, filename)) is not None), None)
        if match is not None:
            try:
                model.sprites[tex_name] = _load_limited_image(match)
                if match not in loaded_images:
                    loaded_images.append(match)
            except (OSError, ValueError) as exc:
                model.warnings.append(f'画像がありません: {match.name} ({exc})')
    model.loaded_plists = tuple(loaded_plists)
    model.loaded_images = tuple(loaded_images)
    discrepancies = []
    for name, texture in model.textures.items():
        source = model.sprites.get(name)
        if source is None:
            continue
        ratio_x = texture.width / source.width
        ratio_y = texture.height / source.height
        if not (0.8 <= ratio_x <= 1.2 and 0.8 <= ratio_y <= 1.2):
            discrepancies.append((name, source.size, (texture.width, texture.height)))
    if discrepancies:
        examples = ', '.join(
            f'{name}: atlas={actual[0]}x{actual[1]}, JSON={configured[0]}x{configured[1]}'
            for name, actual, configured in discrepancies[:6]
        )
        model.warnings.append(
            f'寸法差: {len(discrepancies)} パーツはアトラス元寸法と JSON 寸法が異なります。'
            f' 描画はアトラス元寸法を優先します。 {examples}'
        )
    missing = [name for name in model.textures if name not in model.sprites]
    if missing:
        if not loaded_plists and atlas_paths:
            model.warnings.append('参照ファイル: スプライトアトラス用の PNG がありません: '
                                  + ', '.join(p.name for p in atlas_paths))
        elif not loaded_plists and not atlas_paths and model.config_plists:
            model.warnings.append('参照ファイル: スプライトアトラス plist を検出できませんでした')
        model.warnings.append(f'未解決画像: {len(missing)} 個の画像は枠表示します: ' + ', '.join(missing))


def easing_percent(ratio: float, easing: int) -> float:
    """CocoStudio legacy numeric tween codes (non-overshooting curves).

    The Cocos2d 1.x tween enumeration includes sine, quadratic, cubic and
    quartic variants. Other/custom codes deliberately fall back to linear;
    guessing a spring or Bezier curve can move the limb outside its endpoints.
    """
    t = max(0.0, min(1.0, ratio))
    if easing == 1:  # Sine_EaseIn
        return 1.0 - math.cos(t * math.pi / 2.0)
    if easing == 2:  # Sine_EaseOut (early Cocostudio enum)
        return math.sin(t * math.pi / 2.0)
    if easing == 3:  # Sine_EaseInOut
        return (1.0 - math.cos(math.pi * t)) / 2.0
    if easing in (4, 7, 10):  # Quad/Cubic/Quart EaseIn
        return t ** {4: 2, 7: 3, 10: 4}[easing]
    if easing in (5, 8, 11):  # Quad/Cubic/Quart EaseOut
        power = {5: 2, 8: 3, 11: 4}[easing]
        return 1.0 - (1.0 - t) ** power
    if easing in (6, 9, 12):  # EaseInOut
        power = {6: 2, 9: 3, 12: 4}[easing]
        return (2.0 ** (power - 1) * t ** power if t < 0.5
                else 1.0 - ((-2.0 * t + 2.0) ** power) / 2.0)
    return t


def frame_state(frames: tuple[Mapping[str, Any], ...], index: float) -> Optional[dict[str, Any]]:
    """Evaluate animation transforms without reinterpreting authored rotations.

    Frame `fi` values are absolute timeline positions. `dI`, `z` and visibility
    are discrete keyframe properties, whereas spatial transforms are tweened.
    Rotations must preserve their authored winding (e.g. +5 radians): wrapping
    to the nearest ±pi path reverses large arm/leg motions.
    """
    if not frames:
        return None
    positions = [_numeric(f, 'fi') for f in frames]
    i = max(0, bisect.bisect_right(positions, index) - 1)
    src = frames[i]
    result = dict(src)
    if i + 1 >= len(frames) or not bool(src.get('tweenFrame', True)):
        return result
    nextframe = frames[i + 1]
    f0, f1 = positions[i], positions[i + 1]
    if f1 <= f0 or index >= f1:
        return result
    ratio = easing_percent((index - f0) / (f1 - f0), int(src.get('twE', 0)))
    for key, default in (('x', 0.0), ('y', 0.0), ('cX', 1.0),
                         ('cY', 1.0), ('kX', 0.0), ('kY', 0.0)):
        start = _numeric(src, key, default)
        end = _numeric(nextframe, key, default)
        result[key] = start + (end - start) * ratio
    # Color tween (if authored): same easing as the transform, absent = white.
    if isinstance(src.get('color'), Mapping) or isinstance(nextframe.get('color'), Mapping):
        c0 = _color_for_state(src)
        c1 = _color_for_state(nextframe)
        result['color'] = dict(zip(('r', 'g', 'b', 'a'),
                                   (int(round(a + (b - a) * ratio))
                                    for a, b in zip(c0, c1))))
    return result


def combined_bone_components(rest: Mapping[str, Any],
                             animation: Optional[Mapping[str, Any]]) -> dict[str, float]:
    """CocoStudio VERSION_COMBINED: combine frame with bone setup components.

    Bone::update invokes TransformHelp::nodeConcat then subtracts one from the
    scale components. The original engine used a mutable TweenData; composing
    into a fresh dict avoids an older engine bug that accumulated rest data.
    """
    frame = animation or {}
    return {
        'x': _numeric(rest, 'x') + _numeric(frame, 'x'),
        'y': _numeric(rest, 'y') + _numeric(frame, 'y'),
        'cX': _numeric(rest, 'cX', 1.0) + _numeric(frame, 'cX', 1.0) - 1.0,
        'cY': _numeric(rest, 'cY', 1.0) + _numeric(frame, 'cY', 1.0) - 1.0,
        'kX': _numeric(rest, 'kX') + _numeric(frame, 'kX'),
        'kY': _numeric(rest, 'kY') + _numeric(frame, 'kY'),
    }


def combined_bone_matrix(rest: Mapping[str, Any],
                         animation: Optional[Mapping[str, Any]]) -> tuple[float, ...]:
    """Local matrix for a bone before parent-space inheritance."""
    return node_matrix(combined_bone_components(rest, animation))


@dataclass(frozen=True)
class CocosBoneWorld:
    """World *components*, not a parent-matrix product.

    CocoStudio Bone::applyParentTransform transforms the child position with
    the parent's world matrix, but inherits signed scale by multiplication
    and skew angles by addition. The resulting world matrix is rebuilt from
    these components. Matrix multiplication gives a different result for
    nonuniform scale + skew and for mirrored parents.
    """
    x: float
    y: float
    sx: float
    sy: float
    kx: float
    ky: float

    @property
    def matrix(self) -> tuple[float, ...]:
        return node_matrix({'x': self.x, 'y': self.y,
                            'cX': self.sx, 'cY': self.sy,
                            'kX': self.kx, 'kY': self.ky})


def cocos_parent_inherit(local: Mapping[str, Any],
                         parent: Optional[CocosBoneWorld]) -> CocosBoneWorld:
    x, y = _numeric(local, 'x'), _numeric(local, 'y')
    sx, sy = _numeric(local, 'cX', 1.0), _numeric(local, 'cY', 1.0)
    kx, ky = _numeric(local, 'kX'), _numeric(local, 'kY')
    if parent is None:
        return CocosBoneWorld(x, y, sx, sy, kx, ky)
    x, y = matrix_apply(parent.matrix, x, y)
    return CocosBoneWorld(x, y, sx * parent.sx, sy * parent.sy,
                          kx + parent.kx, ky + parent.ky)


# OpenGL enum values written by Cocos Studio as frame_data.bd_src / bd_dst.
# 776 (SRC_ALPHA_SATURATE) is supported in the CPU path; ModernGL does not
# expose it as a supported context.blend_func constant, so GPU falls back.
BLEND_FACTOR_NAMES = {
    0: 'ZERO', 1: 'ONE',
    768: 'SRC_COLOR', 769: 'ONE_MINUS_SRC_COLOR',
    770: 'SRC_ALPHA', 771: 'ONE_MINUS_SRC_ALPHA',
    772: 'DST_ALPHA', 773: 'ONE_MINUS_DST_ALPHA',
    774: 'DST_COLOR', 775: 'ONE_MINUS_DST_COLOR',
    776: 'SRC_ALPHA_SATURATE',
}
BLEND_DEFAULT = (770, 771)
COLOR_WHITE = (255, 255, 255, 255)


def _blend_for_state(state: Optional[Mapping[str, Any]]) -> tuple[int, int]:
    if state is None:
        return BLEND_DEFAULT
    source = int(state.get('bd_src', BLEND_DEFAULT[0]))
    dest = int(state.get('bd_dst', BLEND_DEFAULT[1]))
    if source not in BLEND_FACTOR_NAMES or dest not in BLEND_FACTOR_NAMES or dest == 776:
        raise ValueError(f'未対応のブレンド係数: src={source}, dst={dest}')
    return source, dest


def _color_for_state(state: Optional[Mapping[str, Any]]) -> tuple[int, int, int, int]:
    if not state or not isinstance(state.get('color'), Mapping):
        return COLOR_WHITE
    c = state['color']
    return tuple(max(0, min(255, int(round(float(c.get(key, 255))))))
                 for key in ('r', 'g', 'b', 'a'))


def _color_multiply(a: tuple[int, int, int, int],
                    b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return tuple((x * y + 127) // 255 for x, y in zip(a, b))


@dataclass(frozen=True)
class PlacedSprite:
    bone_name: str
    image_name: str
    texture: TextureDef
    image: Optional[Image.Image]
    matrix: tuple[float, ...]
    z: float
    blend: tuple[int, int] = BLEND_DEFAULT
    tint: tuple[int, int, int, int] = COLOR_WHITE


def adjust_hair_layer(placed: list[PlacedSprite], bones: tuple[BoneDef, ...],
                      mode: str = 'original') -> list[PlacedSprite]:
    """Override only the relative draw order of hair and its own head group.

    Cocos Studio authors use different head / hair / accessory Z values between
    the left/right rigs. A blanket Z inversion would corrupt all other parts.
    This operation leaves sprite transforms, images, bone visibility, and the
    relative order of all non-hair/ornament parts untouched.

    "swap" switches hair vs matching *headgear*, "front" places hair above
    its head and ornament, "back" places hair below them. Frames in which the
    relevant part is hidden simply retain their original ordering.
    """
    if mode not in ('original', 'swap', 'front', 'back'):
        raise ValueError(f'不明な髪レイヤー設定: {mode}')
    if mode == 'original' or len(placed) < 2:
        return placed

    result = list(placed)
    for hair in bones:
        if not any('hair' in display.name.casefold() for display in hair.displays):
            continue
        if not any(part.bone_name == hair.name for part in result):
            continue
        # Only consider siblings under the very same parent head.
        ornaments = [b.name for b in bones if b.parent == hair.parent and
                     ('headgia' in b.name.casefold().replace('_', '') or
                      any('headgia' in disp.name.casefold().replace('_', '')
                          for disp in b.displays))]
        # Do not move parts of a different head / facing rig.
        ornament_items = [part for part in result if part.bone_name in ornaments]
        head_item = next((part for part in result if part.bone_name == hair.parent), None)
        hair_item = next(part for part in result if part.bone_name == hair.name)
        if mode == 'swap':
            if not ornament_items:
                continue
            other = ornament_items[0]
            hair_idx = result.index(hair_item)
            ornament_idx = result.index(other)
            if hair_idx < ornament_idx:
                # Put the ornament immediately behind hair; keep hair's
                # absolute placement against unrelated body / limb sprites.
                result.remove(other)
                result.insert(result.index(hair_item), other)
            else:
                result.remove(hair_item)
                result.insert(result.index(other), hair_item)
        else:
            relatives = ornament_items + ([head_item] if head_item is not None else [])
            if not relatives:
                continue
            hair_idx = result.index(hair_item)
            relative_indices = [result.index(item) for item in relatives]
            if mode == 'front' and hair_idx < max(relative_indices):
                target = max(relatives, key=result.index)
                result.remove(hair_item)
                result.insert(result.index(target) + 1, hair_item)
            elif mode == 'back' and hair_idx > min(relative_indices):
                target = min(relatives, key=result.index)
                result.remove(hair_item)
                result.insert(result.index(target), hair_item)
    return result


# The rendering core does not guess UV orientation from determinant, filename,
# motion, or facing. Optional manual modes below are explicit user overrides.
REFLECTION_EPSILON = 1e-7
SKIN_OVERRIDE_OPTIONS = {
    '標準（Cocos Studio）': 'auto',
    '元データのまま': 'original',
    'X反転': 'x',
    'Y反転': 'y',
    'XY反転': 'xy',
    'X反転（旧設定互換）': 'auto_x',
    'Y反転（旧設定互換）': 'auto_y',
    'XY反転（旧設定互換）': 'auto_xy',
}
SKIN_OVERRIDE_FLIPS = {
    'auto': (1, 1), 'original': (1, 1),
    'x': (-1, 1), 'y': (1, -1), 'xy': (-1, -1),
    'auto_x': (-1, 1), 'auto_y': (1, -1), 'auto_xy': (-1, -1),
}


def skin_override_key(bone_name: str, display_index: int) -> str:
    return f'{bone_name}::{display_index}'


def skin_matrix_for_display(model: ArmatureModel, bone: BoneDef,
                            index: int) -> tuple[float, ...]:
    """Authored skin matrix, with explicit user override only (no heuristics)."""
    local = node_matrix(bone.displays[index].skin)
    mode = model.skin_overrides.get(skin_override_key(bone.name, index), 'auto')
    if mode not in SKIN_OVERRIDE_FLIPS:
        raise ValueError(f'不明なパーツ補正: {bone.name} dI={index}: {mode}')
    sx, sy = SKIN_OVERRIDE_FLIPS[mode]
    if (sx, sy) != (1, 1):
        local = matrix_mul(local, (float(sx), 0.0, 0.0, float(sy), 0.0, 0.0))
    return local


def validate_skin_overrides(model: ArmatureModel, values: Mapping[str, str]) -> dict[str, str]:
    """Reject invalid keys/modes rather than silently ignoring user settings."""
    valid = {skin_override_key(b.name, i) for b in model.bones
             for i in range(len(b.displays))}
    result = {}
    for key, mode in values.items():
        if not isinstance(key, str) or key not in valid:
            raise ValueError(f'存在しないボーン/画像の設定: {key!r}')
        if mode not in SKIN_OVERRIDE_FLIPS:
            raise ValueError(f'不正な補正モード: {key!r}: {mode!r}')
        if mode != 'auto':
            result[key] = mode
    return result


def _compose_pose_uncached(model: ArmatureModel, movement: MovementDef, frame: float,
                           hidden: frozenset[str] = frozenset(),
                           hair_layer: str = 'original') -> list[PlacedSprite]:
    """Compute the original pose. Used during precomputation and for subframes."""
    by_name = {bone.name: bone for bone in model.bones}
    world: dict[str, CocosBoneWorld] = {}
    active: set[str] = set()
    placed: list[PlacedSprite] = []

    def resolve(bone: BoneDef, depth: int = 0) -> CocosBoneWorld:
        if bone.name in world:
            return world[bone.name]
        if depth > ARMATURE_BONE_LIMIT or bone.name in active:
            raise ValueError(f'ボーン階層に循環参照があります: {bone.name}')
        active.add(bone.name)
        state = frame_state(movement.timelines.get(bone.name, ()), frame)
        local = combined_bone_components(bone.rest, state)
        parent_bone = by_name.get(bone.parent)
        parent = resolve(parent_bone, depth+1) if parent_bone is not None else None
        resolved = cocos_parent_inherit(local, parent)
        active.remove(bone.name)
        world[bone.name] = resolved
        return resolved

    for bone in model.bones:
        transform = resolve(bone).matrix
        state = frame_state(movement.timelines.get(bone.name, ()), frame)
        display_index = int(state.get('dI', bone.display_index)) if state else bone.display_index
        if display_index < 0 or display_index >= len(bone.displays) or bone.name in hidden:
            continue
        display = bone.displays[display_index]
        if display.kind == 2:
            effect = model.particle_effects.get(Path(display.name).name.casefold())
            if effect is not None:
                start = _particle_emission_start(movement.timelines.get(bone.name, ()),
                                                 frame, bone.display_index)
                if start is not None:
                    z = bone.z + _numeric(state, 'z') if state else bone.z
                    particles = particle_placements(bone, effect, transform, frame, start, z)
                    frame_color = _color_for_state(state)
                    for particle in particles:
                        placed.append(PlacedSprite(
                            particle.bone_name, particle.image_name, particle.texture,
                            particle.image, particle.matrix, particle.z, particle.blend,
                            _color_multiply(particle.tint, frame_color)))
            continue
        if display.kind != 0:
            continue
        name = _normal_name(display.name)
        texture = model.textures.get(name)
        image = model.sprites.get(name)
        if texture is None:
            if image is None:
                continue
            texture = TextureDef(name, image.width, image.height, .5, .5)
        skin_matrix = skin_matrix_for_display(model, bone, display_index)
        final = matrix_mul(transform, skin_matrix)
        placed.append(PlacedSprite(bone.name, name, texture, image,
                                   final, bone.z + _numeric(state, 'z') if state else bone.z,
                                   _blend_for_state(state), _color_for_state(state)))
    placed.sort(key=lambda item: item.z)
    return adjust_hair_layer(placed, model.bones, hair_layer)


def precompute_pose_cache(model: ArmatureModel) -> tuple[int, int]:
    """Precompute EVERY integer animation frame once, including particles.

    Cache keys are (movement identity, frame number), so movements with identical
    display names cannot collide. The base pose is unhidden and source Z-ordered.
    Hair order and hidden bones are lightweight, reversible presentation filters.
    UV/screen direction, camera and zoom do not change any cached world matrix.
    All frames share existing Pillow images; no rendered bitmap is stored here.
    """
    beginning = time.perf_counter()
    # Clear any previous content first: overrides or newly loaded textures may
    # have changed world transforms / image references.
    model.pose_cache.clear()
    model.pose_cache_frames = 0
    model.pose_cache_sprites = 0
    model.pose_cache_seconds = 0.0
    cache: dict[int, dict[int, tuple[PlacedSprite, ...]]] = {}
    frame_count = 0
    sprite_count = 0
    try:
        for movement in model.movements:
            frame_map: dict[int, tuple[PlacedSprite, ...]] = {}
            for frame_index in range(movement.duration):
                pose = tuple(_compose_pose_uncached(model, movement, float(frame_index)))
                frame_map[frame_index] = pose
                frame_count += 1
                sprite_count += len(pose)
            cache[id(movement)] = frame_map
    except Exception:
        # Never retain a partially populated cache if loading was interrupted.
        cache.clear()
        raise
    model.pose_cache = cache
    model.pose_cache_frames = frame_count
    model.pose_cache_sprites = sprite_count
    model.pose_cache_seconds = time.perf_counter() - beginning
    return frame_count, sprite_count


def compose_pose(model: ArmatureModel, movement: MovementDef, frame: float,
                 hidden: frozenset[str] = frozenset(),
                 hair_layer: str = 'original') -> list[PlacedSprite]:
    """Retrieve an integer-frame pose in O(1), then apply display-only filters.

    A Tk slider can also produce fractional positions. Such subframes keep the
    original tween calculation, rather than silently rounding to a nearby pose.
    """
    frame_map = model.pose_cache.get(id(movement))
    # Use the same key as the animation frames (no quantization drift).
    base = None
    if frame_map is not None and isinstance(frame, (int, float)) and math.isfinite(frame):
        if frame == int(frame):
            base = frame_map.get(int(frame))
    if base is None:
        return _compose_pose_uncached(model, movement, frame, hidden, hair_layer)
    placed = ([part for part in base if part.bone_name not in hidden]
              if hidden else list(base))
    return adjust_hair_layer(placed, model.bones, hair_layer)


def sprite_size(item: PlacedSprite) -> tuple[int, int]:
    """Use the original size reconstructed from the atlas.

    JSON dimensions are used solely as a fallback for a missing image's
    diagnostic placeholder; they never resize an available atlas sprite.
    """
    return item.image.size if item.image is not None else (item.texture.width, item.texture.height)


def image_polygon(item: PlacedSprite) -> list[tuple[float,float]]:
    tex = item.texture
    width, height = sprite_size(item)
    left = -tex.pivot_x*width
    right = left + width
    bottom = -tex.pivot_y*height
    top = bottom + height
    return [matrix_apply(item.matrix, x,y) for x,y in
            ((left,top),(right,top),(right,bottom),(left,bottom))]


def alignment_diagnostics(model: ArmatureModel, movement: MovementDef,
                          frame: float = 0.0) -> str:
    """Describe image-space geometry without modifying the render transform.

    Cropping by the atlas is already undone by extract_sprite(). Thus, the
    normalized anchor must be applied to the restored *originalSize*. The
    ExportJson texture width/height must not independently shift that anchor.
    """
    lines = [f'アーマチュア: {model.name} / 動作: {movement.name} / フレーム: {frame:g}',
             '基準点: アトラスで復元した元画像サイズ × texture_data の pX / pY',
             'JSON の width/height は寸法比較用であり、位置オフセットには使用しません。', '']
    for part in compose_pose(model, movement, frame):
        source = part.image
        if source is None:
            lines.append(f'{part.bone_name} / {part.image_name}: 画像未検出')
            continue
        alpha_bounds = source.getchannel('A').getbbox()
        # Cocos local origin is bottom-left; alpha bounding box uses top-left.
        ax = source.width * part.texture.pivot_x
        ay = source.height * (1.0 - part.texture.pivot_y)
        if alpha_bounds:
            center_x = (alpha_bounds[0] + alpha_bounds[2]) / 2.0 - ax
            center_y = ay - (alpha_bounds[1] + alpha_bounds[3]) / 2.0
            delta = f'可視領域中心 - 基準点: ({center_x:+.2f}, {center_y:+.2f}) px'
        else:
            delta = '可視領域なし'
        world_x, world_y = matrix_apply(part.matrix, 0, 0)
        lines.append(
            f'{part.bone_name} / {part.image_name}: '
            f'アトラス元寸法={source.width}x{source.height}, '
            f'JSON寸法={part.texture.width}x{part.texture.height}, '
            f'アンカー=({part.texture.pivot_x:g},{part.texture.pivot_y:g}), '
            f'基準点ワールド座標=({world_x:.2f},{world_y:.2f}), {delta}'
        )
    return '\n'.join(lines)


def pose_bounds(model: ArmatureModel, movement: MovementDef) -> tuple[float,float,float,float]:
    # Stable camera framing while scrubbing an animation. Sample key frames plus
    # intermediate frames, without allocating/rendering any bitmap.
    positions: set[int] = {0, max(0,movement.duration-1)}
    for timeline in movement.timelines.values():
        positions.update(int(_numeric(frame,'fi')) for frame in timeline)
    for k in range(0,movement.duration, max(1,movement.duration//50)):
        positions.add(k)
    xs = [-16.0, 16.0]
    ys = [-16.0, 16.0]
    for frame in sorted(positions):
        if frame < 0 or frame >= movement.duration:
            continue
        for sprite in compose_pose(model, movement, frame):
            for x,y in image_polygon(sprite):
                xs.append(x)
                ys.append(y)
    x0,x1 = min(xs),max(xs)
    y0,y1 = min(ys),max(ys)
    return (x0-CANVAS_PADDING,y0-CANVAS_PADDING,x1+CANVAS_PADDING,y1+CANVAS_PADDING)


def _screen(m: tuple[float,...], w: int, h: int, scale: float,
            camera: tuple[float,float], flip_screen_y: bool = False) -> tuple[float,...]:
    """Map armature coordinates to Pillow's Y-down screen coordinates.

    Normal Cocos Y-up mode negates the entire Y row. The alternate
    flip_screen_y mode reverses that output Y direction, including all
    sprite positions and polygon overlays, independently from texture UVs.
    """
    a,b,c,d,x,y=m
    y_direction = 1.0 if flip_screen_y else -1.0
    return (scale*a, y_direction*scale*b, scale*c, y_direction*scale*d,
            (w/2 + scale*(x-camera[0])),
            (h/2 + y_direction*scale*(y-camera[1])))



def sprite_pixel_to_screen(item: PlacedSprite, width: int, height: int,
                           scale: float, camera: tuple[float, float],
                           flip_uv_y: bool = False, flip_screen_y: bool = False,
                           flip_uv_x: bool = False) -> tuple[float, ...]:
    """One canonical pixel-to-screen matrix, shared by CPU and GPU renderers."""
    if item.image is None:
        raise ValueError('スプライト画像がありません')
    src = item.image
    tex = item.texture
    render_width, render_height = sprite_size(item)
    u = render_width / src.width
    v = render_height / src.height
    texture_scale_x = -u if flip_uv_x else u
    texture_origin_x = (1.0 - tex.pivot_x) * render_width if flip_uv_x else -tex.pivot_x * render_width
    if flip_uv_y:
        texture_scale_y = v
        texture_origin_y = -tex.pivot_y * render_height
    else:
        texture_scale_y = -v
        texture_origin_y = (1.0 - tex.pivot_y) * render_height
    to_bone = (texture_scale_x, 0.0, 0.0, texture_scale_y,
               texture_origin_x, texture_origin_y)
    return _screen(matrix_mul(item.matrix, to_bone), width, height,
                   scale, camera, flip_screen_y)

def _tint_image(image: Image.Image, color: tuple[int, int, int, int]) -> Image.Image:
    """Modulate straight RGBA channels; the source image is never mutated."""
    if color == COLOR_WHITE:
        return image
    channels = image.split()
    return Image.merge('RGBA', tuple(
        ch.point([int((i * factor + 127) // 255) for i in range(256)])
        if factor != 255 else ch
        for ch, factor in zip(channels, color)))


def _cpu_blend_factor(code: int, channel: int,
                      src: tuple[Image.Image, ...],
                      dst: tuple[Image.Image, ...]) -> Image.Image:
    """8-bit representation of OpenGL's per-channel blend factors."""
    from PIL import ImageChops
    if code == 0:
        return Image.new('L', src[0].size, 0)
    if code == 1:
        return Image.new('L', src[0].size, 255)
    if code in (768, 769):
        v = src[channel]
        return ImageChops.invert(v) if code == 769 else v
    if code in (774, 775):
        v = dst[channel]
        return ImageChops.invert(v) if code == 775 else v
    if code in (770, 771):
        v = src[3]
        return ImageChops.invert(v) if code == 771 else v
    if code in (772, 773):
        v = dst[3]
        return ImageChops.invert(v) if code == 773 else v
    if code == 776 and channel != 3:
        return ImageChops.darker(src[3], ImageChops.invert(dst[3]))
    if code == 776 and channel == 3:
        return Image.new('L', src[0].size, 255)
    raise ValueError(f'ブレンド係数が不正です: {code}')


def blend_rgba_region(destination: Image.Image, source: Image.Image,
                      blend: tuple[int, int]) -> Image.Image:
    """Compose a sprite crop with GL_FUNC_ADD factor rules, using Pillow only.

    Source ONE denotes premultiplied input, so premultiply the straight PIL
    pixels in the CPU path (the GPU shader does exactly the same).
    """
    if destination.size != source.size:
        raise ValueError('ブレンド画像の寸法が異なります')
    if blend == BLEND_DEFAULT or blend == (1, 771):
        # Correct straight-alpha source-over for a transparent PNG surface.
        out = destination.copy()
        out.alpha_composite(source)
        return out
    ss = source.split()
    dd = destination.split()
    if blend[0] == 1:
        ss = tuple(ImageChops.multiply(ss[i], ss[3]) if i < 3 else ss[i]
                   for i in range(4))
    out = []
    for channel in range(4):
        sf = _cpu_blend_factor(blend[0], channel, ss, dd)
        df = _cpu_blend_factor(blend[1], channel, ss, dd)
        sc = ImageChops.multiply(ss[channel], sf)
        dc = ImageChops.multiply(dd[channel], df)
        out.append(ImageChops.add(sc, dc))
    return Image.merge('RGBA', tuple(out))


def _draw_affine_sprite(target: Image.Image, item: PlacedSprite,
                        scale: float, camera: tuple[float,float],
                        flip_uv_y: bool = False,
                        flip_screen_y: bool = False,
                        flip_uv_x: bool = False) -> None:
    if item.image is None:
        return
    tw, th = target.size
    src = item.image
    a, b, c, d, tx, ty = sprite_pixel_to_screen(
        item, tw, th, scale, camera, flip_uv_y, flip_screen_y,
        flip_uv_x)
    det = a*d-b*c
    if abs(det) < 1e-10:
        return
    polygon = [(a*x+c*y+tx,b*x+d*y+ty) for x,y in
               ((0,0),(src.width,0),(src.width,src.height),(0,src.height))]
    x0=max(0,math.floor(min(x for x,_ in polygon)) - 1)
    y0=max(0,math.floor(min(y for _,y in polygon)) - 1)
    x1=min(tw,math.ceil(max(x for x,_ in polygon)) + 1)
    y1=min(th,math.ceil(max(y for _,y in polygon)) + 1)
    if x0>=x1 or y0>=y1:
        return
    aa=d/det; bb=-c/det; dd=-b/det; ee=a/det
    # Pillow inverse mapping: output-to-source (source x, source y).
    coeffs=(aa,bb,-aa*tx-bb*ty+aa*x0+bb*y0,
            dd,ee,-dd*tx-ee*ty+dd*x0+ee*y0)
    small = src.transform((x1-x0,y1-y0), IMAGE_TRANSFORM_AFFINE,
                          coeffs, resample=RESAMPLE_BICUBIC)
    small = _tint_image(small, item.tint)
    if item.blend == BLEND_DEFAULT or item.blend == (1, 771):
        target.alpha_composite(small, (x0, y0))
    else:
        from PIL import ImageDraw
        old = target.crop((x0, y0, x1, y1))
        result = blend_rgba_region(old, small, item.blend)
        # Outside the transformed sprite quad must remain untouched even for
        # blend functions whose destination factor is zero.
        mask = Image.new('L', old.size, 0)
        ImageDraw.Draw(mask).polygon([(int(round(x - x0)), int(round(y - y0)))
                                     for x, y in polygon], fill=255)
        target.paste(result, (x0, y0), mask)


def render_armature(model: ArmatureModel, movement: MovementDef, frame: float,
                    width: int, height: int, scale: float, camera: tuple[float,float],
                    show_bounds: bool = False, selected: Optional[str] = None,
                    hidden: frozenset[str] = frozenset(),
                    flip_uv_y: bool = False,
                    flip_screen_y: bool = False,
                    flip_uv_x: bool = False,
                    hair_layer: str = 'original',
                    opaque_background: bool = False) -> tuple[Image.Image,list[tuple[str,list[tuple[float,float]]]]]:
    if width<=0 or height<=0 or width*height>RENDER_PIXEL_LIMIT:
        raise ValueError('描画領域のサイズが上限を超えています')
    # Opaque GUI output matches the GPU framebuffer for additive sprites.
    # PNG export preserves the transparent background by default.
    bg = CANVAS_BACKGROUND + (255,) if opaque_background else (0,0,0,0)
    output = Image.new('RGBA', (width,height), bg)
    placed = compose_pose(model, movement, frame, hidden, hair_layer)
    polys = []
    for item in placed:
        _draw_affine_sprite(output,item,scale,camera,
                            flip_uv_y=flip_uv_y,flip_screen_y=flip_screen_y,
                            flip_uv_x=flip_uv_x)
    overlay = ImageDraw.Draw(output,'RGBA')
    y_direction = 1.0 if flip_screen_y else -1.0
    for item in placed:
        poly = [(width/2 + (x-camera[0])*scale,
                 height/2 + y_direction*(y-camera[1])*scale) for x,y in image_polygon(item)]
        polys.append((item.bone_name,poly))
        if show_bounds or selected==item.bone_name:
            color = ENGINE_SELECTION_COLOR if selected==item.bone_name else (MISSING_COLOR if item.image is None else CORNERS_COLOR)
            overlay.line(poly+[poly[0]], fill=color, width=3 if selected==item.bone_name else 1)
            if show_bounds or selected==item.bone_name:
                cx=sum(p[0] for p in poly)/4
                cy=sum(p[1] for p in poly)/4
                overlay.text((cx+4,cy+4),item.bone_name,fill=color)
    return output, polys


# ---- GPU implementation (included in this file; no sidecar .py needed) ----
"""Integrated ModernGL renderer for the standalone viewer.

The caller owns pose evaluation and the Tk window. This module draws sprite
quadrilaterals using OpenGL, not Pillow's per-sprite affine rasterization.
The framebuffer is read back for Tkinter display (the unavoidable CPU copy in
this mixed Tk/GL design). The static atlas sprites are uploaded only once.
Requires moderngl 5.8+ and a working OpenGL 3.3 context.
"""

from collections import OrderedDict
from struct import pack
from typing import Any, Callable, Iterable, Optional

from PIL import Image

# Distinct cap from Pillow atlas validation; bounds the GPU upload cache.
MAX_GPU_CACHE_BYTES = 256 * 1024 * 1024
MAX_GPU_VIEW_PIXELS = 12_000_000
RGBA_COMPONENTS = 4
CANVAS_BACKGROUND = (36, 40, 51)

VERTEX_SHADER = '''#version 330
in vec2 in_position;
in vec2 in_uv;
out vec2 uv;
void main() {
    gl_Position = vec4(in_position, 0.0, 1.0);
    uv = in_uv;
}
'''
FRAGMENT_SHADER = '''#version 330
uniform sampler2D tex;
uniform vec4 sprite_color;
uniform bool premultiply_source;
in vec2 uv;
out vec4 output_color;
void main() {
    vec4 texel = texture(tex, uv) * sprite_color;
    if (premultiply_source) {
        texel.rgb *= texel.a;
    }
    output_color = texel;
}
'''


def quad_vertices(affine: tuple[float, ...], width: int, height: int,
                  viewport_width: int, viewport_height: int) -> bytes:
    """Create OpenGL NDC vertices from Pillow-screen (x right, y down).

    Sprite UV row 0 is the first PIL image row. Raw top-to-bottom PIL rows are
    uploaded as OpenGL data; OpenGL's v=0 accesses that first supplied row.
    """
    a, b, c, d, tx, ty = affine
    vertices = []
    for px, py, u, v in (
        (0.0, 0.0, 0.0, 0.0),
        (float(width), 0.0, 1.0, 0.0),
        (0.0, float(height), 0.0, 1.0),
        (float(width), float(height), 1.0, 1.0),
    ):
        sx = a * px + c * py + tx
        sy = b * px + d * py + ty
        vertices.extend((2.0 * sx / viewport_width - 1.0,
                         1.0 - 2.0 * sy / viewport_height, u, v))
    return pack('16f', *vertices)


class GPUArmatureRenderer:
    """ModernGL off-screen renderer; all GL calls must run on one GUI thread."""

    def __init__(self, max_cache_bytes: int = MAX_GPU_CACHE_BYTES) -> None:
        try:
            import moderngl
        except ImportError as exc:
            raise RuntimeError('ModernGL がありません。pip install moderngl を実行してください。') from exc
        self.gl = moderngl
        self.ctx = moderngl.create_standalone_context(require=330)
        self.program = self.ctx.program(vertex_shader=VERTEX_SHADER,
                                        fragment_shader=FRAGMENT_SHADER)
        self.program['tex'].value = 0
        self.program['sprite_color'].value = (1.0, 1.0, 1.0, 1.0)
        self.program['premultiply_source'].value = False
        self.vertex_buffer = self.ctx.buffer(reserve=16 * 4, dynamic=True)
        self.vao = self.ctx.vertex_array(
            self.program, [(self.vertex_buffer, '2f 2f', 'in_position', 'in_uv')]
        )
        self.ctx.enable(moderngl.BLEND)
        # The blend function is reconfigured per sprite from Cocos Studio keys.
        self.ctx.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)
        self._current_blend = BLEND_DEFAULT
        self._gpu_textures: OrderedDict[tuple[str, int], tuple[Any, int]] = OrderedDict()
        self._gpu_bytes = 0
        self._cache_limit = max(4 * 1024 * 1024, int(max_cache_bytes))
        self._render_texture: Optional[Any] = None
        self._framebuffer: Optional[Any] = None
        self._viewport_size = (0, 0)
        self._closed = False

    @property
    def cached_textures(self) -> int:
        return len(self._gpu_textures)

    def clear_textures(self) -> None:
        for texture, _size in self._gpu_textures.values():
            texture.release()
        self._gpu_textures.clear()
        self._gpu_bytes = 0

    def _texture_for(self, name: str, image: Image.Image) -> Any:
        key = (name, id(image))
        entry = self._gpu_textures.get(key)
        if entry is not None:
            self._gpu_textures.move_to_end(key)
            return entry[0]
        size_bytes = image.width * image.height * RGBA_COMPONENTS
        if size_bytes > self._cache_limit:
            raise ValueError(f'GPU キャッシュの制限を超える画像です: {name}')
        while self._gpu_textures and self._gpu_bytes + size_bytes > self._cache_limit:
            _, (expired, allocated) = self._gpu_textures.popitem(last=False)
            expired.release()
            self._gpu_bytes -= allocated
        texture = self.ctx.texture((image.width, image.height), RGBA_COMPONENTS,
                                   image.convert('RGBA').tobytes())
        texture.filter = (self.gl.LINEAR, self.gl.LINEAR)
        texture.repeat_x = False
        texture.repeat_y = False
        self._gpu_textures[key] = (texture, size_bytes)
        self._gpu_bytes += size_bytes
        return texture

    def _resize_viewport(self, width: int, height: int) -> None:
        if self._viewport_size == (width, height):
            return
        if self._framebuffer is not None:
            self._framebuffer.release()
            self._framebuffer = None
        if self._render_texture is not None:
            self._render_texture.release()
            self._render_texture = None
        self._render_texture = self.ctx.texture((width, height), RGBA_COMPONENTS)
        self._render_texture.filter = (self.gl.NEAREST, self.gl.NEAREST)
        self._framebuffer = self.ctx.framebuffer(color_attachments=[self._render_texture])
        self._viewport_size = (width, height)

    def render(self, items: Iterable[Any], width: int, height: int,
               transform: Callable[[Any, int, int], tuple[float, ...]]) -> Image.Image:
        """Draw sprites in their supplied Z order using a CPU-defined affine.

        `transform(item, w, h)` supplies the exact same sprite-pixel-to-screen
        affine as the Pillow renderer. GPU does projection and sampling.
        """
        if self._closed:
            raise RuntimeError('GPU レンダラーは解放済みです')
        if width <= 0 or height <= 0 or width * height > MAX_GPU_VIEW_PIXELS:
            raise ValueError('GPU 描画サイズが範囲外です')
        self._resize_viewport(width, height)
        framebuffer = self._framebuffer
        framebuffer.use()
        r, g, b = CANVAS_BACKGROUND
        framebuffer.clear(r / 255.0, g / 255.0, b / 255.0, 1.0)
        # Reset state at the start of every render (not only on construction).
        self._current_blend = BLEND_DEFAULT
        self.ctx.blend_func = (self.gl.SRC_ALPHA, self.gl.ONE_MINUS_SRC_ALPHA)
        for item in items:
            sprite = item.image
            if sprite is None:
                continue
            affine = transform(item, width, height)
            a, b, c, d, _, _ = affine
            if abs(a * d - b * c) < 1e-10:
                continue
            pair = item.blend
            if pair[0] == 776 or pair[1] == 776:
                raise ValueError('ModernGL は SRC_ALPHA_SATURATE を扱えないため CPU 描画へ切り替えます')
            if pair != self._current_blend:
                self.ctx.blend_func = (getattr(self.gl, BLEND_FACTOR_NAMES[pair[0]]),
                                       getattr(self.gl, BLEND_FACTOR_NAMES[pair[1]]))
                self._current_blend = pair
            self.program['sprite_color'].value = tuple(v / 255.0 for v in item.tint)
            self.program['premultiply_source'].value = pair[0] == 1
            texture = self._texture_for(item.image_name, sprite)
            self.vertex_buffer.write(quad_vertices(affine, sprite.width, sprite.height,
                                                   width, height))
            texture.use(location=0)
            self.vao.render(mode=self.gl.TRIANGLE_STRIP, vertices=4)
        # Readback is required by Tk PhotoImage. OpenGL bytes begin at bottom.
        pixels = framebuffer.read(components=RGBA_COMPONENTS, alignment=1)
        transpose = getattr(Image, 'Transpose', Image)
        return Image.frombytes('RGBA', (width, height), pixels).transpose(transpose.FLIP_TOP_BOTTOM)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.clear_textures()
        if self._framebuffer is not None:
            self._framebuffer.release()
        if self._render_texture is not None:
            self._render_texture.release()
        self.vao.release()
        self.vertex_buffer.release()
        self.program.release()
        self.ctx.release()




# ==== シーンUI編集・プレビュー・JSON保存処理 ====
#!/usr/bin/env python3
"""16:9 Cocos Studio UI scene editor (Python 3.9+, Tkinter + Pillow).

Uses the reference viewer's validated TexturePacker/Cocos Studio decoder.
Scene coordinates: center=(0,0), +X right, +Y up, design size 1920x1080.
GUI rotation: counterclockwise in degrees; scene JSON uses rotate in radians (Y-up).
Scene assets support independent flip_x/flip_y flags (JSON booleans).
"""

import copy
import hashlib
import json
import math
import os
import re
import sys
import time
import tkinter as tk
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Optional

from PIL import Image, ImageOps, ImageTk, UnidentifiedImageError

import ctypes
import queue
import shutil
import subprocess
import tempfile
import threading
import wave
import array


APP_NAME = "シーン UI エディター"
DESIGN_W = 1920
DESIGN_H = 1080
ASPECT = DESIGN_W / DESIGN_H
DEFAULT_SCENE = "home"
DEFAULT_FPS = 60.0       # Source Cocos Studio animation frame rate.
TICK_MILLISECONDS = 33   # GUI preview sampling, approx. 30 refreshes/second.
MIN_SCALE = 0.02
MAX_SCALE = 32.0
MAX_ASSETS = 1000
UNDO_MAX_STEPS = 60
DRAG_TRANSFORM_INTERVAL = 1.0 / 30.0  # Cap expensive resampling to approximately 30 FPS.
MAX_IMAGE_PIXELS = 40_000_000
MAX_RENDER_PIXELS = 7_000_000
MAX_SCENE_JSON_BYTES = 8_000_000
HANDLE_RADIUS = 5
HANDLE_HIT_RADIUS = 12
ROTATION_HANDLE_DISTANCE = 34
SNAP_DISTANCE_PX = 9.0  # Screen-space magnet range; consistent at different editor sizes.
SNAP_GUIDE_COLOR = "#ff5cb8"
SNAP_GUIDE_DASH = (5, 3)
BG_COLOR = (32, 37, 48, 255)
SELECTION_COLOR = "#ffbb55"
OUTLINE_COLOR = "#53a9ef"
NUMBER_MAX = 1_000_000.0
AUDIO_EXTENSIONS = frozenset((".wav", ".mp3", ".ogg", ".hca"))
AUDIO_MAX_DECODE_SECONDS = 900  # One stream, 15 min maximum preview length.
AUDIO_DECODE_TIMEOUT = 120
AUDIO_MAX_DECODED_BYTES = 200 * 1024 * 1024
AUDIO_SE_VOICE_LIMIT = 8
AUDIO_POLL_MS = 150
VGMSTREAM_RELATIVE_EXE = Path("vgmstream-win64") / "vgmstream-cli.exe"
TOOL_DIR = Path(__file__).resolve().parent
ASSET_ROOT = (TOOL_DIR / "assets").resolve()
SCENE_OUTPUT_DIR = TOOL_DIR / "out" / "scene"
AUDIO_MANAGER_SIZE = (720, 450)
AUDIO_ADD_SIZE = (400, 215)
UV_JSON_DIGITS = 10  # normalized UV coordinates; enough for pixel-precise round trips

RESAMPLE = Image.Resampling.BICUBIC if hasattr(Image, "Resampling") else Image.BICUBIC


def safe_scene_name(value: str) -> str:
    name = value.strip()
    if (not name or name in (".", "..") or len(name) > 120
            or "/" in name or "\\" in name or "\x00" in name
            or any(char in name for char in '<>:"|?*') or name.endswith(".")
            or name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL",
                *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}
            or any(ord(ch) < 32 for ch in name)):
        raise ValueError("シーン名に使用できない文字が含まれています。")
    return name


def scene_output_path(scene_name: str) -> Path:
    """Return the sole permitted scene destination, relative to the editor."""
    name = safe_scene_name(scene_name)
    return SCENE_OUTPUT_DIR / f"{name}.json"


def prepare_scene_output_dir() -> Path:
    """Create the output directory and reject symlinks leading outside the tool."""
    SCENE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    resolved = SCENE_OUTPUT_DIR.resolve()
    try:
        resolved.relative_to(TOOL_DIR)
    except ValueError as exc:
        raise ValueError("シーン保存先 ./out/scene がツールの外側を参照しています。") from exc
    return resolved


def safe_asset_key(value: str) -> str:
    name = value.strip()
    if (not name or len(name) > 128 or not re.fullmatch(r"[\w.\-]+", name, flags=re.UNICODE)):
        raise ValueError("アセット名は英数字・日本語・アンダースコア・ハイフン・ピリオドで指定してください。")
    return name


def checked_float(value: Any, name: str, *, min_value: float = -NUMBER_MAX,
                  max_value: float = NUMBER_MAX) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} は数値を指定してください。")
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} は数値を指定してください。") from exc
    if not math.isfinite(num) or not min_value <= num <= max_value:
        raise ValueError(f"{name} が有効範囲外です。")
    return num


def jnum(value: float) -> str:
    result = f"{value:.6f}".rstrip("0").rstrip(".")
    return ("0" if result == "-0" else result) + (".0" if "." not in result else "")


def json_number(value: float) -> float:
    """Finite JSON number, never a quoted numeric string; round to 6 decimals."""
    return float(jnum(checked_float(value, "JSONの数値")))


def scene_number(value: Any, name: str, *, strict_numeric: bool = False,
                 min_value: float = -NUMBER_MAX, max_value: float = NUMBER_MAX) -> float:
    """Schema 1 requires JSON numbers; legacy schema 3 may carry numeric strings."""
    if strict_numeric and type(value) not in (int, float):
        raise ValueError(f"{name} は文字列ではなくJSON数値にしてください。")
    return checked_float(value, name, min_value=min_value, max_value=max_value)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_asset_path(path: Path) -> Path:
    """Accept only paths below the tool's assets directory (including symlink targets)."""
    candidate = path.expanduser().resolve()
    try:
        relative = candidate.relative_to(ASSET_ROOT)
    except ValueError as exc:
        raise ValueError(
            f"アセットはツールと同じ階層の assets フォルダー配下から選択してください。\n"
            f"対象: {candidate}\n配置先: {ASSET_ROOT}"
        ) from exc
    if not relative.parts:
        raise ValueError("アセットには assets フォルダー内のファイルを指定してください。")
    return candidate


def reference_name(path: Path, destination_dir: Optional[Path] = None) -> str:
    """Emit a forward-slash, tool-relative ./assets/... reference, never scene-relative."""
    del destination_dir  # Kept for compatibility with the prior function signature.
    validated = ensure_asset_path(path)
    return "./assets/" + validated.relative_to(ASSET_ROOT).as_posix()


def resolve_reference(ref: str, scene_dir: Optional[Path] = None) -> Path:
    """Resolve references using the tool directory, not the scene JSON directory.

    Earlier scene files may contain paths relative to their JSON directory. Import
    those *only* if the path already resolves into the tool's assets folder;
    subsequent saves normalize all of them to ./assets/... .
    """
    if not isinstance(ref, str) or not ref or len(ref) > 4096 or "\x00" in ref:
        raise ValueError("ファイル参照が不正です。")
    from pathlib import PureWindowsPath
    normalized = ref.replace("\\", "/")
    if PureWindowsPath(ref).drive or normalized.startswith("/"):
        # Legacy absolute paths are only tolerated when they reside in assets.
        if not Path(ref).is_absolute():
            raise ValueError(f"絶対パスは使用できません: {ref}")
        return ensure_asset_path(Path(ref))
    parts = Path(normalized).parts
    if not parts or parts[0] in (".", "") and len(parts) <= 1:
        raise ValueError(f"アセット参照が不正です: {ref}")
    if parts[0] == "assets":
        if ".." in parts:
            raise ValueError(f"assets 配下からの相対パスに .. は指定できません: {ref}")
        return ensure_asset_path(TOOL_DIR / Path(normalized))
    # Legacy scene-relative paths: do not allow access outside ./assets.
    if scene_dir is not None:
        return ensure_asset_path(scene_dir / Path(normalized))
    raise ValueError(f"ツール基準の ./assets/... を指定してください: {ref}")


def uv_rect_to_json(rect: tuple[int, int, int, int], image_size: tuple[int, int]) -> dict[str, float]:
    """Normalized 0..1 UV coordinates, top-left origin; pixel rectangle is (x, y, w, h)."""
    width, height = image_size
    x, y, w, h = validate_uv_rect(rect, image_size)
    return {
        "u0": round(x / width, UV_JSON_DIGITS),
        "v0": round(y / height, UV_JSON_DIGITS),
        "u1": round((x + w) / width, UV_JSON_DIGITS),
        "v1": round((y + h) / height, UV_JSON_DIGITS),
    }


def validate_uv_rect(rect: tuple[int, int, int, int],
                     image_size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Half-open integer pixel region, fully within the original source image."""
    if not isinstance(rect, (tuple, list)) or len(rect) != 4:
        raise ValueError("UV範囲は (X, Y, 幅, 高さ) の4整数で指定してください。")
    width, height = image_size
    if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("画像のサイズが範囲外です。")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in rect):
        raise ValueError("UV範囲は整数ピクセルで指定してください。")
    x, y, w, h = rect
    if x < 0 or y < 0 or w < 1 or h < 1 or x + w > width or y + h > height:
        raise ValueError(f"UV範囲が画像 {width}×{height} の内側に収まりません。")
    return (x, y, w, h)


def uv_json_to_rect(raw: Any, image_size: tuple[int, int], *,
                    strict_numeric: bool = False) -> tuple[int, int, int, int]:
    """Read normalized UV values with strict validation before converting to pixels."""
    if not isinstance(raw, dict) or set(raw) != {"u0", "v0", "u1", "v1"}:
        raise ValueError("uv には u0, v0, u1, v1 を指定してください。")
    values = {key: scene_number(raw[key], f"uv.{key}", strict_numeric=strict_numeric,
                                min_value=0.0, max_value=1.0)
              for key in ("u0", "v0", "u1", "v1")}
    if values["u1"] <= values["u0"] or values["v1"] <= values["v0"]:
        raise ValueError("uv の終点は始点より大きくしてください。")
    width, height = image_size
    x0 = round(values["u0"] * width)
    y0 = round(values["v0"] * height)
    x1 = round(values["u1"] * width)
    y1 = round(values["v1"] * height)
    return validate_uv_rect((x0, y0, x1 - x0, y1 - y0), image_size)


def png_uv_state(path: Path, rect: Optional[tuple[int, int, int, int]]) -> dict[str, Any]:
    """Serialize image state as file+normalized UV, even for full image."""
    path = ensure_asset_path(path)
    if path.suffix.casefold() not in (".png", ".jpg", ".jpeg"):
        raise ValueError(f"ボタン状態の画像はPNG/JPGで指定してください: {path}")
    with Image.open(path) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("画像の画素数が上限を超えています。")
        bounds = rect or (0, 0, *image.size)
        return {"file": reference_name(path),
                "uv": uv_rect_to_json(validate_uv_rect(bounds, image.size), image.size)}


def generated_sprite_png(image: Image.Image) -> Path:
    """Materialize a trimmed/rotated plist sprite as a standalone state texture.

    A plist sprite key cannot be represented by file+UV alone (trim offsets and
    packed rotations lose information). Save the restored image instead.
    """
    rgba = image.convert("RGBA")
    identifier = hashlib.sha256(f"{rgba.width}x{rgba.height}:".encode("ascii") +
                                rgba.tobytes()).hexdigest()[:32]
    directory = ASSET_ROOT / ".scene_generated"
    directory.mkdir(parents=True, exist_ok=True)
    ensure_asset_path(directory / f"{identifier}.png")
    target = directory / f"{identifier}.png"
    if not target.is_file():
        temporary = directory / f".{identifier}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("wb") as output:
                rgba.save(output, format="PNG")
                output.flush()
                os.fsync(output.fileno())
            # Content address means identical images share a single output.
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()
    return ensure_asset_path(target)


def atlas_sprite_state(path: Path, key: str) -> dict[str, Any]:
    """Represent a plist sprite as an extracted PNG with full 0..1 UV."""
    atlas = load_atlas(ensure_asset_path(path))
    frame = next((item for item in atlas.frames if item.name == key), None)
    if frame is None:
        raise ValueError(f"plistスプライトがありません: {key}")
    sprite = extract_sprite(atlas.image, frame)
    return png_uv_state(generated_sprite_png(sprite), None)


WINDOW_MODES = ("repeat", "stretch")
# Previous builds supported an "opaque" mode that silently promoted every
# nonzero source alpha to 255. Keep its name solely for recognizing older JSON;
# all window drawing now always preserves the source image's alpha values.
WINDOW_ALPHA_MODES = ("source", "opaque")
WINDOW_MAX_SIDE = 8192


def validate_window_spec(source_size: tuple[int, int], width: int, height: int,
                         borders: tuple[int, int, int, int], mode: str) -> None:
    sw, sh = source_size
    if mode not in WINDOW_MODES:
        raise ValueError("ウィンドウの描画方式は repeat または stretch で指定してください。")
    if (type(width) is not int or type(height) is not int or
            not (1 <= width <= WINDOW_MAX_SIDE and 1 <= height <= WINDOW_MAX_SIDE)):
        raise ValueError("ウィンドウの幅・高さが有効範囲外です。")
    if not isinstance(borders, tuple) or len(borders) != 4 or any(type(n) is not int for n in borders):
        raise ValueError("分割幅は左・右・上・下の整数ピクセルで指定してください。")
    l, r, t, b = borders
    if min(l, r, t, b) < 1 or l + r >= sw or t + b >= sh:
        raise ValueError("分割幅は正の値とし、元画像の中央領域を1px以上確保してください。")
    if width < l + r + 1 or height < t + b + 1:
        raise ValueError("ウィンドウサイズは四隅の合計と中央1px以上を確保してください。")


def render_nine_slice(source: Image.Image, width: int, height: int,
                      borders: tuple[int, int, int, int], mode: str = "repeat",
                      alpha_mode: str = "source") -> Image.Image:
    """Render a nine-slice while preserving each source pixel's RGBA.

    ``alpha_mode`` is retained for old callers only. Even if an older scene
    supplies "opaque", we never modify the image's alpha channel. Nine disjoint
    cells are pasted once, then composited against the scene only once.
    """
    validate_window_spec(source.size, width, height, borders, mode)
    if alpha_mode not in WINDOW_ALPHA_MODES:
        raise ValueError("ウィンドウのアルファ設定は opaque / source を指定してください。")
    if width * height > MAX_RENDER_PIXELS:
        raise ValueError("9スライス描画のピクセル数が上限を超えています。")
    sw, sh = source.size
    left, right, top, bottom = borders
    sx = (0, left, sw - right, sw)
    sy = (0, top, sh - bottom, sh)
    dx = (0, left, width - right, width)
    dy = (0, top, height - bottom, height)
    result = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    # Always preserve the actual RGBA channel, including palette PNG tRNS data.
    # No flattening, alpha normalization, or mask-based re-pasting is allowed.
    texture = source.convert("RGBA")
    for row in range(3):
        for col in range(3):
            dw, dh = dx[col+1] - dx[col], dy[row+1] - dy[row]
            tile = texture.crop((sx[col], sy[row], sx[col+1], sy[row+1]))
            tw, th = tile.size
            if mode == "stretch" and (col == 1 or row == 1):
                part = tile.resize((dw, dh), RESAMPLE_NEAREST)
                # The nine destination cells never overlap: write straight-RGBA
                # pixels exactly once, then alpha-composite the finished window
                # against the scene. Avoid intermediate blend/rounding passes.
                result.paste(part, (dx[col], dy[row]))
            elif col in (0, 2) and row in (0, 2):
                result.paste(tile, (dx[col], dy[row]))
            else:
                # Repeat at native resolution and crop only incomplete end tiles.
                # Tile with doubling rather than per-pixel Python iterations.
                first_w, first_h = min(dw, tw), min(dh, th)
                area = Image.new("RGBA", (dw, dh), (0, 0, 0, 0))
                area.paste(tile.crop((0, 0, first_w, first_h)), (0, 0))
                filled_w = first_w
                while filled_w < dw:
                    chunk = min(filled_w, dw - filled_w)
                    area.paste(area.crop((0, 0, chunk, first_h)), (filled_w, 0))
                    filled_w += chunk
                filled_h = first_h
                while filled_h < dh:
                    chunk = min(filled_h, dh - filled_h)
                    area.paste(area.crop((0, 0, dw, chunk)), (0, filled_h))
                    filled_h += chunk
                result.paste(area, (dx[col], dy[row]))
    return result


def read_asset_flip(record: Mapping[str, Any], key: str, label: str) -> bool:
    """Read only JSON booleans; strings such as "false" must not mean True."""
    value = record.get(key, False)
    if type(value) is not bool:
        raise ValueError(f"{label}.{key}: true / false の真偽値が必要です。")
    return value


def apply_scene_flips(image: Image.Image, asset: "Asset") -> Image.Image:
    """Flip around the asset's local center, prior to its scene rotation.

    Work on transformed display rasters rather than mutating the underlying
    texture, UV crop, atlas, animation frames, or 9-slice geometry. Pillow's
    transpositions keep exact RGBA channel values.
    """
    if asset.flip_x:
        image = ImageOps.mirror(image)
    if asset.flip_y:
        image = ImageOps.flip(image)
    return image


def read_new_scene_assets(resources: Mapping[str, Any], critical: set[str],
                          prefetch: set[str], scene_dir: Path, *, strict_numeric: bool = False
                          ) -> tuple[list[Asset], list[str]]:
    """Read state-nested schema with optional strict JSON numeric validation."""
    ui = resources.get("ui", {})
    if (not isinstance(ui, dict) or not {"image", "button"}.issubset(ui)
            or not set(ui).issubset({"image", "button", "toggle button", "window"})):
        raise ValueError("ui.image / ui.button / ui['toggle button'] の構造が必要です。")
    animation = resources.get("animation", {})
    if not isinstance(animation, dict):
        raise ValueError("animation はオブジェクトにしてください。")
    assets: list[Asset] = []
    warnings: list[str] = []
    for group in ("image", "button", "toggle button", "window", "animation"):
        items = animation if group == "animation" else ui.get(group, {})
        if not isinstance(items, dict):
            raise ValueError(f"ui.{group} はオブジェクトにしてください。")
        for key, record in items.items():
            if len(assets) >= MAX_ASSETS:
                raise ValueError("シーンのアセット数が上限を超えています。")
            safe_asset_key(key)
            label = f"animation.{key}" if group == "animation" else f"ui.{group}.{key}"
            if not isinstance(record, dict):
                raise ValueError(f"{label}: オブジェクトが必要です。")
            if "rx" in record or "ry" in record:
                raise ValueError(f"{label}: 回転はrotate（ラジアン）のみ対応しています。")
            if group in ("button", "toggle button"):
                toggle = group == "toggle button"
                default_state = record.get("default") if toggle else "off"
                if toggle and (not isinstance(default_state, str) or default_state not in ("off", "on")):
                    raise ValueError(f"{label}.default は off / on のどちらかを指定してください。")
                idle_name, push_name = ("off", "on") if toggle else ("idle", "on push")
                states = record.get("state")
                if not isinstance(states, dict) or set(states) != {idle_name, push_name}:
                    raise ValueError(f"{label}.state: {idle_name} / {push_name} の2状態が必要です。旧形式は変換ツールを使用してください。")
                if any(name in record for name in ("idle", "on push", "off", "on")):
                    raise ValueError(f"{label}: 状態情報はstate内にのみ記載してください。")
                idle, push = states[idle_name], states[push_name]
                for state_name, state in ((idle_name, idle), (push_name, push)):
                    if not isinstance(state, dict) or set(state) != {"file", "uv"}:
                        raise ValueError(f"{label}.state.{state_name}: file と uv のみを指定してください。")
                source = resolve_reference(idle["file"], scene_dir)
                pressed_source = resolve_reference(push["file"], scene_dir)
                if source.suffix.casefold() not in (".png", ".jpg", ".jpeg") or \
                        pressed_source.suffix.casefold() not in (".png", ".jpg", ".jpeg"):
                    raise ValueError(f"{label}: ボタン状態にはPNG/JPGが必要です。")
                with Image.open(source) as image:
                    uv_rect = uv_json_to_rect(idle["uv"], image.size, strict_numeric=strict_numeric)
                with Image.open(pressed_source) as image:
                    pressed_uv_rect = uv_json_to_rect(push["uv"], image.size, strict_numeric=strict_numeric)
                kind = "toggle_uv" if toggle else "button_uv"
                sprite_key = ""
            else:
                source = resolve_reference(record.get("file"), scene_dir)
                if group == "window" and source.suffix.casefold() not in (".png", ".jpg", ".jpeg"):
                    raise ValueError(f"{label}: ウィンドウにはPNG/JPGを指定してください。")
                pressed_source = None
                pressed_uv_rect = None
                kind = group
                sprite_key = record.get("key", "") if group == "animation" else ""
                if group == "animation" and (not isinstance(sprite_key, str) or not sprite_key):
                    raise ValueError(f"{label}: アニメーションのkeyが必要です。")
                if group == "image" and source.suffix.casefold() not in (".png", ".jpg", ".jpeg"):
                    raise ValueError(f"{label}: 画像はPNG/JPGで指定してください。")
                uv_rect = None
                if group == "image" and "uv" in record:
                    with Image.open(source) as image:
                        uv_rect = uv_json_to_rect(record["uv"], image.size, strict_numeric=strict_numeric)
            audio_link = record.get("se") if group in ("button", "toggle button", "animation") else None
            if audio_link is not None:
                audio_link = safe_asset_key(audio_link)
                if audio_link.startswith("se."):
                    raise ValueError(f"{label}.se はSE名だけを指定してください（se.は不要です）。")
            asset = Asset(name=key, kind=kind, source=source, key=sprite_key,
                uv_rect=uv_rect, pressed_uv_rect=pressed_uv_rect,
                pressed_source=pressed_source,
                button_se=audio_link if group in ("button", "toggle button") else None,
                toggle_default=default_state if group == "toggle button" else "off",
                toggle_group=(validated_toggle_group_name(record["group"], label)
                              if group == "toggle button" and "group" in record else None),
                animation_se=audio_link if group == "animation" else None,
                flip_x=read_asset_flip(record, "flip_x", label),
                flip_y=read_asset_flip(record, "flip_y", label),
                px=scene_number(record.get("px", 0), f"{label}.px", strict_numeric=strict_numeric),
                py=scene_number(record.get("py", 0), f"{label}.py", strict_numeric=strict_numeric),
                sx=scene_number(record.get("sx", 1), f"{label}.sx", strict_numeric=strict_numeric,
                                min_value=MIN_SCALE, max_value=MAX_SCALE),
                sy=scene_number(record.get("sy", 1), f"{label}.sy", strict_numeric=strict_numeric,
                                min_value=MIN_SCALE, max_value=MAX_SCALE),
                rotation=scene_number(record.get("rotate", 0), f"{label}.rotate", strict_numeric=strict_numeric),
                critical=label in critical, prefetch=label in prefetch)
            if group == "window":
                sizes = record.get("size")
                slices = record.get("slice")
                if not isinstance(sizes, dict) or set(sizes) != {"width", "height"}:
                    raise ValueError(f"{label}.size: width/height が必要です。")
                if not isinstance(slices, dict) or set(slices) != {"left", "right", "top", "bottom"}:
                    raise ValueError(f"{label}.slice: left/right/top/bottom が必要です。")
                def read_int(value: Any, key: str) -> int:
                    number = scene_number(value, f"{label}.{key}", strict_numeric=True)
                    if int(number) != number:
                        raise ValueError(f"{label}.{key} は整数にしてください。")
                    return int(number)
                asset.window_width = read_int(sizes["width"], "size.width")
                asset.window_height = read_int(sizes["height"], "size.height")
                asset.window_slices = tuple(read_int(slices[k], f"slice.{k}") for k in ("left", "right", "top", "bottom"))
                asset.window_mode = record.get("fill", "repeat")
                stored_alpha = record.get("alpha", "source")
                if stored_alpha not in WINDOW_ALPHA_MODES:
                    raise ValueError(f"{label}.alpha は source/opaque のみ指定できます。")
                # Interpret both saved modes as source opacity. Existing scenes
                # written by v30/v31 defaulted to "opaque" unintentionally.
                asset.window_alpha_mode = "source"
                with Image.open(source) as window_source:
                    validate_window_spec(window_source.size, asset.window_width, asset.window_height,
                                         asset.window_slices, asset.window_mode)
            load_visual(asset)
            if group == "window" and (asset.sx < window_min_scales(asset)[0] or
                                      asset.sy < window_min_scales(asset)[1]):
                raise ValueError(f"{label}: サイズが四隅の固定幅を下回っています。")
            if asset.missing:
                warnings.append(f"{label}: {asset.missing}")
            assets.append(asset)
    validate_toggle_groups(assets)
    return assets, warnings


def read_toggle_events(resources: Mapping[str, Any], main_assets: list[Asset],
                       scene_dir: Path, *, strict_numeric: bool = False) -> tuple[list[Asset], list[str]]:
    """Resolve embedded event assets and hide targets; supports nested toggles."""
    result = list(main_assets)
    warnings: list[str] = []
    main_by_ref = {item.full_key: item for item in main_assets}
    ui = resources.get("ui", {})
    toggles = ui.get("toggle button", {})
    buttons = ui.get("button", {})
    if not isinstance(toggles, dict) or not isinstance(buttons, dict):
        raise ValueError("ui.button / ui['toggle button'] はオブジェクトにしてください。")

    def attach(owner: Asset, record: Mapping[str, Any], depth: int) -> None:
        if depth > 8:
            raise ValueError("トグルイベントのネストは最大8階層です。")
        raw_events = record.get("events", [])
        if not isinstance(raw_events, list) or len(raw_events) > 500:
            raise ValueError(f"{owner.name}.events: イベントは500件以下の配列にしてください。")
        for item in raw_events:
            if not isinstance(item, dict):
                raise ValueError(f"{owner.name}.events: イベントはオブジェクトが必要です。")
            etype = item.get("type")
            if etype == "hide":
                if owner.kind != "toggle_uv":
                    raise ValueError("通常ボタンにはアセット非表示イベントを設定できません。")
                if set(item) != {"type", "target"}:
                    raise ValueError("非表示イベントは type と target のみ指定してください。")
                target = main_by_ref.get(item["target"])
                if target is None or target.uid == owner.uid:
                    raise ValueError(f"{owner.name}: 非表示対象 '{item['target']}' が見つかりません。")
                owner.events.append({"type": "hide", "target_uid": target.uid})
            elif etype in ("show_scroll_group", "hide_scroll_group", "toggle_scroll_group"):
                if set(item) != {"type", "group"}:
                    raise ValueError("スクロールグループイベントには type と group のみ指定してください。")
                if owner.kind == "toggle_uv" and etype != "show_scroll_group":
                    raise ValueError("トグルボタンで使用できるのはグループ表示イベントのみです。")
                group_name = safe_asset_key(item["group"])
                if any(e.get("group_name") == group_name for e in owner.events
                       if e.get("type") in ("show_scroll_group", "hide_scroll_group", "toggle_scroll_group")):
                    raise ValueError(f"{owner.name}: 同じスクロールグループの操作が重複しています。")
                owner.events.append({"type": etype, "group_name": group_name})
            elif etype == "show":
                if owner.kind != "toggle_uv":
                    raise ValueError("通常ボタンにはアセット追加表示イベントを設定できません。")
                if set(item) != {"type", "asset"}:
                    raise ValueError("追加表示イベントは type と asset のみ指定してください。")
                spec = item["asset"]
                if not isinstance(spec, dict) or not {"group", "name", "data"}.issubset(spec):
                    raise ValueError("追加表示には asset.group/name/data が必要です。")
                group = spec["group"]
                if group not in ("image", "button", "toggle button", "window", "animation"):
                    raise ValueError("追加表示のアセット種別が不正です。")
                name = safe_asset_key(spec["name"])
                data = spec["data"]
                if not isinstance(data, dict):
                    raise ValueError("追加表示のdataはオブジェクトが必要です。")
                if len(result) >= MAX_ASSETS:
                    raise ValueError("イベントを含む総アセット数が上限を超えています。")
                mini_ui = {"image": {}, "button": {}, "toggle button": {}, "window": {}}
                mini_anim: dict[str, Any] = {}
                if group == "animation":
                    mini_anim[name] = data
                else:
                    mini_ui[group][name] = data
                loaded, child_warnings = read_new_scene_assets(
                    {"ui": mini_ui, "animation": mini_anim}, set(), set(), scene_dir,
                    strict_numeric=strict_numeric)
                child = loaded[0]
                child.event_owner_uid = owner.uid
                layer = spec.get("layer")
                if layer is not None and (type(layer) is not int or layer < 0 or layer >= MAX_ASSETS):
                    raise ValueError("追加表示アセットのlayerが不正です。")
                child.event_layer = layer
                child.critical = spec.get("critical", False)
                child.prefetch = spec.get("prefetch", False)
                if not isinstance(child.critical, bool) or not isinstance(child.prefetch, bool):
                    raise ValueError("追加表示アセットのcritical/prefetchは真偽値が必要です。")
                if any(a.name == child.name and a.category == child.category for a in result):
                    raise ValueError(f"追加表示アセット名が重複しています: {child.name}")
                result.append(child)
                owner.events.append({"type": "show", "asset_uid": child.uid})
                warnings.extend(child_warnings)
                if child.kind in ("toggle_uv", "button_uv"):
                    attach(child, data, depth + 1)
            else:
                raise ValueError(f"{owner.name}: 未対応のイベント種別: {etype!r}")
    for owner in main_assets:
        if owner.kind == "toggle_uv":
            attach(owner, toggles[owner.name], 0)
        elif owner.kind == "button_uv":
            attach(owner, buttons[owner.name], 0)
    return result, warnings



def validated_toggle_group_name(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}.group は文字列にしてください。")
    return safe_asset_key(value)


def validate_toggle_groups(assets: Sequence["Asset"]) -> None:
    """Validate exclusive groups without changing user-authored initial states."""
    groups: dict[str, list[Asset]] = {}
    for asset in assets:
        if asset.toggle_group is None:
            continue
        if asset.kind != "toggle_uv" or asset.event_owner_uid is not None:
            raise ValueError(f"{asset.name}: 排他グループには通常配置のトグルボタンのみ指定できます。")
        safe_asset_key(asset.toggle_group)
        groups.setdefault(asset.toggle_group, []).append(asset)
    for name, members in groups.items():
        if len(members) < 2:
            raise ValueError(f"グループ {name}: スイッチが2個以上必要です。")
        if sum(member.toggle_default == "on" for member in members) != 1:
            raise ValueError(f"グループ {name}: 初期ONのスイッチを1個にしてください。")


def inside_polygon(px: float, py: float, vertices: list[tuple[float, float]]) -> bool:
    hit = False
    last_x, last_y = vertices[-1]
    for x, y in vertices:
        if (y > py) != (last_y > py):
            intersect = (last_x - x) * (py - y) / (last_y - y) + x
            if px < intersect:
                hit = not hit
        last_x, last_y = x, y
    return hit


def transform_local(asset: "Asset", x: float, y: float) -> tuple[float, float]:
    cs, sn = math.cos(asset.rotation), math.sin(asset.rotation)
    return (asset.px + cs * asset.sx * x - sn * asset.sy * y,
            asset.py + sn * asset.sx * x + cs * asset.sy * y)


@dataclass
class ScrollGroup:
    """Viewport and member IDs. Scroll displacement is preview-only and not persisted."""
    name: str
    px: float = 0.0
    py: float = 0.0
    width: float = 640.0
    height: float = 360.0
    scroll_x: bool = False
    scroll_y: bool = True
    members: set[str] = field(default_factory=set)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.px - self.width / 2, self.py - self.height / 2,
                self.px + self.width / 2, self.py + self.height / 2)


def read_scroll_groups(raw: Any, assets: Sequence["Asset"], *, strict_numeric: bool = True
                       ) -> dict[str, ScrollGroup]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > 128:
        raise ValueError("scroll_groups は128件以下のオブジェクトにしてください。")
    # Both root assets and show-event children have globally unique
    # group-qualified scene references.  Keep members as asset references so
    # moving a root into a toggle event does not break the saved scroll group.
    refs: dict[str, Asset] = {}
    for asset in assets:
        ref = asset.full_key
        if ref in refs:
            raise ValueError(f"scroll_groups: アセット参照が重複しています: {ref}")
        refs[ref] = asset
    result: dict[str, ScrollGroup] = {}
    used: set[str] = set()
    for name, record in raw.items():
        safe_asset_key(name)
        if not isinstance(record, dict):
            raise ValueError(f"scroll_groups.{name} はオブジェクトにしてください。")
        if set(record) != {"px", "py", "width", "height", "scroll_x", "scroll_y", "members"}:
            raise ValueError(f"scroll_groups.{name}: px/py/width/height/scroll_x/scroll_y/members が必要です。")
        dims = {key: scene_number(record[key], f"scroll_groups.{name}.{key}", strict_numeric=strict_numeric)
                for key in ("px", "py", "width", "height")}
        if not 1 <= dims["width"] <= DESIGN_W * 4 or not 1 <= dims["height"] <= DESIGN_H * 4:
            raise ValueError(f"scroll_groups.{name}: 範囲の幅・高さが不正です。")
        for axis in ("scroll_x", "scroll_y"):
            if type(record[axis]) is not bool:
                raise ValueError(f"scroll_groups.{name}.{axis} はboolが必要です。")
        if not isinstance(record["members"], list) or any(not isinstance(v, str) for v in record["members"]):
            raise ValueError(f"scroll_groups.{name}.members はアセット参照の配列にしてください。")
        if len(set(record["members"])) != len(record["members"]):
            raise ValueError(f"scroll_groups.{name}: 同一アセットが重複しています。")
        members = set()
        for ref in record["members"]:
            if ref not in refs:
                raise ValueError(f"scroll_groups.{name}: アセットが見つかりません: {ref}")
            if ref in used:
                raise ValueError(f"scroll_groups: アセット {ref} は複数のグループに所属できません。")
            members.add(refs[ref].uid)
            used.add(ref)
        result[name] = ScrollGroup(name=name, **dims, scroll_x=record["scroll_x"],
                                   scroll_y=record["scroll_y"], members=members)
    return result


def validate_scroll_toggle_events(assets: Sequence["Asset"],
                                  scroll_groups: Mapping[str, ScrollGroup]) -> None:
    """Verify referenced viewport exists and its own trigger is not one of its members."""
    for owner in assets:
        if owner.kind not in ("toggle_uv", "button_uv", "button"):
            continue
        for event in owner.events:
            if event.get("type") not in ("show_scroll_group", "hide_scroll_group", "toggle_scroll_group"):
                continue
            name = event.get("group_name")
            group = scroll_groups.get(name) if isinstance(name, str) else None
            if group is None:
                raise ValueError(f"{owner.name}: スクロールグループ '{name}' が見つかりません。")
            if owner.uid in group.members and event["type"] != "hide_scroll_group":
                raise ValueError(f"{owner.name}: 自身の所属グループは表示・切替イベントで制御できません。")


@dataclass
class Asset:
    name: str
    kind: str                   # image / button_uv / toggle_uv / button / animation / window
    source: Path
    window_width: int = 0       # Base size, in scene units; sx/sy resize the panel without scaling corners.
    window_height: int = 0
    window_slices: tuple[int, int, int, int] = (1, 1, 1, 1)  # left, right, top, bottom
    window_mode: str = "repeat"
    window_alpha_mode: str = "source"  # Always preserve original RGBA alpha.
    key: str = ""              # Atlas frame or animation movement.
    uv_rect: Optional[tuple[int, int, int, int]] = None  # PNG/JPG pixel crop (x, y, w, h).
    pressed_uv_rect: Optional[tuple[int, int, int, int]] = None  # Button's pressed-state UV.
    pressed_key: Optional[str] = None  # Pressed sprite key for a plist button.
    pressed_source: Optional[Path] = None  # Optional separate PNG/JPG for pressed state.
    button_se: Optional[str] = None  # Refers to assets.se.<key> for buttons.
    toggle_default: str = "off"   # Toggle only: initial state, either off or on.
    toggle_group: Optional[str] = None  # Exclusive group ID.  Only one member may be ON.
    event_owner_uid: Optional[str] = None  # A show-event asset is embedded in its toggle.
    event_layer: Optional[int] = None  # Absolute stack index for JSON round-trips.
    events: list[dict[str, str]] = field(default_factory=list)  # Toggle: show/hide; buttons: show/hide/toggle_scroll_group.
    pressed_image: Optional[Image.Image] = field(default=None, repr=False)
    px: float = 0.0
    py: float = 0.0
    sx: float = 1.0
    sy: float = 1.0
    rotation: float = 0.0       # CCW radians in Y-up coordinates.
    flip_x: bool = False       # Mirror final local image horizontally (including both button states).
    flip_y: bool = False       # Mirror final local image vertically.
    critical: bool = False
    prefetch: bool = False
    animation_se: Optional[str] = None  # Refers to assets.se.<key>; animation assets only.
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    image: Optional[Image.Image] = field(default=None, repr=False)
    model: Any = field(default=None, repr=False)
    movement: Any = field(default=None, repr=False)
    bounds: tuple[float, float, float, float] = (-64.0, -64.0, 64.0, 64.0)
    missing: str = ""
    raster_cache: dict[tuple[int, float], Image.Image] = field(default_factory=dict, repr=False)

    @property
    def category(self) -> str:
        return "animation" if self.kind == "animation" else "ui"

    @property
    def full_key(self) -> str:
        if self.category == "animation":
            return f"animation.{self.name}"
        group = ("toggle button" if self.kind == "toggle_uv" else
                 "button" if self.kind in ("button_uv", "button") else
                 "window" if self.kind == "window" else "image")
        return f"ui.{group}.{self.name}"


def window_min_scales(asset: Asset) -> tuple[float, float]:
    """Smallest scales that still leave the fixed corners and a 1px center."""
    if asset.kind != "window":
        return MIN_SCALE, MIN_SCALE
    l, r, t, b = asset.window_slices
    return (max(MIN_SCALE, (l + r + 1) / asset.window_width),
            max(MIN_SCALE, (t + b + 1) / asset.window_height))


def asset_world_aabb(asset: Asset) -> tuple[float, float, float, float]:
    """Visible axis-aligned bounds of an asset, including rotation and scale.

    Snapping uses the rendered quadrilateral rather than the origin alone;
    asymmetric and rotated sprites therefore align visually.
    """
    x0, y0, x1, y1 = asset.bounds
    cs, sn = math.cos(asset.rotation), math.sin(asset.rotation)
    xs, ys = [], []
    for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1)):
        xs.append(asset.px + cs * asset.sx * x - sn * asset.sy * y)
        ys.append(asset.py + sn * asset.sx * x + cs * asset.sy * y)
    return min(xs), min(ys), max(xs), max(ys)


def snap_reference_lines(assets: Sequence[Asset], active_uid: str
                         ) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Snapshot of scene edges/center plus other assets' visible edges/center.

    These targets are computed once at drag start rather than for each mouse event.
    """
    horizontal = [-DESIGN_W / 2.0, 0.0, DESIGN_W / 2.0]
    vertical = [-DESIGN_H / 2.0, 0.0, DESIGN_H / 2.0]
    for item in assets:
        if item.uid == active_uid:
            continue
        x0, y0, x1, y1 = asset_world_aabb(item)
        horizontal.extend((x0, (x0 + x1) / 2.0, x1))
        vertical.extend((y0, (y0 + y1) / 2.0, y1))
    return tuple(sorted(horizontal)), tuple(sorted(vertical))


def nearest_snap(points: Sequence[float], sorted_targets: Sequence[float],
                 tolerance: float) -> tuple[float, Optional[float]]:
    """Return the smallest axis correction and its reference line, if in range."""
    best_distance = max(0.0, tolerance)
    correction = 0.0
    guide: Optional[float] = None
    for point in points:
        index = bisect.bisect_left(sorted_targets, point)
        for i in (index - 1, index):
            if not 0 <= i < len(sorted_targets):
                continue
            target = sorted_targets[i]
            diff = target - point
            if abs(diff) <= best_distance:
                # On ties, keep the first candidate to avoid flicker.
                if guide is None or abs(diff) < best_distance - 1e-9:
                    guide = target
                    correction = diff
                    best_distance = abs(diff)
    return correction, guide


@dataclass(frozen=True)
class EqualGapSnap:
    """Axis-wise translation and two equal-spacing guide intervals (world units)."""
    delta: float
    axis: str
    intervals: tuple[tuple[float, float], tuple[float, float]]
    perpendicular: float
    gap: float


def equal_gap_snap(moving: tuple[float, float, float, float],
                   obstacles: Sequence[tuple[float, float, float, float]],
                   axis: str, tolerance: float,
                   proximity: float = 0.0) -> Optional[EqualGapSnap]:
    """Find PowerPoint-style equal spacing without modifying scene geometry.

    Supports centering between immediate neighbours and extending a pre-existing
    gap to either side of a row/column.  Requires perpendicular alignment, and
    rejects candidates that overlap any of the row's other visible objects.
    """
    if axis not in ("x", "y"):
        raise ValueError("axis は 'x' または 'y' を指定してください。")
    index = 0 if axis == "x" else 1
    orthogonal = 1 - index
    a0, a1 = moving[index], moving[index + 2]
    b0, b1 = moving[orthogonal], moving[orthogonal + 2]
    width = a1 - a0
    if width <= 0 or not math.isfinite(width):
        return None
    center = (b0 + b1) / 2.0
    intervals = []
    for box in obstacles:
        lo, hi = box[index], box[index + 2]
        perp_lo, perp_hi = box[orthogonal], box[orthogonal + 2]
        if lo >= hi or min(b1, perp_hi) + proximity < max(b0, perp_lo):
            continue
        intervals.append((lo, hi))
    intervals.sort()
    if len(intervals) < 2:
        return None
    best: Optional[EqualGapSnap] = None

    def add_candidate(target: float,
                      guides: tuple[tuple[float, float], tuple[float, float]]) -> None:
        nonlocal best
        delta = target - a0
        gap_a, gap_b = guides
        gap = gap_a[1] - gap_a[0]
        if not (math.isfinite(delta) and abs(delta) <= tolerance and gap >= -1e-8):
            return
        if abs((gap_b[1] - gap_b[0]) - gap) > 1e-6:
            return
        new_end = target + width
        # No forced overlap with other row/column members.
        if any(target < hi - 1e-7 and new_end > lo + 1e-7 for lo, hi in intervals):
            return
        result = EqualGapSnap(delta, axis, guides, center, max(0.0, gap))
        if best is None or abs(result.delta) + 1e-7 < abs(best.delta):
            best = result

    # Inside two neighbours: equal left and right whitespace.
    for left, right in zip(intervals, intervals[1:]):
        if right[0] < left[1] + width:
            continue
        target = (left[1] + right[0] - width) / 2.0
        add_candidate(target, ((left[1], target), (target + width, right[0])))

    # Reproduce existing gaps.  Search only nearby stationary edges for each
    # recorded gap (O(n log n), not a Cartesian product over every asset).
    # This preserves the fast drag path for scenes containing many assets.
    import bisect
    starts = [lo for lo, _hi in intervals]
    by_end = sorted((hi, lo) for lo, hi in intervals)
    ends = [hi for hi, _lo in by_end]
    tested: set[tuple[int, int]] = set()
    for previous, following in zip(intervals, intervals[1:]):
        gap = following[0] - previous[1]
        if gap < 0:
            continue
        # Candidate after an asset: moving.left == stationary.right + gap.
        end_pos = bisect.bisect_left(ends, a0 - gap)
        for idx in (end_pos - 1, end_pos):
            if not 0 <= idx < len(ends):
                continue
            end = ends[idx]
            after = end + gap
            if abs(after - a0) > tolerance:
                continue
            marker = (idx, round(gap * 1000))
            if marker not in tested:
                tested.add(marker)
                add_candidate(after, ((previous[1], following[0]), (end, after)))
        # Candidate before an asset: moving.right + gap == stationary.left.
        start_pos = bisect.bisect_left(starts, a1 + gap)
        for idx in (start_pos - 1, start_pos):
            if not 0 <= idx < len(starts):
                continue
            start = starts[idx]
            before = start - gap - width
            if abs(before - a0) > tolerance:
                continue
            marker = (len(ends) + idx, round(gap * 1000))
            if marker not in tested:
                tested.add(marker)
                add_candidate(before, ((previous[1], following[0]), (before + width, start)))
    return best


# -*- coding: utf-8 -*-
"""Cocos Studio UI (widgetTree/actionlist) ExportJson support.
Standalone integration: these definitions are inserted into the .pyw module.
"""

@dataclass(frozen=True)
class UIAction:
    name: str
    duration: int
    loop: bool
    fps: float
    tracks: dict[str, tuple[dict[str, Any], ...]]


@dataclass
class UIAnimationModel:
    json_path: Path
    width: int
    height: int
    widget_tree: dict[str, Any]
    movements: tuple[UIAction, ...]
    sprites: dict[str, Image.Image]
    warnings: list[str] = field(default_factory=list)
    missing_sprite_names: set[str] = field(default_factory=set)


def parse_ui_export(path: Path, root: dict[str, Any]) -> UIAnimationModel:
    """Read Cocos Studio 2.x UI exports without relying on armature_data."""
    tree = root.get('widgetTree')
    if not isinstance(tree, dict):
        raise ValueError('widgetTree オブジェクトが必要です。')
    width = int(checked_float(root.get('designWidth', 1280), 'designWidth', min_value=1, max_value=8192))
    height = int(checked_float(root.get('designHeight', 720), 'designHeight', min_value=1, max_value=8192))
    if width * height > MAX_RENDER_PIXELS:
        raise ValueError('UI の設計サイズが大きすぎます。')
    animation = root.get('animation')
    actions = animation.get('actionlist', []) if isinstance(animation, dict) else []
    if not isinstance(actions, list) or len(actions) > 256:
        raise ValueError('animation.actionlist の形式または件数が不正です。')
    movements: list[UIAction] = []
    names: set[str] = set()
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        base_name = str(action.get('name') or f'action_{index + 1}')
        name = base_name
        counter = 2
        while name in names:
            name = f'{base_name}_{counter}'
            counter += 1
        names.add(name)
        unit_time = checked_float(action.get('unittime', 1 / DEFAULT_FPS), 'unittime',
                                  min_value=1 / 240, max_value=60)
        nodes = action.get('actionnodelist', [])
        if not isinstance(nodes, list) or len(nodes) > MAX_ASSETS * 10:
            raise ValueError('actionnodelist が不正です。')
        tracks: dict[str, tuple[dict[str, Any], ...]] = {}
        duration = 1
        for node in nodes:
            if not isinstance(node, dict):
                continue
            raw_frames = node.get('actionframelist', [])
            if not isinstance(raw_frames, list) or len(raw_frames) > MOVEMENT_FRAME_LIMIT:
                raise ValueError('actionframelist が不正です。')
            frames: list[dict[str, Any]] = []
            for frame in raw_frames:
                if not isinstance(frame, dict):
                    continue
                position = int(checked_float(frame.get('frameid', 0), 'frameid', min_value=0, max_value=MOVEMENT_FRAME_LIMIT))
                frames.append({**frame, 'frameid': position})
                duration = max(duration, position + 1)
            frames.sort(key=lambda f: f['frameid'])
            identifiers = []
            if node.get('name') is not None:
                identifiers.append(str(node['name']))
            if node.get('ActionTag') is not None:
                identifiers.append(f"tag:{node['ActionTag']}")
            for identifier in identifiers:
                if identifier:
                    tracks[identifier] = tuple(frames)
        movements.append(UIAction(name, duration, bool(action.get('loop', False)),
                                  1.0 / unit_time, tracks))
    if not movements:
        movements.append(UIAction('default', 1, False, DEFAULT_FPS, {}))
    model = UIAnimationModel(path, width, height, tree, tuple(movements), {})
    _load_ui_textures(model, root)
    return model


def load_animation_file(path: Path) -> Any:
    """Dispatch both supported ExportJson families, with a clear unsupported-format error."""
    path = path.resolve()
    if path.stat().st_size > JSON_SIZE_LIMIT:
        raise ValueError('ExportJson のファイルサイズが上限を超えています。')
    with path.open('r', encoding='utf-8-sig') as reader:
        root = json.load(reader)
    if not isinstance(root, dict):
        raise ValueError('ExportJson のルートがオブジェクトではありません。')
    if isinstance(root.get('armature_data'), list):
        model = parse_exportjson(path)
        resolve_armature_images(model)
        return model
    if isinstance(root.get('widgetTree'), dict):
        return parse_ui_export(path, root)
    raise ValueError('armature_data 形式または widgetTree / animation.actionlist 形式の ExportJson が必要です。')


def _load_ui_textures(model: UIAnimationModel, root: dict[str, Any]) -> None:
    """Resolve listed atlas pages; absent PNGs produce visible diagnostic placeholders."""
    sources = root.get('textures', [])
    if not isinstance(sources, list):
        sources = []
    if len(sources) > 128:
        raise ValueError('textures の件数が上限を超えています。')
    atlas_loaded = False
    for name in sources:
        if not isinstance(name, str) or not name.casefold().endswith('.plist'):
            continue
        path = _find_export_asset(model.json_path.parent, name)
        if path is None:
            model.warnings.append(f'参照 plist がありません: {name}')
            continue
        try:
            atlas = load_atlas(path)
            atlas_loaded = True
            model.warnings.extend(atlas.warnings[:20])
            for frame in atlas.frames:
                image = extract_sprite(atlas.image, frame)
                model.sprites.setdefault(frame.name.casefold(), image)
                model.sprites.setdefault(Path(frame.name).stem.casefold(), image)
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            model.warnings.append(f'{name}: {exc}')
            # The metadata still defines sprite dimensions, useful for a missing-image preview.
            try:
                with path.open('rb') as handle:
                    plist_root = plistlib.load(handle)
                entries, _warnings, _images = _frame_entries(plist_root.get('frames', {}))
                for sprite_name, desc in entries:
                    frame = parse_frame(sprite_name, desc)
                    # Keep placeholders bounded even for unusually large atlases.
                    iw, ih = min(frame.source_width, 8192), min(frame.source_height, 8192)
                    if iw * ih <= MAX_RENDER_PIXELS:
                        image = _ui_missing_sprite((iw, ih), sprite_name)
                        model.sprites.setdefault(sprite_name.casefold(), image)
                        model.sprites.setdefault(Path(sprite_name).stem.casefold(), image)
            except (OSError, ValueError, TypeError, plistlib.InvalidFileException) as inner:
                model.warnings.append(f'{name} のフレーム情報を解析できません: {inner}')
    if not atlas_loaded:
        pngs = root.get('texturesPng', [])
        if isinstance(pngs, list):
            for name in pngs:
                if isinstance(name, str) and _find_export_asset(model.json_path.parent, name) is None:
                    model.warnings.append(f'参照テクスチャ画像がありません: {name}')


def _ui_missing_sprite(size: tuple[int, int], name: str) -> Image.Image:
    img = Image.new('RGBA', size, (102, 39, 39, 42))
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.rectangle((0, 0, w - 1, h - 1), outline=(255, 113, 90, 200), width=2)
    draw.line((0, 0, w - 1, h - 1), fill=(255, 113, 90, 90), width=2)
    draw.line((0, h - 1, w - 1, 0), fill=(255, 113, 90, 90), width=2)
    draw.text((10, 10), 'PNG missing: ' + name[:80], fill=(255, 205, 190, 255))
    return img


def _ui_value(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _ui_options(node: dict[str, Any]) -> dict[str, Any]:
    opts = node.get('options')
    return opts if isinstance(opts, dict) else {}


def _ui_image(model: UIAnimationModel, opts: dict[str, Any]) -> Optional[Image.Image]:
    for key in ('fileNameData', 'textureData', 'backGroundImageData', 'normalData',
                'pressedData', 'disabledData', 'fileName', 'texture'):
        info = opts.get(key)
        if isinstance(info, dict):
            name = info.get('path')
        elif isinstance(info, str) and info not in ('None', ''):
            name = info
        else:
            continue
        if not isinstance(name, str) or not name or name == 'None':
            continue
        norm = name.replace('\\', '/').casefold()
        if norm in model.missing_sprite_names:
            continue
        image = model.sprites.get(norm) or model.sprites.get(Path(norm).name) or model.sprites.get(Path(norm).stem)
        if image is not None:
            return image
        # Standalone resources are resolved from the ExportJson directory only.
        resolved = _find_export_asset(model.json_path.parent, name)
        if resolved is not None and resolved.suffix.casefold() in ('.png', '.jpg', '.jpeg'):
            try:
                image = _load_limited_image(resolved)
                model.sprites[norm] = image
                return image
            except (OSError, ValueError, UnidentifiedImageError):
                pass
    return None


def _ui_track_pose(opts: dict[str, Any], movement: UIAction,
                   frame: float) -> dict[str, Any]:
    name = str(opts.get('name', ''))
    tag = str(opts.get('actiontag', ''))
    track = movement.tracks.get(name) or movement.tracks.get('tag:' + tag)
    state: dict[str, Any] = {
        'x': _ui_value(opts.get('x')), 'y': _ui_value(opts.get('y')),
        'sx': _ui_value(opts.get('scaleX'), 1), 'sy': _ui_value(opts.get('scaleY'), 1),
        'rotation': _ui_value(opts.get('rotation')),
        'opacity': _ui_value(opts.get('opacity'), 255),
        'visible': str(opts.get('visible', 'True')).lower() not in ('false', '0'),
    }
    if not track:
        return state
    before = track[0]
    after = track[-1]
    for ix, item in enumerate(track):
        if frame >= item['frameid']:
            before = item
            after = track[min(ix + 1, len(track) - 1)]
        else:
            after = item
            break
    span = after['frameid'] - before['frameid']
    # Cocos action timeline tweenType == 0 denotes the default linear interpolation.
    weight = max(0.0, min(1.0, (frame - before['frameid']) / span)) if span > 0 else 0.0
    if _ui_value(before.get('tweenType'), 0.0) != 0:
        weight = 0.0  # Nonlinear easing is conservatively held rather than guessed.
    keys = {'positionx': 'x', 'positiony': 'y', 'scalex': 'sx', 'scaley': 'sy',
            'rotation': 'rotation', 'opacity': 'opacity'}
    for source, target in keys.items():
        if source in before:
            a = _ui_value(before[source], state[target])
            b = _ui_value(after.get(source, a), a)
            state[target] = a + (b - a) * weight
    # Treat visibility as stepped (non-interpolated).
    if 'visible' in before:
        state['visible'] = str(before['visible']).lower() not in ('false', '0')
    return state


def _ui_font(font_size: int) -> Any:
    from PIL import ImageFont
    size = max(8, min(font_size, 96))
    try:
        return ImageFont.truetype('DejaVuSans.ttf', size)
    except OSError:
        return ImageFont.load_default()


def render_ui_export(model: UIAnimationModel, movement: UIAction, frame: int,
                     width: int, height: int, scale: float,
                     camera: tuple[float, float]) -> tuple[Image.Image, list[Any]]:
    """Render a UI widget tree with animated transformations, including nested widgets."""
    if width <= 0 or height <= 0 or width * height > MAX_RENDER_PIXELS:
        raise ValueError('プレビューの描画サイズが上限を超えています。')
    output = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    root_transform = (1.0, 0.0, 0.0, 1.0, -model.width / 2, -model.height / 2)
    visited = [0]
    def visit(node: Any, parent_matrix: tuple[float, ...], parent_offset: tuple[float, float], parent_opacity: float) -> None:
        if not isinstance(node, dict):
            return
        visited[0] += 1
        if visited[0] > MAX_ASSETS * 10:
            return
        opts = _ui_options(node)
        if not opts:
            return
        pose = _ui_track_pose(opts, movement, frame)
        if not pose['visible'] or abs(pose['sx'] * pose['sy']) < 0.000001:
            return
        angle = -math.radians(pose['rotation'])
        cs, sn = math.cos(angle), math.sin(angle)
        local = (pose['sx'] * cs, pose['sx'] * sn,
                 -pose['sy'] * sn, pose['sy'] * cs,
                 pose['x'] + parent_offset[0], pose['y'] + parent_offset[1])
        matrix = matrix_mul(parent_matrix, local)
        nw = max(1, _ui_value(opts.get('width'), 1))
        nh = max(1, _ui_value(opts.get('height'), 1))
        anchor_x = _ui_value(opts.get('anchorPointX'), 0.5)
        anchor_y = _ui_value(opts.get('anchorPointY'), 0.5)
        opacity = parent_opacity * max(0, min(255, pose['opacity'])) / 255.0
        classname = str(node.get('classname', ''))
        img = _ui_image(model, opts) if classname not in ('Panel', 'Label') else None
        if classname == 'Label':
            text = str(opts.get('text') or '')
            if text:
                font_size = int(_ui_value(opts.get('fontSize'), 18))
                img = Image.new('RGBA', (max(2, int(nw)), max(2, int(nh))), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                color = (max(0, min(255, int(_ui_value(opts.get('colorR'), 255)))),
                         max(0, min(255, int(_ui_value(opts.get('colorG'), 255)))),
                         max(0, min(255, int(_ui_value(opts.get('colorB'), 255)))), 255)
                draw.text((0, 0), text[:200], fill=color, font=_ui_font(font_size))
        if img is not None:
            if classname == 'LoadingBar':
                percent = min(100, max(0, _ui_value(opts.get('percent'), 100))) / 100
                clipped_width = max(1, int(img.width * percent))
                img = img.crop((0, 0, clipped_width, img.height))
                nw *= percent
            if opacity > 0 and nw > 0 and nh > 0:
                alpha = max(0, min(255, int(round(opacity * 255))))
                item = PlacedSprite(
                    str(opts.get('name', 'widget')), '',
                    TextureDef(str(opts.get('name', 'widget')), img.width, img.height, anchor_x, anchor_y),
                    img,
                    matrix_mul(matrix, (nw / img.width, 0, 0, nh / img.height, 0, 0)),
                    _ui_value(opts.get('ZOrder')), BLEND_DEFAULT, (255, 255, 255, alpha))
                _draw_affine_sprite(output, item, scale, camera)
        child_offset = (-nw * anchor_x, -nh * anchor_y)
        children = node.get('children', [])
        if isinstance(children, list):
            # ZOrder controls sibling order; parent graphics always precede children.
            for child in sorted((c for c in children if isinstance(c, dict)),
                    key=lambda c: _ui_value(_ui_options(c).get('ZOrder'))):
                visit(child, matrix, child_offset, opacity)
    visit(model.widget_tree, root_transform, (0, 0), 1.0)
    return output, []


def load_visual(asset: Asset) -> None:
    """Populate bitmap/model without changing scene transforms or saved identity."""
    asset.image = None
    asset.pressed_image = None
    asset.model = None
    asset.movement = None
    asset.raster_cache.clear()
    asset.missing = ""
    try:
        suffix = asset.source.suffix.casefold()
        if asset.kind == "animation":
            if suffix not in (".exportjson", ".json"):
                raise ValueError("ExportJson を指定してください。")
            model = load_animation_file(asset.source)
            movement = next((m for m in model.movements if m.name == asset.key), None)
            if movement is None:
                raise ValueError(f"アニメーション '{asset.key}' が存在しません。")
            asset.model = model
            asset.movement = movement
            if isinstance(model, UIAnimationModel):
                asset.bounds = (-model.width / 2, -model.height / 2,
                                model.width / 2, model.height / 2)
                if model.warnings:
                    asset.missing = " / ".join(model.warnings[:3])
            else:
                asset.bounds = pose_bounds(model, movement)
                if not model.sprites:
                    asset.missing = "参照テクスチャを読み込めませんでした。"
        elif suffix == ".plist":
            atlas = load_atlas(asset.source)
            frame = next((f for f in atlas.frames if f.name == asset.key), None)
            if frame is None:
                raise ValueError(f"スプライト '{asset.key}' が存在しません。")
            asset.image = extract_sprite(atlas.image, frame)
            if asset.kind == "button":
                pressed_key = asset.pressed_key or asset.key
                pressed_frame = next((f for f in atlas.frames if f.name == pressed_key), None)
                if pressed_frame is None:
                    raise ValueError(f"押下中スプライト '{pressed_key}' が存在しません。")
                asset.pressed_image = extract_sprite(atlas.image, pressed_frame)
        elif suffix in (".png", ".jpg", ".jpeg") and asset.kind in ("image", "button_uv", "toggle_uv", "window"):
            with Image.open(asset.source) as source:
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValueError("画像の画素数が上限を超えています。")
                original_size = source.size
                if asset.kind == "window":
                    validate_window_spec(original_size, asset.window_width, asset.window_height,
                                         asset.window_slices, asset.window_mode)
                    asset.image = source.convert("RGBA")
                elif asset.kind in ("button_uv", "toggle_uv"):
                    if asset.uv_rect is None or asset.pressed_uv_rect is None:
                        raise ValueError("UVボタンには通常・押下中の両方の範囲が必要です。")
                    nx, ny, nw, nh = validate_uv_rect(asset.uv_rect, original_size)
                    asset.image = source.crop((nx, ny, nx + nw, ny + nh)).convert("RGBA")
                    pressed_path = ensure_asset_path(asset.pressed_source or asset.source)
                    with Image.open(pressed_path) as pressed_src:
                        if pressed_src.width * pressed_src.height > MAX_IMAGE_PIXELS:
                            raise ValueError("押下中画像の画素数が上限を超えています。")
                        px, py, pw, ph = validate_uv_rect(asset.pressed_uv_rect, pressed_src.size)
                        asset.pressed_image = pressed_src.crop((px, py, px + pw, py + ph)).convert("RGBA")
                elif asset.uv_rect is not None:
                    x, y, w, h = validate_uv_rect(asset.uv_rect, original_size)
                    asset.image = source.crop((x, y, x + w, y + h)).convert("RGBA")
                else:
                    asset.image = source.convert("RGBA")
        else:
            raise ValueError("この種類に対応しないファイル形式です。")
        if asset.image is not None:
            w, h = ((asset.window_width, asset.window_height) if asset.kind == "window"
                    else asset.image.size)
            asset.bounds = (-w / 2, -h / 2, w / 2, h / 2)
    except (OSError, ValueError, TypeError, MemoryError, UnidentifiedImageError,
            json.JSONDecodeError) as exc:
        asset.missing = str(exc)
        asset.bounds = (-64.0, -48.0, 64.0, 48.0)


def asset_bitmap(asset: Asset, frame_index: int, viewport_scale: float) -> tuple[Image.Image, float]:
    """Return source raster and its pixels-per-scene-unit factor."""
    if asset.kind == "window" and asset.image is not None:
        target_w = round(asset.window_width * asset.sx)
        target_h = round(asset.window_height * asset.sy)
        key = (target_w, target_h, asset.window_slices, asset.window_mode,
               asset.window_alpha_mode)
        cached = asset.raster_cache.get(key)
        if cached is None:
            cached = render_nine_slice(asset.image, target_w, target_h,
                                       asset.window_slices, asset.window_mode,
                                       asset.window_alpha_mode)
            if len(asset.raster_cache) >= 3:
                asset.raster_cache.clear()
            asset.raster_cache[key] = cached
        return cached, 1.0
    if asset.kind != "animation" or asset.model is None or asset.movement is None:
        if asset.image is None:
            placeholder = Image.new("RGBA", (128, 96), (160, 63, 63, 190))
            return placeholder, 1.0
        return asset.image, 1.0
    x0, y0, x1, y1 = asset.bounds
    # Rasterize animations near their displayed size; transform stage handles scaling.
    density = max(0.12, min(2.0, viewport_scale * max(asset.sx, asset.sy) * 1.2))
    width = max(1, math.ceil((x1 - x0) * density))
    height = max(1, math.ceil((y1 - y0) * density))
    if width * height > MAX_RENDER_PIXELS:
        density *= math.sqrt(MAX_RENDER_PIXELS / (width * height)) * 0.98
        width = max(1, math.ceil((x1 - x0) * density))
        height = max(1, math.ceil((y1 - y0) * density))
    density = round(density, 3)
    key = (max(0, frame_index), density)
    if key not in asset.raster_cache:
        if isinstance(asset.model, UIAnimationModel):
            img, _ = render_ui_export(asset.model, asset.movement, key[0], width, height, density,
                                      camera=((x0 + x1) / 2, (y0 + y1) / 2))
        else:
            img, _ = render_armature(
                asset.model, asset.movement, key[0], width, height, density,
                camera=((x0 + x1) / 2, (y0 + y1) / 2), opaque_background=False)
        if len(asset.raster_cache) >= 5:
            asset.raster_cache.pop(next(iter(asset.raster_cache)))
        asset.raster_cache[key] = img
    return asset.raster_cache[key], density


class Picker(tk.Toplevel):
    """Searchable chooser with preview of sprite regions or Cocos movements."""
    def __init__(self, parent: tk.Misc, label: str, options: list[str], preview_fn: Any):
        super().__init__(parent)
        self.title(label)
        self.geometry("640x540")
        self.minsize(460, 380)
        self.transient(parent)
        self.result: Optional[str] = None
        self.options = options
        self.filtered = options[:]
        self.preview_fn = preview_fn
        self.photo: Optional[ImageTk.PhotoImage] = None
        self.query = tk.StringVar()
        header = ttk.Frame(self, padding=10)
        header.pack(fill="x")
        ttk.Label(header, text=label).pack(anchor="w")
        search = ttk.Entry(header, textvariable=self.query)
        search.pack(fill="x", pady=(6, 0))
        self.query.trace_add("write", lambda *_: self.filter_items())
        columns = ttk.PanedWindow(self, orient="horizontal")
        columns.pack(fill="both", expand=True, padx=10)
        lf = ttk.Frame(columns)
        rf = ttk.Frame(columns)
        columns.add(lf, weight=2)
        columns.add(rf, weight=3)
        self.items = tk.Listbox(lf, exportselection=False)
        scrollbar = ttk.Scrollbar(lf, orient="vertical", command=self.items.yview)
        self.items.configure(yscrollcommand=scrollbar.set)
        self.items.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.preview = ttk.Label(rf, anchor="center", text="選択してください")
        self.preview.pack(fill="both", expand=True)
        self.items.bind("<<ListboxSelect>>", self.refresh_preview)
        self.items.bind("<Double-Button-1>", lambda _: self.confirm())
        controls = ttk.Frame(self, padding=10)
        controls.pack(fill="x")
        ttk.Button(controls, text="キャンセル", command=self.destroy).pack(side="right")
        ttk.Button(controls, text="配置", command=self.confirm).pack(side="right", padx=8)
        self.bind("<Escape>", lambda _: self.destroy())
        self.bind("<Return>", lambda _: self.confirm())
        self.filter_items()
        search.focus_set()
        self.grab_set()
        self.wait_window(self)

    def filter_items(self) -> None:
        query = self.query.get().strip().casefold()
        self.filtered = [name for name in self.options if query in name.casefold()]
        self.items.delete(0, "end")
        for name in self.filtered:
            self.items.insert("end", name)
        if self.filtered:
            self.items.selection_set(0)
            self.refresh_preview()
        else:
            self.photo = None
            self.preview.configure(image="", text="一致する項目がありません")

    def refresh_preview(self, _event: Any = None) -> None:
        chosen = self.items.curselection()
        if not chosen:
            return
        name = self.filtered[chosen[0]]
        try:
            image = self.preview_fn(name)
            image = image.copy()
            image.thumbnail((315, 350), RESAMPLE)
            self.photo = ImageTk.PhotoImage(image)
            self.preview.configure(image=self.photo, text="")
        except (OSError, ValueError, TypeError, MemoryError) as exc:
            self.photo = None
            self.preview.configure(image="", text=f"プレビュー不可: {exc}")

    def confirm(self) -> None:
        indexes = self.items.curselection()
        if indexes:
            self.result = self.filtered[indexes[0]]
            self.destroy()


class UVSelectionDialog(tk.Toplevel):
    """Image UV selection with resize handles, move, redraw, and numeric controls."""

    HANDLE_NAMES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")
    HANDLE_SIZE = 6
    HANDLE_PICK = 12
    PREVIEW_MAX = (252, 228)

    def __init__(self, parent: tk.Misc, path: Path,
                 initial_rect: Optional[tuple[int, int, int, int]] = None) -> None:
        super().__init__(parent)
        self.title(f"画像をUVで配置 / UVを編集 - {path.name}")
        self.minsize(820, 480)
        # Keep bottom confirmation buttons visible on smaller laptop screens.
        # Long UV controls live in a separately scrollable right column.
        self.geometry("1150x730")
        self.transient(parent)
        self.result: Optional[tuple[int, int, int, int]] = None
        self.path = ensure_asset_path(path)
        with Image.open(self.path) as src:
            if src.width * src.height > MAX_IMAGE_PIXELS:
                raise ValueError("画像の画素数が上限を超えています。")
            self.source = src.convert("RGBA")
        self.image_width, self.image_height = self.source.size
        self.rect = validate_uv_rect(initial_rect or
                    (0, 0, self.image_width, self.image_height), self.source.size)
        self.scale = 1.0
        self.origin_x = 0.0
        self.origin_y = 0.0
        self.canvas_photo: Optional[ImageTk.PhotoImage] = None
        self.preview_photo: Optional[ImageTk.PhotoImage] = None
        self.drag: Optional[dict[str, Any]] = None
        # The crop preview has its own zoom/pan state; never mutate crop bounds.
        self.preview_zoom = 1.0
        self.preview_center = (0.5, 0.5)
        self.preview_drag: Optional[dict[str, Any]] = None
        self.preview_photo = None
        # Numeric fields use mouse-wheel nudging; dragging their labels is free.
        self.force_draw = False
        self.hint_var = tk.StringVar(value="枠内ドラッグ: 移動 / 四隅・辺: サイズ変更 / 『範囲を描き直す』: 新しい範囲")
        self.pixel_vars = {name: tk.StringVar() for name in ("x", "y", "w", "h")}
        self.uv_vars = {name: tk.StringVar() for name in ("u0", "v0", "u1", "v1")}
        self._syncing_fields = False
        self._last_edited_group: Optional[str] = None
        for variable in self.pixel_vars.values():
            variable.trace_add("write", lambda *_args: self._mark_field_edit("pixel"))
        for variable in self.uv_vars.values():
            variable.trace_add("write", lambda *_args: self._mark_field_edit("uv"))
        self._build_ui()
        self.update_idletasks()
        # Clamp to the screen, reserving room for titlebar/taskbar. The bottom
        # action bar stays fixed; overflowing form controls remain scrollable.
        available_w = max(820, self.winfo_screenwidth() - 64)
        available_h = max(480, self.winfo_screenheight() - 112)
        width, height = min(1150, available_w), min(820, available_h)
        px = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        px = max(0, min(px, max(0, self.winfo_screenwidth() - width)))
        # Leave additional space below for Windows title bars and taskbars.
        py = max(0, min(py, max(0, self.winfo_screenheight() - height - 88)))
        self.geometry(f"{width}x{height}+{px}+{py}")
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", self._apply_enter)
        self.grab_set()
        self.focus_set()
        self.wait_window(self)

    def _build_ui(self) -> None:
        head = ttk.Frame(self, padding=(12, 10))
        head.pack(fill="x")
        self.image_info_var = tk.StringVar()
        self._update_image_info()
        ttk.Label(head, textvariable=self.image_info_var).pack(anchor="w")
        # The button subclass supplies a per-state image chooser above this header.
        if self.__class__ is UVSelectionDialog:
            source_row = ttk.Frame(head)
            source_row.pack(fill="x", pady=(5, 0))
            ttk.Button(source_row, text="元画像を変更…",
                       command=self.change_source).pack(side="left")
            ttk.Label(source_row, text="選択中のUV比率を維持します。",
                      foreground="#566173").pack(side="left", padx=(10, 0))
        ttk.Label(head, textvariable=self.hint_var, foreground="#566173").pack(anchor="w", pady=(3, 0))
        body = ttk.Frame(self, padding=(10, 0))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=7)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        self.canvas = tk.Canvas(left, bg="#242935", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._resize_canvas)
        self.canvas.bind("<ButtonPress-1>", self._mouse_press)
        self.canvas.bind("<B1-Motion>", self._mouse_move)
        self.canvas.bind("<ButtonRelease-1>", self._mouse_release)
        right_host = ttk.Frame(body)
        right_host.grid(row=0, column=1, sticky="nsew")
        right_host.columnconfigure(0, weight=1)
        right_host.rowconfigure(0, weight=1)
        self.right_scroll = tk.Canvas(right_host, highlightthickness=0, borderwidth=0)
        self.right_scroll.grid(row=0, column=0, sticky="nsew")
        right_scrollbar = ttk.Scrollbar(right_host, orient="vertical",
                                        command=self.right_scroll.yview)
        right_scrollbar.grid(row=0, column=1, sticky="ns")
        self.right_scroll.configure(yscrollcommand=right_scrollbar.set)
        right = ttk.Frame(self.right_scroll, padding=(10, 0))
        self.right_content = right
        self._right_window = self.right_scroll.create_window(0, 0, anchor="nw", window=right)
        right.bind("<Configure>", lambda _event: self.right_scroll.configure(
            scrollregion=self.right_scroll.bbox("all")))
        self.right_scroll.bind("<Configure>", lambda event: self.right_scroll.itemconfigure(
            self._right_window, width=event.width))
        self.bind("<MouseWheel>", self._dialog_scroll)
        self.bind("<Button-4>", self._dialog_scroll)
        self.bind("<Button-5>", self._dialog_scroll)
        ttk.Label(right, text="切り出し範囲（元画像のピクセル）").pack(anchor="w", pady=(2, 4))
        ttk.Label(right, text="項目名または数値欄でホイール操作すると、1px単位で調整できます。",
                  foreground="#566173").pack(anchor="w")
        self._entry_grid(right, self.pixel_vars,
                         (("x", "X"), ("y", "Y"), ("w", "幅"), ("h", "高さ")), self.apply_pixel)
        ttk.Separator(right).pack(fill="x", pady=(14, 10))
        ttk.Label(right, text="UV座標（0 ～ 1 / 左上原点）").pack(anchor="w", pady=(0, 5))
        self._entry_grid(right, self.uv_vars,
                         (("u0", "U開始"), ("v0", "V開始"), ("u1", "U終了"), ("v1", "V終了")), self.apply_uv)
        ttk.Separator(right).pack(fill="x", pady=(14, 8))
        preview_toolbar = ttk.Frame(right)
        preview_toolbar.pack(fill="x")
        ttk.Label(preview_toolbar, text="切り出し結果（ドラッグで範囲拡大）").pack(side="left")
        ttk.Button(preview_toolbar, text="全体", width=5,
                   command=self.reset_preview_zoom).pack(side="right")
        ttk.Button(preview_toolbar, text="−", width=3,
                   command=lambda: self._preview_zoom_factor(1 / 1.5)).pack(side="right")
        ttk.Button(preview_toolbar, text="＋", width=3,
                   command=lambda: self._preview_zoom_factor(1.5)).pack(side="right")
        self.preview = tk.Canvas(right, background="#22252d", highlightthickness=1,
                                 highlightbackground="#555e6b", height=188, cursor="crosshair")
        self.preview.pack(fill="x", expand=False, pady=(6, 0))
        self.preview.bind("<Configure>", lambda _e: self._refresh_preview())
        self.preview.bind("<ButtonPress-1>", self._preview_press)
        self.preview.bind("<B1-Motion>", self._preview_motion)
        self.preview.bind("<ButtonRelease-1>", self._preview_release)
        self.preview.bind("<ButtonPress-3>", self._preview_pan_press)
        self.preview.bind("<B3-Motion>", self._preview_pan_motion)
        self.preview.bind("<ButtonRelease-3>", self._preview_pan_release)
        self.preview.bind("<MouseWheel>", self._preview_wheel)
        self.preview.bind("<Button-4>", self._preview_wheel)
        self.preview.bind("<Button-5>", self._preview_wheel)
        ttk.Label(right, text="左ドラッグ: 範囲を拡大 / 右ドラッグ: 移動 / ホイール: 拡大縮小",
                  foreground="#566173").pack(anchor="w")
        actions = ttk.Frame(self, padding=(12, 11))
        # Reserve a fixed bottom bar *before* allocating height to the body;
        # otherwise Tk's packer can clip Confirm/Cancel on shorter screens.
        actions.pack(side="bottom", fill="x", before=body)
        ttk.Button(actions, text="範囲を描き直す", command=self._arm_redraw).pack(side="left")
        ttk.Button(actions, text="全体を選択", command=self._select_all).pack(side="left", padx=8)
        ttk.Button(actions, text="キャンセル", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="この範囲で配置", command=self.confirm).pack(side="right", padx=8)
        self._sync_fields()
        self._refresh_preview()

    def _entry_grid(self, parent: ttk.Frame, variables: dict[str, tk.StringVar],
                    fields: tuple[tuple[str, str], ...], apply: Any) -> None:
        grid = ttk.Frame(parent)
        grid.pack(fill="x")
        for index, (name, caption) in enumerate(fields):
            r, c = divmod(index, 2)
            label = ttk.Label(grid, text=f"{caption} ◉", cursor="arrow")
            label.grid(row=r * 2, column=c, sticky="w", pady=(2, 2))
            group = "pixel" if variables is self.pixel_vars else "uv"
            entry = ttk.Entry(grid, textvariable=variables[name], width=13)
            entry.grid(row=r * 2 + 1, column=c, sticky="ew", padx=(0, 10))
            for widget in (label, entry):
                widget.bind("<MouseWheel>", lambda event, g=group, n=name:
                            self._field_wheel(event, g, n))
                widget.bind("<Button-4>", lambda event, g=group, n=name:
                            self._field_wheel(event, g, n))
                widget.bind("<Button-5>", lambda event, g=group, n=name:
                            self._field_wheel(event, g, n))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        ttk.Button(parent, text="数値を反映", command=apply).pack(anchor="e", pady=(7, 0))

    def _field_wheel(self, event: tk.Event, group: str, name: str) -> str:
        steps = wheel_steps(event)
        if not steps:
            return "break"
        if self.drag is not None:
            self._mouse_release(None)
        if self._last_edited_group == "pixel" and not self.apply_pixel():
            return "break"
        if self._last_edited_group == "uv" and not self.apply_uv():
            return "break"
        x, y, w, h = self.rect
        for _ in range(abs(steps)):
            direction = 1 if steps > 0 else -1
            if name == "x":
                x = max(0, min(self.image_width - w, x + direction))
            elif name == "y":
                y = max(0, min(self.image_height - h, y + direction))
            elif name == "w":
                w = max(1, min(self.image_width - x, w + direction))
            elif name == "h":
                h = max(1, min(self.image_height - y, h + direction))
            elif name == "u0":
                right = x + w
                x = max(0, min(right - 1, x + direction))
                w = right - x
            elif name == "v0":
                bottom = y + h
                y = max(0, min(bottom - 1, y + direction))
                h = bottom - y
            elif name == "u1":
                w = max(1, min(self.image_width - x, w + direction))
            elif name == "v1":
                h = max(1, min(self.image_height - y, h + direction))
        self._change_rect((x, y, w, h), preview=True)
        return "break"

    def _dialog_scroll(self, event: tk.Event) -> Optional[str]:
        if not hasattr(self, "right_scroll"):
            return None
        # Scroll right-hand form only when the pointer is over that column.
        rx, ry = event.x_root, event.y_root
        x0, y0 = self.right_scroll.winfo_rootx(), self.right_scroll.winfo_rooty()
        if not (x0 <= rx < x0 + self.right_scroll.winfo_width() and
                y0 <= ry < y0 + self.right_scroll.winfo_height()):
            return None
        if hasattr(self, "preview"):
            px, py = self.preview.winfo_rootx(), self.preview.winfo_rooty()
            if px <= rx < px + self.preview.winfo_width() and py <= ry < py + self.preview.winfo_height():
                return None  # Preview owns its zoom wheel.
        steps = wheel_steps(event)
        if steps:
            self.right_scroll.yview_scroll(-steps, "units")
        return "break"

    def nudge_extent(self, axis: str, amount: int) -> None:
        """Adjust width or height only; preserve the crop's top-left origin."""
        if axis not in ("w", "h") or amount not in (-1, 1):
            raise ValueError("範囲操作が不正です。")
        if self._last_edited_group == "pixel" and not self.apply_pixel():
            return
        if self._last_edited_group == "uv" and not self.apply_uv():
            return
        x, y, w, h = self.rect
        if axis == "w":
            w = max(1, min(self.image_width - x, w + amount))
        else:
            h = max(1, min(self.image_height - y, h + amount))
        self._change_rect((x, y, w, h), preview=True)

    def nudge_position_by_extent(self, axis: str, direction: int) -> None:
        """Move the crop by exactly its current width or height, clamped to source."""
        if axis not in ("x", "y") or direction not in (-1, 1):
            raise ValueError("範囲移動操作が不正です。")
        if self._last_edited_group == "pixel" and not self.apply_pixel():
            return
        if self._last_edited_group == "uv" and not self.apply_uv():
            return
        x, y, w, h = self.rect
        if axis == "x":
            x = max(0, min(self.image_width - w, x + direction * w))
        else:
            y = max(0, min(self.image_height - h, y + direction * h))
        self._change_rect((x, y, w, h), preview=True)

    def _apply_enter(self, event: tk.Event) -> None:
        focus = self.focus_get()
        if isinstance(focus, ttk.Entry):
            if focus.cget("textvariable") in [str(v) for v in self.pixel_vars.values()]:
                self.apply_pixel()
            else:
                self.apply_uv()

    def _mark_field_edit(self, group: str) -> None:
        if not self._syncing_fields:
            self._last_edited_group = group

    def _update_image_info(self) -> None:
        self.image_info_var.set(
            f"画像: {reference_name(self.path)}  /  "
            f"{self.image_width} × {self.image_height} px")

    def change_source(self) -> None:
        """Change a single UV image without changing the scene asset on Cancel.

        Maintain normalized UV coordinates even when the new texture size differs.
        Validate everything before swapping the dialog's live image and path.
        """
        selected = filedialog.askopenfilename(
            parent=self, title="UVの元画像（PNG/JPG）を選択",
            initialdir=str(self.path.parent),
            filetypes=[("画像", "*.png *.jpg *.jpeg")])
        if not selected:
            return
        try:
            new_path = ensure_asset_path(Path(selected))
            if new_path.suffix.casefold() not in (".png", ".jpg", ".jpeg"):
                raise ValueError("PNG/JPGのみ指定できます。")
            if self.drag is not None:
                self._mouse_release(None)
            if self._last_edited_group == "pixel" and not self.apply_pixel():
                return
            if self._last_edited_group == "uv" and not self.apply_uv():
                return
            uv = uv_rect_to_json(self.rect, self.source.size)
            with Image.open(new_path) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("画像の画素数が上限を超えています。")
                updated_rect = uv_json_to_rect(uv, image.size)
                updated_image = image.convert("RGBA")
            self.path = new_path
            self.source = updated_image
            self.image_width, self.image_height = updated_image.size
            self.rect = updated_rect
            self._sync_fields()
            self._update_image_info()
            self._resize_canvas()
            self.reset_preview_zoom()
        except (OSError, ValueError, TypeError, MemoryError, UnidentifiedImageError) as exc:
            messagebox.showerror(APP_NAME, f"元画像を変更できませんでした:\n{exc}", parent=self)

    def _sync_fields(self) -> None:
        self._syncing_fields = True
        try:
            for name, value in zip(("x", "y", "w", "h"), self.rect):
                self.pixel_vars[name].set(str(value))
            uv = uv_rect_to_json(self.rect, self.source.size)
            for name, value in uv.items():
                self.uv_vars[name].set(f"{value:.10f}".rstrip("0").rstrip(".") if value else "0")
            self._last_edited_group = None
        finally:
            self._syncing_fields = False

    def _change_rect(self, rect: tuple[int, int, int, int], *, preview: bool) -> None:
        if self.rect != rect:
            self.rect = rect
            self._sync_fields()
            self._draw_overlay()
            if preview:
                self._refresh_preview()

    def apply_pixel(self) -> bool:
        try:
            values = tuple(int(self.pixel_vars[key].get().strip()) for key in ("x", "y", "w", "h"))
            rect = validate_uv_rect(values, self.source.size)
        except (TypeError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"ピクセル範囲を確認してください:\n{exc}", parent=self)
            return False
        self._change_rect(rect, preview=True)
        self._sync_fields()
        return True

    def apply_uv(self) -> bool:
        try:
            raw = {name: self.uv_vars[name].get() for name in ("u0", "v0", "u1", "v1")}
            rect = uv_json_to_rect(raw, self.source.size)
        except (TypeError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"UV座標を確認してください:\n{exc}", parent=self)
            return False
        self._change_rect(rect, preview=True)
        self._sync_fields()
        return True

    def _resize_canvas(self, _event: Any = None) -> None:
        width = max(10, self.canvas.winfo_width() - 20)
        height = max(10, self.canvas.winfo_height() - 20)
        self.scale = min(width / self.image_width, height / self.image_height, 2.0)
        pix_w = max(1, round(self.image_width * self.scale))
        pix_h = max(1, round(self.image_height * self.scale))
        self.origin_x = (self.canvas.winfo_width() - pix_w) / 2
        self.origin_y = (self.canvas.winfo_height() - pix_h) / 2
        preview = self.source.resize((pix_w, pix_h), RESAMPLE)
        self.canvas_photo = ImageTk.PhotoImage(preview)
        self.canvas.delete("all")
        self.canvas.create_image(self.origin_x, self.origin_y, anchor="nw",
                                 image=self.canvas_photo, tags="uvimage")
        self._draw_overlay()

    def _view_xy(self, px: float, py: float) -> tuple[float, float]:
        return (self.origin_x + px * self.scale, self.origin_y + py * self.scale)

    def _pixel_xy(self, sx: float, sy: float) -> tuple[int, int]:
        x = round((sx - self.origin_x) / self.scale)
        y = round((sy - self.origin_y) / self.scale)
        return max(0, min(self.image_width, x)), max(0, min(self.image_height, y))

    def _handles(self) -> dict[str, tuple[float, float]]:
        x, y, w, h = self.rect
        x0, y0 = self._view_xy(x, y)
        x1, y1 = self._view_xy(x + w, y + h)
        return {"nw": (x0, y0), "n": ((x0 + x1) / 2, y0),
                "ne": (x1, y0), "e": (x1, (y0 + y1) / 2),
                "se": (x1, y1), "s": ((x0 + x1) / 2, y1),
                "sw": (x0, y1), "w": (x0, (y0 + y1) / 2)}

    def _draw_overlay(self) -> None:
        self.canvas.delete("uvguide")
        x, y, w, h = self.rect
        x0, y0 = self._view_xy(x, y)
        x1, y1 = self._view_xy(x + w, y + h)
        self.canvas.create_rectangle(x0, y0, x1, y1, outline="#ffb347", width=2,
                                     fill="", tags="uvguide")
        for hx, hy in self._handles().values():
            r = self.HANDLE_SIZE
            self.canvas.create_rectangle(hx - r, hy - r, hx + r, hy + r,
                                         fill="#f9cf8a", outline="#704925", tags="uvguide")

    def _arm_redraw(self) -> None:
        self.force_draw = True
        self.hint_var.set("次のドラッグで新しい選択範囲を作成します。")

    def _select_all(self) -> None:
        self.force_draw = False
        self._change_rect((0, 0, self.image_width, self.image_height), preview=True)

    def _mouse_press(self, event: tk.Event) -> None:
        x, y = self._pixel_xy(event.x, event.y)
        px, py = self._view_xy(x, y)
        if not (self.origin_x <= event.x <= self.origin_x + self.image_width * self.scale and
                self.origin_y <= event.y <= self.origin_y + self.image_height * self.scale):
            return
        mode = None
        if not self.force_draw:
            for name, (hx, hy) in self._handles().items():
                if abs(event.x - hx) <= self.HANDLE_PICK and abs(event.y - hy) <= self.HANDLE_PICK:
                    mode = name
                    break
            if mode is None:
                rx, ry, rw, rh = self.rect
                if rx <= x <= rx + rw and ry <= y <= ry + rh:
                    mode = "move"
        if mode is None:
            mode = "draw"
        self.drag = {"mode": mode, "start": (x, y), "rect": self.rect}
        self.force_draw = False
        self._mouse_move(event)

    def _mouse_move(self, event: tk.Event) -> None:
        if self.drag is None:
            return
        x, y = self._pixel_xy(event.x, event.y)
        mode = self.drag["mode"]
        sx, sy = self.drag["start"]
        ox, oy, ow, oh = self.drag["rect"]
        if mode == "move":
            new_x = max(0, min(self.image_width - ow, ox + x - sx))
            new_y = max(0, min(self.image_height - oh, oy + y - sy))
            rect = (new_x, new_y, ow, oh)
        else:
            left, top, right, bottom = ox, oy, ox + ow, oy + oh
            if mode == "draw":
                left, right = sorted((sx, x))
                top, bottom = sorted((sy, y))
                # A one-pixel rectangle is valid even during the first mouse event.
                if left == right:
                    right = min(self.image_width, left + 1)
                    left = max(0, right - 1)
                if top == bottom:
                    bottom = min(self.image_height, top + 1)
                    top = max(0, bottom - 1)
            else:
                if "w" in mode:
                    left = min(x, right - 1)
                if "e" in mode:
                    right = max(x, left + 1)
                if "n" in mode:
                    top = min(y, bottom - 1)
                if "s" in mode:
                    bottom = max(y, top + 1)
            rect = (left, top, right - left, bottom - top)
        self._change_rect(validate_uv_rect(rect, self.source.size), preview=False)

    def _mouse_release(self, _event: tk.Event) -> None:
        if self.drag is None:
            return
        self.drag = None
        self.hint_var.set("枠内ドラッグ: 移動 / 四隅・辺: サイズ変更 / 『範囲を描き直す』: 新しい範囲")
        self._refresh_preview()

    def reset_preview_zoom(self) -> None:
        self.preview_zoom = 1.0
        self.preview_center = (0.5, 0.5)
        self._refresh_preview()

    def _preview_zoom_factor(self, factor: float) -> None:
        self.preview_zoom = max(1.0, min(64.0, self.preview_zoom * factor))
        self._refresh_preview()

    def _preview_wheel(self, event: tk.Event) -> str:
        steps = wheel_steps(event)
        if steps:
            self._preview_zoom_factor(1.25 ** steps)
        return "break"

    def _preview_press(self, event: tk.Event) -> None:
        self.preview_drag = {"mode": "zoom", "start": (event.x, event.y)}
        self.preview.delete("preview_selection")

    def _preview_motion(self, event: tk.Event) -> None:
        drag = self.preview_drag
        if drag is None or drag["mode"] != "zoom":
            return
        self.preview.delete("preview_selection")
        sx, sy = drag["start"]
        self.preview.create_rectangle(sx, sy, event.x, event.y, outline="#ffbd66",
                                      width=2, dash=(4, 2), tags="preview_selection")

    def _preview_release(self, event: tk.Event) -> None:
        drag = self.preview_drag
        self.preview_drag = None
        self.preview.delete("preview_selection")
        if drag is None or drag["mode"] != "zoom":
            return
        sx, sy = drag["start"]
        w, h = max(1, self.preview.winfo_width()), max(1, self.preview.winfo_height())
        x0, x1 = sorted((max(0, min(w, sx)), max(0, min(w, event.x))))
        y0, y1 = sorted((max(0, min(h, sy)), max(0, min(h, event.y))))
        if x1 - x0 < 6 or y1 - y0 < 6:
            return
        cx, cy = self.preview_center
        scale, _, _ = self._preview_scale()
        image_w, image_h = self.rect[2:]
        # Selection coordinates are relative to the displayed crop center.
        center_x = cx * image_w + ((x0 + x1) / 2 - w / 2) / scale
        center_y = cy * image_h + ((y0 + y1) / 2 - h / 2) / scale
        self.preview_center = (max(0, min(1, center_x / image_w)),
                               max(0, min(1, center_y / image_h)))
        self.preview_zoom = max(1.0, min(64.0, self.preview_zoom *
                            min(w / (x1 - x0), h / (y1 - y0))))
        self._refresh_preview()

    def _preview_pan_press(self, event: tk.Event) -> None:
        self.preview_drag = {"mode": "pan", "start": (event.x, event.y),
                             "center": self.preview_center}

    def _preview_pan_motion(self, event: tk.Event) -> None:
        drag = self.preview_drag
        if drag is None or drag["mode"] != "pan":
            return
        scale, _, _ = self._preview_scale()
        width, height = self.rect[2:]
        cx, cy = drag["center"]
        self.preview_center = (max(0.0, min(1.0, cx - (event.x - drag["start"][0]) / (scale * width))),
                               max(0.0, min(1.0, cy - (event.y - drag["start"][1]) / (scale * height))))
        self._refresh_preview()

    def _preview_pan_release(self, _event: tk.Event) -> None:
        self.preview_drag = None

    def _preview_scale(self) -> tuple[float, int, int]:
        w = max(30, self.preview.winfo_width())
        h = max(30, self.preview.winfo_height())
        crop_w, crop_h = self.rect[2:]
        return min(w / crop_w, h / crop_h) * self.preview_zoom, w, h

    def _refresh_preview(self) -> None:
        if not hasattr(self, "preview") or not self.preview.winfo_exists():
            return
        x, y, w, h = self.rect
        scale, view_w, view_h = self._preview_scale()
        center_x = self.preview_center[0] * w
        center_y = self.preview_center[1] * h
        # Render only visible source pixels, bounded by canvas size at any zoom.
        left = max(0, int(math.floor(center_x - view_w / (2 * scale))))
        top = max(0, int(math.floor(center_y - view_h / (2 * scale))))
        right = min(w, int(math.ceil(center_x + view_w / (2 * scale))))
        bottom = min(h, int(math.ceil(center_y + view_h / (2 * scale))))
        self.preview.delete("preview_image")
        if right <= left or bottom <= top:
            return
        section = self.source.crop((x + left, y + top, x + right, y + bottom))
        scaled_width = max(1, round((right - left) * scale))
        scaled_height = max(1, round((bottom - top) * scale))
        # NEAREST helps identify isolated unwanted source pixels at high zoom.
        resample = Image.Resampling.NEAREST if hasattr(Image, "Resampling") else Image.NEAREST
        image = section.resize((scaled_width, scaled_height), resample)
        self.preview_photo = ImageTk.PhotoImage(image)
        disp_x = view_w / 2 + (left - center_x) * scale
        disp_y = view_h / 2 + (top - center_y) * scale
        self.preview.create_image(disp_x, disp_y, image=self.preview_photo,
                                  anchor="nw", tags="preview_image")
        self.preview.tag_lower("preview_image")

    def confirm(self) -> None:
        if self.drag is not None:
            self._mouse_release(None)
        # Committing a dialog also commits a numeric edit without requiring
        # an extra press on the individual "数値を反映" button.
        if self._last_edited_group == "pixel" and not self.apply_pixel():
            return
        if self._last_edited_group == "uv" and not self.apply_uv():
            return
        self.result = self.rect
        self.destroy()


class WindowNineSliceDialog(tk.Toplevel):
    """Nine-slice panel editor: numeric borders, draggable guides, live rendered preview."""
    def __init__(self, parent: tk.Misc, source: Path,
                 settings: Optional[tuple[int, int, tuple[int, int, int, int], str, str]] = None) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("ウィンドウ画像の9スライス設定")
        self.transient(parent)
        self.source_path = ensure_asset_path(source)
        with Image.open(self.source_path) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("画像の画素数が上限を超えています。")
            self.source_image = image.convert("RGBA")
        self.source_alpha_range = self.source_image.getchannel("A").getextrema()
        sw, sh = self.source_image.size
        if settings is None:
            borders = (max(1, sw // 5), max(1, sw // 5),
                       max(1, sh // 5), max(1, sh // 5))
            if borders[0] + borders[1] >= sw or borders[2] + borders[3] >= sh:
                raise ValueError("9スライスには幅・高さが各3px以上の画像が必要です。")
            settings = (min(WINDOW_MAX_SIDE, max(sw, round(sw * 1.5))),
                        min(WINDOW_MAX_SIDE, max(sh, round(sh * 1.5))), borders,
                        "repeat", "source")
        w, h, (l, r, t, b), mode, _legacy_alpha_mode = settings
        self.vars = {key: tk.StringVar(value=str(value)) for key, value in
            (("width", w), ("height", h), ("left", l), ("right", r),
             ("top", t), ("bottom", b))}
        self.mode_var = tk.StringVar(value=mode)
        # Transparency is no longer an editing option: use source RGBA always.
        self.result: Optional[tuple[int, int, tuple[int, int, int, int], str, str]] = None
        self._source_photo: Optional[ImageTk.PhotoImage] = None
        self._target_photo: Optional[ImageTk.PhotoImage] = None
        self._drag_guide: Optional[str] = None
        self._source_offset = (0.0, 0.0, 1.0)
        self.preview_job: Optional[str] = None
        self.message_var = tk.StringVar()
        self._build()
        self.update_idletasks()
        self.geometry(f"{min(1010, self.winfo_screenwidth()-40)}x{min(680, self.winfo_screenheight()-80)}")
        self.deiconify()
        self.grab_set()
        self.refresh()
        self.wait_window(self)

    def _build(self) -> None:
        parent = ttk.Frame(self, padding=10)
        parent.pack(fill="both", expand=True)
        ttk.Label(parent, text="四隅は原寸のまま固定し、辺と中央をリピート（端数カット）してサイズを変更します。",
                  wraplength=950).pack(anchor="w", pady=(0, 6))
        area = ttk.Frame(parent)
        area.pack(fill="both", expand=True)
        left = ttk.LabelFrame(area, text="元画像と分割線（線の近くをドラッグして変更）", padding=6)
        left.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.source_canvas = tk.Canvas(left, bg="#2a2e38", highlightthickness=0, height=325)
        self.source_canvas.pack(fill="both", expand=True)
        self.source_canvas.bind("<Configure>", lambda e: self._draw_source())
        self.source_canvas.bind("<ButtonPress-1>", self._press)
        self.source_canvas.bind("<B1-Motion>", self._motion)
        self.source_canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag_guide", None))
        right = ttk.LabelFrame(area, text="完成プレビュー", padding=6)
        right.pack(side="left", fill="both", expand=True)
        self.result_canvas = tk.Canvas(right, bg="#2a2e38", highlightthickness=0, height=325)
        self.result_canvas.pack(fill="both", expand=True)
        self.result_canvas.bind("<Configure>", lambda e: self.refresh())
        inputs = ttk.Frame(parent)
        inputs.pack(fill="x", pady=(8, 0))
        for col, (name, label) in enumerate((("width", "幅"), ("height", "高さ"),
                        ("left", "左"), ("right", "右"), ("top", "上"), ("bottom", "下"))):
            box = ttk.Frame(inputs)
            box.grid(row=0, column=col, sticky="ew", padx=(0, 7))
            ttk.Label(box, text=label + " (px)").pack(anchor="w")
            edit = ttk.Entry(box, width=9, textvariable=self.vars[name])
            edit.pack(fill="x")
            edit.bind("<Return>", lambda e: self.refresh())
            edit.bind("<FocusOut>", lambda e: self.refresh())
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                edit.bind(seq, lambda e, n=name: self._wheel(e, n))
            inputs.columnconfigure(col, weight=1)
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(8, 2))
        ttk.Label(actions, text="辺・中央の処理:").pack(side="left")
        ttk.Combobox(actions, state="readonly", textvariable=self.mode_var,
                     values=WINDOW_MODES, width=10).pack(side="left", padx=6)
        ttk.Button(actions, text="画像を変更…", command=self._change_source).pack(side="left", padx=10)
        ttk.Button(actions, text="プレビュー更新", command=self.refresh).pack(side="left", padx=4)
        ttk.Label(actions, text="repeat: 原寸反復＋端数切り取り / stretch: 辺・中央を伸縮",
                  foreground="#636c78").pack(side="right")
        ttk.Label(parent,
                  text="透明度: 元画像のアルファ値をそのまま使用します（自動的な不透明化は行いません）。",
                  foreground="#636c78").pack(anchor="w", pady=(5, 0))
        ttk.Label(parent, textvariable=self.message_var, foreground="#a05a36").pack(anchor="w", pady=(4, 0))
        footer = ttk.Frame(parent)
        footer.pack(fill="x", pady=(6, 0))
        ttk.Button(footer, text="キャンセル", command=self.destroy).pack(side="right", padx=5)
        ttk.Button(footer, text="この設定で配置 / 適用", command=self.confirm).pack(side="right", padx=5)
        self.mode_var.trace_add("write", lambda *_: self.refresh())

    def _read(self) -> tuple[int, int, tuple[int, int, int, int], str, str]:
        try:
            values = {key: int(var.get()) for key, var in self.vars.items()}
        except (ValueError, OverflowError) as exc:
            raise ValueError("サイズと分割幅は整数で入力してください。") from exc
        borders = (values["left"], values["right"], values["top"], values["bottom"])
        validate_window_spec(self.source_image.size, values["width"], values["height"], borders,
                             self.mode_var.get())
        return values["width"], values["height"], borders, self.mode_var.get(), "source"

    def _wheel(self, event: tk.Event, field: str) -> str:
        delta = 1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else -1
        try:
            self.vars[field].set(str(int(self.vars[field].get()) + delta))
            self.refresh()
        except ValueError:
            pass
        return "break"

    def _change_source(self) -> None:
        chosen = filedialog.askopenfilename(parent=self, title="ウィンドウ用画像を選択",
            initialdir=str(self.source_path.parent),
            filetypes=[("PNG/JPG", "*.png *.jpg *.jpeg")])
        if not chosen:
            return
        try:
            path = ensure_asset_path(Path(chosen))
            with Image.open(path) as img:
                if img.width * img.height > MAX_IMAGE_PIXELS:
                    raise ValueError("画像の画素数が上限を超えています。")
                converted = img.convert("RGBA")
            if converted.width < 3 or converted.height < 3:
                raise ValueError("画像には幅・高さとも3px以上必要です。")
            self.source_image = converted
            self.source_alpha_range = converted.getchannel("A").getextrema()
            self.source_path = path
            self.refresh()
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)

    def _draw_source(self) -> None:
        canvas = self.source_canvas
        canvas.delete("all")
        cw, ch = max(canvas.winfo_width(), 10), max(canvas.winfo_height(), 10)
        sw, sh = self.source_image.size
        scale = min((cw-20)/sw, (ch-20)/sh)
        ox, oy = (cw-sw*scale)/2, (ch-sh*scale)/2
        self._source_offset = ox, oy, scale
        resized = self.source_image.resize((max(1, round(sw*scale)), max(1, round(sh*scale))), RESAMPLE_NEAREST)
        self._source_photo = ImageTk.PhotoImage(resized)
        canvas.create_image(ox, oy, anchor="nw", image=self._source_photo)
        try:
            l, r, t, b = (int(self.vars[name].get()) for name in ("left", "right", "top", "bottom"))
            for v in (l, sw-r):
                canvas.create_line(ox+v*scale, oy, ox+v*scale, oy+sh*scale, fill="#f5a623", width=2)
            for v in (t, sh-b):
                canvas.create_line(ox, oy+v*scale, ox+sw*scale, oy+v*scale, fill="#f5a623", width=2)
        except ValueError:
            pass

    def _press(self, event: tk.Event) -> None:
        ox, oy, zoom = self._source_offset
        sw, sh = self.source_image.size
        try:
            l, r, t, b = (int(self.vars[name].get()) for name in ("left", "right", "top", "bottom"))
        except ValueError:
            return
        guides = (("left", ox+l*zoom, "x"), ("right", ox+(sw-r)*zoom, "x"),
                  ("top", oy+t*zoom, "y"), ("bottom", oy+(sh-b)*zoom, "y"))
        matches = [(abs((event.x if axis == "x" else event.y)-pos), key) for key, pos, axis in guides]
        distance, name = min(matches)
        self._drag_guide = name if distance <= 10 else None

    def _motion(self, event: tk.Event) -> None:
        if not self._drag_guide:
            return
        ox, oy, zoom = self._source_offset
        sw, sh = self.source_image.size
        name = self._drag_guide
        coord = round((event.x-ox)/zoom) if name in ("left", "right") else round((event.y-oy)/zoom)
        if name == "right": coord = sw-coord
        if name == "bottom": coord = sh-coord
        other = int(self.vars[{"left":"right", "right":"left", "top":"bottom", "bottom":"top"}[name]].get())
        maximum = (sw if name in ("left", "right") else sh) - other - 1
        self.vars[name].set(str(max(1, min(maximum, coord))))
        self.refresh()

    def refresh(self) -> None:
        self._draw_source()
        try:
            w, h, borders, mode, alpha_mode = self._read()
            preview_scale = min(1.0, 410/w, 295/h)
            panel = render_nine_slice(self.source_image, w, h, borders, mode, alpha_mode)
            if preview_scale < 1:
                panel = panel.resize((max(1, round(w*preview_scale)), max(1, round(h*preview_scale))), RESAMPLE_NEAREST)
            # Checkerboard is preview-only; rendering preserves the input RGBA.
            check = Image.new("RGBA", panel.size, (93, 99, 111, 255))
            checker_draw = ImageDraw.Draw(check)
            tile_size = 12
            for y in range(0, panel.height, tile_size):
                for x in range(0, panel.width, tile_size):
                    if ((x // tile_size) + (y // tile_size)) % 2:
                        checker_draw.rectangle((x, y,
                            min(x + tile_size - 1, panel.width - 1),
                            min(y + tile_size - 1, panel.height - 1)),
                            fill=(179, 185, 195, 255))
            check.alpha_composite(panel)
            self._target_photo = ImageTk.PhotoImage(check)
            self.result_canvas.delete("all")
            self.result_canvas.create_image(self.result_canvas.winfo_width()/2,
                self.result_canvas.winfo_height()/2, image=self._target_photo, anchor="center")
            low, high = self.source_alpha_range
            self.message_var.set(
                f"入力画像: {self.source_path.name}（{self.source_image.width}×{self.source_image.height}px） "
                f"/ 元画像アルファ: {low}～{high}（255=不透明・元画像の値を保持）")
        except (ValueError, MemoryError, OSError) as exc:
            self.message_var.set(str(exc))

    def confirm(self) -> None:
        try:
            settings = self._read()
            # Ensure the selected dimensions are feasible and will render.
            if settings[0] * settings[1] > MAX_RENDER_PIXELS:
                raise ValueError("ウィンドウサイズが描画上限を超えています。")
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        self.result = settings
        self.destroy()


class ButtonUVDialog(UVSelectionDialog):
    """Two independent UV selections (normal / held), with optional linked SE.

    Reuses the tested drag, eight handles, pixel coordinates and normalized UV
    inputs from UVSelectionDialog. Only the active selection is modified.
    """

    PREVIEW_MAX = (180, 96)  # Keep both state previews and confirmation buttons visible.

    def __init__(self, parent: tk.Misc, path: Path,
                 normal: Optional[tuple[int, int, int, int]] = None,
                 pressed: Optional[tuple[int, int, int, int]] = None,
                 se: Optional[str] = None, se_keys: Sequence[str] = (),
                 pressed_path: Optional[Path] = None,
                 toggle_mode: bool = False, default_state: str = "off") -> None:
        if default_state not in ("off", "on"):
            raise ValueError("トグル初期状態は off または on を指定してください。")
        self.toggle_mode = toggle_mode
        self._initial_default = default_state
        self._initial_pressed_path = pressed_path or path
        self._initial_pressed = pressed
        self._initial_se = se
        self._se_keys = tuple(sorted(set(se_keys)))
        self.active_state = "normal"
        self.state_rects: dict[str, tuple[int, int, int, int]] = {}
        self.other_photo: Optional[ImageTk.PhotoImage] = None
        super().__init__(parent, path, normal)

    def _build_ui(self) -> None:
        self.state_sources: dict[str, Path] = {
            "normal": self.path, "pressed": self._initial_pressed_path,
        }
        with Image.open(self._initial_pressed_path) as _pressed_image:
            if _pressed_image.width * _pressed_image.height > MAX_IMAGE_PIXELS:
                raise ValueError("押下中画像の画素数が上限を超えています。")
            pressed_size = _pressed_image.size
        self.state_rects = {
            "normal": self.rect,
            "pressed": validate_uv_rect(self._initial_pressed or
                (0, 0, *pressed_size), pressed_size),
        }
        controls = ttk.Frame(self, padding=(12, 8))
        controls.pack(fill="x")
        ttk.Label(controls, text="編集するボタン画像:").pack(side="left")
        self.state_var = tk.StringVar(value="normal")
        ttk.Radiobutton(controls, text="① OFF" if self.toggle_mode else "① 通常（押す前）", value="normal",
                        variable=self.state_var, command=self._switch_state).pack(side="left", padx=(10, 5))
        ttk.Radiobutton(controls, text="② ON" if self.toggle_mode else "② 押下中", value="pressed",
                        variable=self.state_var, command=self._switch_state).pack(side="left", padx=5)
        ttk.Label(controls, text="切替SE:" if self.toggle_mode else "押下SE:").pack(side="left", padx=(25, 5))
        options = ["なし"] + [f"se.{key}" for key in self._se_keys]
        self.se_var = tk.StringVar(value=f"se.{self._initial_se}" if self._initial_se else "なし")
        if self.se_var.get() not in options:
            options.append(self.se_var.get())
        self.se_combo = ttk.Combobox(controls, state="readonly", width=24,
                                     values=options, textvariable=self.se_var)
        self.se_combo.pack(side="left", fill="x", expand=True)
        source_row = ttk.Frame(self, padding=(12, 0))
        source_row.pack(fill="x")
        self.source_label_var = tk.StringVar(value=f"編集中の画像: {self.path.name}")
        ttk.Label(source_row, textvariable=self.source_label_var).pack(side="left", fill="x", expand=True)
        ttk.Button(source_row, text="この状態の画像を変更…",
                   command=self.change_state_source).pack(side="right")
        copy_row = ttk.Frame(self, padding=(12, 0))
        copy_row.pack(fill="x")
        ttk.Label(copy_row, text="UV範囲のコピー:").pack(side="left", padx=(0, 8))
        ttk.Button(copy_row, text="通常 → 押下中へコピー",
                   command=lambda: self.copy_state_rect("normal", "pressed")).pack(side="left", padx=(0, 8))
        ttk.Button(copy_row, text="押下中 → 通常へコピー",
                   command=lambda: self.copy_state_rect("pressed", "normal")).pack(side="left")
        if self.toggle_mode:
            initial_row = ttk.Frame(self, padding=(12, 5))
            initial_row.pack(fill="x")
            ttk.Label(initial_row, text="配置時の初期状態:").pack(side="left")
            self.default_var = tk.StringVar(value=self._initial_default)
            ttk.Radiobutton(initial_row, text="OFF", variable=self.default_var,
                            value="off").pack(side="left", padx=(10, 8))
            ttk.Radiobutton(initial_row, text="ON", variable=self.default_var,
                            value="on").pack(side="left", padx=(0, 8))
        super()._build_ui()
        # Only button-style assets have per-state trimming controls.
        trim_row = ttk.Frame(self.preview.master, padding=(0, 3))
        trim_row.pack(fill="x", before=self.preview)
        ttk.Label(trim_row, text="幅・高さ ±1px:").pack(side="left")
        for caption, axis, amount in (("幅−", "w", -1), ("幅＋", "w", 1),
                                      ("高さ−", "h", -1), ("高さ＋", "h", 1)):
            ttk.Button(trim_row, text=caption, width=5,
                       command=lambda a=axis, d=amount: self.nudge_extent(a, d)).pack(side="left", padx=1)
        self.title(f"{'トグルボタン' if self.toggle_mode else 'ボタン'}をUVで配置 / 2状態を編集 - {self.path.name}")
        shift_row = ttk.Frame(self.preview.master, padding=(0, 3))
        shift_row.pack(fill="x", before=self.preview)
        ttk.Label(shift_row, text="範囲移動（幅/高さ単位）:").pack(side="left")
        for caption, axis, direction in (("X−幅", "x", -1), ("X＋幅", "x", 1),
                                         ("Y−高さ", "y", -1), ("Y＋高さ", "y", 1)):
            ttk.Button(shift_row, text=caption,
                       command=lambda a=axis, d=direction: self.nudge_position_by_extent(a, d)).pack(
                       side="left", padx=1)
        self.hint_var.set("OFF / ONのUV範囲と初期状態を選択してください。" if self.toggle_mode else
                          "①②を切り替えて、それぞれをドラッグまたは数値で指定してください。")
        self.preview.pack_configure(fill="x", expand=False)
        ttk.Label(self.preview.master, text="反対側の状態", padding=(0, 5)).pack(anchor="w")
        self.other_preview = ttk.Label(self.preview.master, anchor="center")
        self.other_preview.pack(fill="x", expand=False)
        self._draw_overlay()

    def _change_rect(self, rect: tuple[int, int, int, int], *, preview: bool) -> None:
        self.state_rects[self.active_state] = rect
        super()._change_rect(rect, preview=preview)

    def copy_state_rect(self, source_state: str, target_state: str) -> None:
        """Copy the source UV into the target state, without editing the source image.

        Pending numerical edits are applied first. The destination is then selected
        so users can immediately fine-tune the pasted bounds.
        """
        if source_state not in self.state_rects or target_state not in self.state_rects:
            raise ValueError("UVのコピー対象が不正です。")
        if self.drag is not None:
            self._mouse_release(None)
        if self._last_edited_group == "pixel" and not self.apply_pixel():
            return
        if self._last_edited_group == "uv" and not self.apply_uv():
            return
        self.state_rects[self.active_state] = self.rect
        copied = self.state_rects[source_state]
        with Image.open(self.state_sources[source_state]) as source_image:
            source_uv = uv_rect_to_json(copied, source_image.size)
        with Image.open(self.state_sources[target_state]) as target_image:
            copied = uv_json_to_rect(source_uv, target_image.size)
        self.state_rects[target_state] = copied
        self.active_state = target_state
        self.state_var.set(target_state)
        self._load_state_image(target_state)
        self.rect = copied
        self._last_edited_group = None
        self._sync_fields()
        self._draw_overlay()
        self._refresh_preview()
        source_label = ("OFF" if source_state == "normal" else "ON") if self.toggle_mode else ("通常" if source_state == "normal" else "押下中")
        target_label = ("OFF" if target_state == "normal" else "ON") if self.toggle_mode else ("通常" if target_state == "normal" else "押下中")
        self.hint_var.set(f"{source_label}のUV範囲を{target_label}へコピーしました。")

    def _load_state_image(self, which: str) -> None:
        """Replace the editor texture without altering the other button state."""
        path = ensure_asset_path(self.state_sources[which])
        with Image.open(path) as reader:
            if reader.width * reader.height > MAX_IMAGE_PIXELS:
                raise ValueError("画像の画素数が上限を超えています。")
            image = reader.convert("RGBA")
        self.path = path
        self.source = image
        self.image_width, self.image_height = image.size
        self.source_label_var.set(f"編集中の画像: {reference_name(path)}")
        self._update_image_info()
        self._resize_canvas()

    def change_state_source(self) -> None:
        """Allow idle and on-push frames to reference different texture files."""
        selected = filedialog.askopenfilename(
            parent=self, title="この状態で使うPNG/JPGを選択",
            initialdir=str(self.state_sources[self.active_state].parent),
            filetypes=[("画像", "*.png *.jpg *.jpeg")])
        if not selected:
            return
        try:
            new_path = ensure_asset_path(Path(selected))
            if new_path.suffix.casefold() not in (".png", ".jpg", ".jpeg"):
                raise ValueError("PNG/JPGのみ指定できます。")
            if self._last_edited_group == "pixel" and not self.apply_pixel():
                return
            if self._last_edited_group == "uv" and not self.apply_uv():
                return
            self.state_rects[self.active_state] = self.rect
            # Preserve normalized UV selection when switching texture dimensions.
            old_uv = uv_rect_to_json(self.rect, self.source.size)
            with Image.open(new_path) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("画像の画素数が上限を超えています。")
                next_rect = uv_json_to_rect(old_uv, image.size)
            self.state_sources[self.active_state] = new_path
            self._load_state_image(self.active_state)
            self.rect = next_rect
            self.state_rects[self.active_state] = next_rect
            self._sync_fields()
            self._draw_overlay()
            self.reset_preview_zoom()
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            messagebox.showerror(APP_NAME, f"画像を変更できませんでした:\n{exc}", parent=self)

    def _switch_state(self) -> None:
        next_state = self.state_var.get()
        if next_state == self.active_state:
            return
        if self.drag is not None:
            self._mouse_release(None)
        if self._last_edited_group == "pixel" and not self.apply_pixel():
            self.state_var.set(self.active_state)
            return
        if self._last_edited_group == "uv" and not self.apply_uv():
            self.state_var.set(self.active_state)
            return
        self.state_rects[self.active_state] = self.rect
        self.active_state = next_state
        self._load_state_image(next_state)
        self.rect = self.state_rects[next_state]
        self._sync_fields()
        self._draw_overlay()
        self.reset_preview_zoom()

    def _draw_overlay(self) -> None:
        super()._draw_overlay()
        other_key = "pressed" if self.active_state == "normal" else "normal"
        if other_key not in self.state_rects:
            return
        if self.state_sources[other_key] != self.state_sources[self.active_state]:
            return  # Pixel-space coordinates differ for independent source images.
        x, y, w, h = self.state_rects[other_key]
        x0, y0 = self._view_xy(x, y)
        x1, y1 = self._view_xy(x + w, y + h)
        self.canvas.create_rectangle(x0, y0, x1, y1,
                                     outline="#32c8df", width=2, dash=(4, 4), tags="uvguide")

    def _refresh_preview(self) -> None:
        super()._refresh_preview()
        if not hasattr(self, "other_preview"):
            return
        other_key = "pressed" if self.active_state == "normal" else "normal"
        x, y, w, h = self.state_rects[other_key]
        with Image.open(self.state_sources[other_key]) as preview_source:
            cropped = preview_source.crop((x, y, x + w, y + h)).convert("RGBA")
        cropped.thumbnail((135, 60), RESAMPLE)
        self.other_photo = ImageTk.PhotoImage(cropped)
        self.other_preview.configure(image=self.other_photo)

    def confirm(self) -> None:
        if self.drag is not None:
            self._mouse_release(None)
        if self._last_edited_group == "pixel" and not self.apply_pixel():
            return
        if self._last_edited_group == "uv" and not self.apply_uv():
            return
        self.state_rects[self.active_state] = self.rect
        selection = self.se_var.get()
        if selection != "なし" and not (selection.startswith("se.") and selection[3:] in self._se_keys):
            messagebox.showerror(APP_NAME, "押下SEは登録済みのSEから選択してください。", parent=self)
            return
        # Commit state-specific paths and UVs atomically.
        result = (self.state_rects["normal"], self.state_rects["pressed"],
                  selection[3:] if selection != "なし" else None,
                  self.state_sources["pressed"])
        self.result = result + (self.default_var.get(),) if self.toggle_mode else result
        self.destroy()



class PlistButtonStateDialog(tk.Toplevel):
    """Choose two atlas sprites and optional SE, without changing the original plist."""

    def __init__(self, parent: tk.Misc, source: Path, normal_key: str,
                 pressed_key: Optional[str], se: Optional[str],
                 se_keys: Sequence[str], *, toggle_mode: bool = False,
                 default_state: str = "off") -> None:
        if default_state not in ("off", "on"):
            raise ValueError("トグル初期状態は off / on のどちらかを指定してください。")
        super().__init__(parent)
        self.toggle_mode = toggle_mode
        self.title(f"{'トグルボタン' if toggle_mode else 'ボタン'}の状態を選択 - {source.name}")
        self.transient(parent)
        self.minsize(530, 340)
        self.geometry("640x455" if toggle_mode else "640x405")
        self.result: Optional[tuple[Any, ...]] = None
        self.default_var = tk.StringVar(value=default_state)
        self.atlas = load_atlas(source)
        self.frames = {frame.name: frame for frame in self.atlas.frames}
        if not self.frames:
            self.destroy()
            raise ValueError("plist内に選択可能なスプライトがありません。")
        if normal_key not in self.frames:
            self.destroy()
            raise ValueError(f"通常スプライトが見つかりません: {normal_key}")
        self.normal_var = tk.StringVar(value=normal_key)
        pressed = pressed_key if pressed_key in self.frames else normal_key
        self.pressed_var = tk.StringVar(value=pressed)
        self.se_keys = tuple(sorted(se_keys))
        self.se_var = tk.StringVar(value=f"se.{se}" if se in self.se_keys else "なし")
        self._photos: dict[str, ImageTk.PhotoImage] = {}
        self._previews: dict[str, ttk.Label] = {}
        self._build_ui()
        self.update_idletasks()
        width, height = 640, min(455 if self.toggle_mode else 405,
                                 max(340, self.winfo_screenheight() - 70))
        x = max(0, min(parent.winfo_rootx() + (parent.winfo_width() - width) // 2,
                       self.winfo_screenwidth() - width))
        y = max(0, min(parent.winfo_rooty() + (parent.winfo_height() - height) // 2,
                       self.winfo_screenheight() - height))
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.bind("<Escape>", lambda _event: self.destroy())
        self.grab_set()
        self.wait_window(self)

    def _build_ui(self) -> None:
        ttk.Label(self, text=("plist内のOFF・ONスプライトを指定してください。"
                              if self.toggle_mode else
                              "plist内の通常時・押下中スプライトを指定してください。"),
                  padding=(12, 10)).pack(anchor="w")
        body = ttk.Frame(self, padding=(12, 0))
        body.pack(fill="both", expand=True)
        for col, (label, var, tag) in enumerate((
                ("OFF" if self.toggle_mode else "通常（押下前）", self.normal_var, "normal"),
                ("ON" if self.toggle_mode else "押下中", self.pressed_var, "pressed"))):
            panel = ttk.LabelFrame(body, text=label, padding=10)
            panel.grid(row=0, column=col, sticky="nsew", padx=5)
            select = ttk.Combobox(panel, state="readonly", textvariable=var,
                                  values=tuple(self.frames), width=29)
            select.pack(fill="x")
            preview = ttk.Label(panel, anchor="center", text="")
            preview.pack(fill="both", expand=True, pady=8)
            self._previews[tag] = preview
            select.bind("<<ComboboxSelected>>", lambda _e, which=tag: self._update_preview(which))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        sound = ttk.Frame(self, padding=(15, 8))
        sound.pack(fill="x")
        ttk.Label(sound, text="切替SE" if self.toggle_mode else "押下SE").pack(side="left", padx=(0, 9))
        ttk.Combobox(sound, textvariable=self.se_var, state="readonly",
                     values=["なし"] + [f"se.{key}" for key in self.se_keys],
                     width=36).pack(side="left", fill="x", expand=True)
        if self.toggle_mode:
            initial = ttk.Frame(self, padding=(15, 3))
            initial.pack(fill="x")
            ttk.Label(initial, text="配置時の初期状態").pack(side="left", padx=(0, 10))
            for label, value in (("OFF", "off"), ("ON", "on")):
                ttk.Radiobutton(initial, text=label, value=value,
                                variable=self.default_var).pack(side="left", padx=(0, 12))
        actions = ttk.Frame(self, padding=12)
        actions.pack(fill="x")
        ttk.Button(actions, text="キャンセル", command=self.destroy).pack(side="right")
        ttk.Button(actions, text=("トグルボタンを配置" if self.toggle_mode else "ボタンに変更"),
                   command=self.confirm).pack(side="right", padx=9)
        self._update_preview("normal")
        self._update_preview("pressed")

    def _update_preview(self, which: str) -> None:
        key = self.normal_var.get() if which == "normal" else self.pressed_var.get()
        if key not in self.frames:
            return
        image = extract_sprite(self.atlas.image, self.frames[key])
        image.thumbnail((245, 210), RESAMPLE)
        self._photos[which] = ImageTk.PhotoImage(image)
        self._previews[which].configure(image=self._photos[which], text="")

    def confirm(self) -> None:
        normal = self.normal_var.get()
        pressed = self.pressed_var.get()
        if normal not in self.frames or pressed not in self.frames:
            messagebox.showerror(APP_NAME, "通常・押下中スプライトを選択してください。", parent=self)
            return
        selected_se = self.se_var.get()
        if selected_se != "なし" and not (
                selected_se.startswith("se.") and selected_se[3:] in self.se_keys):
            messagebox.showerror(APP_NAME, "登録済みのSEを選択してください。", parent=self)
            return
        result = (normal, pressed, selected_se[3:] if selected_se != "なし" else None)
        self.result = result + (self.default_var.get(),) if self.toggle_mode else result
        self.destroy()


@dataclass
class _AudioVoice:
    kind: str
    handle: str
    process: Any = None
    looping: bool = False
    started: float = field(default_factory=time.monotonic)
    original: Optional[Path] = None
    key: str = ""


class AudioPlayer:
    """Independent BGM/SE playback using WinMM or ffplay.

    HCA files use the nearby vgmstream-win64/vgmstream-cli.exe to decode a
    single non-looping WAV. Windows OGG uses FFmpeg; WAV/MP3 use Windows MCI.
    Decoding runs outside the GUI thread and is cached in a temporary directory.
    The BGM playback layer loops the complete decoded WAV as needed.
    """

    def __init__(self, parent: tk.Tk, status: tk.StringVar) -> None:
        self.parent = parent
        self.status = status
        self.is_windows = sys.platform == "win32"
        self.voices: dict[str, _AudioVoice] = {}
        self.generation: dict[str, int] = {"bgm": 0, "se": 0}
        self.queue: queue.Queue[tuple[str, int, Optional[Path], bool, Optional[str]]] = queue.Queue()
        self.cache: dict[tuple[str, int, int], Path] = {}
        self.tmpdir = tempfile.TemporaryDirectory(prefix="scene_ui_audio_")
        self._poll_job: Optional[str] = None
        self._workers = 0
        self._closed = False
        self._ffmpeg: Optional[str] = None
        self._ffmpeg_checked = False
        self._ffplay = shutil.which("ffplay")
        self._winmm = ctypes.windll.winmm if self.is_windows else None
        # Preview-only levels, deliberately omitted from every scene JSON.
        # The two channels are independent (100 = unattenuated source).
        self.volumes: dict[str, int] = {"bgm": 25, "se": 35}
        self._gain_cache: dict[tuple[Any, ...], Path] = {}
        self._pending_play: dict[str, tuple[int, str, Path, int]] = {}
        self._current_bgm: Optional[tuple[str, Path]] = None
        self._volume_restart_job: Optional[str] = None

    @staticmethod
    def _vgmstream_path() -> Optional[Path]:
        """Only use the explicitly supplied local decoder, never shell/PATH search."""
        script_folder = Path(__file__).resolve().parent
        candidates = [script_folder / VGMSTREAM_RELATIVE_EXE]
        # Also support launching from a dedicated working directory.
        working_candidate = Path.cwd().resolve() / VGMSTREAM_RELATIVE_EXE
        if working_candidate != candidates[0]:
            candidates.append(working_candidate)
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    def _mci(self, command: str) -> str:
        output = ctypes.create_unicode_buffer(1024)
        assert self._winmm is not None
        error = self._winmm.mciSendStringW(command, output, len(output), 0)
        if error:
            detail = ctypes.create_unicode_buffer(512)
            self._winmm.mciGetErrorStringW(error, detail, len(detail))
            raise OSError(f"Windows音声再生: {detail.value or error}")
        return output.value.strip()

    def _ffmpeg_path(self, *, request_install: bool) -> Optional[str]:
        if self._ffmpeg and Path(self._ffmpeg).is_file():
            return self._ffmpeg
        if not self._ffmpeg_checked:
            self._ffmpeg_checked = True
            path = shutil.which("ffmpeg")
            if path:
                self._ffmpeg = path
                return path
            try:
                import imageio_ffmpeg
                path = imageio_ffmpeg.get_ffmpeg_exe()
                if Path(path).is_file():
                    self._ffmpeg = path
                    return path
            except (ImportError, RuntimeError, OSError):
                pass
        if not request_install:
            return None
        if not self.is_windows:
            return None
        approved = messagebox.askyesno(
            APP_NAME + " - HCA / OGG 再生",
            "この音声形式を試聴するにはFFmpegが必要です。\n"
            "音声変換ライブラリ imageio-ffmpeg をpipでインストールしますか？\n"
            "（FFmpeg実行ファイルが含まれ、ネットワーク接続が必要です）",
            parent=self.parent,
        )
        if not approved:
            return None
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "imageio-ffmpeg"],
                capture_output=True, text=True, timeout=240, check=False,
            )
            if proc.returncode != 0:
                raise OSError((proc.stderr or proc.stdout or "pip失敗")[-1000:])
            import importlib
            importlib.invalidate_caches()
            import imageio_ffmpeg
            path = imageio_ffmpeg.get_ffmpeg_exe()
            if not Path(path).is_file():
                raise OSError("FFmpeg実行ファイルが見つかりません。")
            self._ffmpeg = path
            return path
        except (OSError, subprocess.TimeoutExpired, ImportError, RuntimeError) as exc:
            messagebox.showerror(APP_NAME, f"FFmpegの準備に失敗しました:\n{exc}", parent=self.parent)
            return None

    def _decode(self, source: Path, executable: str, *, use_vgmstream: bool = False) -> Path:
        stat = source.stat()
        cache_key = (str(source.resolve()), stat.st_mtime_ns, stat.st_size,
                     "vgmstream" if use_vgmstream else "ffmpeg")
        cached = self.cache.get(cache_key)
        if cached is not None and cached.is_file():
            return cached
        output = Path(self.tmpdir.name) / (uuid.uuid4().hex + ".wav")
        if use_vgmstream:
            # -i: do not bake source loop repetitions into the converted WAV.
            # Playback looping is controlled by this editor's BGM channel.
            command = [executable, "-i", "-o", str(output), str(source)]
        else:
            command = [executable, "-nostdin", "-v", "error", "-y", "-i", str(source),
                       "-vn", "-t", str(AUDIO_MAX_DECODE_SECONDS),
                       "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(output)]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, errors="replace",
                timeout=AUDIO_DECODE_TIMEOUT, check=False,
                # vgmstream's Windows release may load DLLs from its own folder.
                cwd=str(Path(executable).parent) if use_vgmstream else None,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0 or not output.is_file():
                reason = (result.stderr or result.stdout or "デコーダーがWAVを生成できませんでした。")[-800:]
                raise ValueError(f"音声デコードに失敗しました: {reason}")
            size = output.stat().st_size
            if size <= 44 or size > AUDIO_MAX_DECODED_BYTES:
                raise ValueError("変換したWAVが空、またはサイズ上限を超えています。")
            self.cache[cache_key] = output
            return output
        except BaseException:
            output.unlink(missing_ok=True)
            raise

    def _gain_wav(self, source: Path, volume: int) -> Path:
        """Create a per-channel PCM WAV preview; never change the source file.

        Windows' waveaudio MCI driver does NOT implement `setaudio ... volume`.
        Software attenuation of a temporary PCM WAV is therefore required for
        WAV, decoded HCA and decoded OGG. PCM 8/16/24/32 bit is supported using
        only the standard library (including Python 3.13 without audioop).
        """
        volume = max(0, min(100, int(volume)))
        if volume == 100:
            return source
        stat = source.stat()
        token = (str(source.resolve()), stat.st_mtime_ns, stat.st_size, volume)
        cached = self._gain_cache.get(token)
        if cached is not None and cached.is_file():
            return cached
        target = Path(self.tmpdir.name) / ("gain_" + uuid.uuid4().hex + ".wav")
        try:
            with wave.open(str(source), 'rb') as reader:
                params = reader.getparams()
                width = params.sampwidth
                if width not in (1, 2, 3, 4):
                    raise ValueError(f"音量調整に未対応のWAV量子化幅です: {width}")
                with wave.open(str(target), 'wb') as writer:
                    writer.setparams(params)
                    while True:
                        chunk = reader.readframes(8192)
                        if not chunk:
                            break
                        if width == 1:
                            # 8-bit WAV PCM is unsigned, with silence at 128.
                            converted = bytes(max(0,min(255,round((b-128)*volume/100+128)))
                                              for b in chunk)
                        elif width in (2, 4):
                            import sys as _sys
                            data = array.array('h' if width == 2 else 'i')
                            data.frombytes(chunk)
                            if _sys.byteorder != 'little':
                                data.byteswap()
                            upper = (1 << (width*8-1))-1
                            lower = -upper-1
                            converted_data = array.array(data.typecode,
                                (max(lower,min(upper,round(value*volume/100))) for value in data))
                            if _sys.byteorder != 'little':
                                converted_data.byteswap()
                            converted = converted_data.tobytes()
                        else:
                            # Signed little-endian 24-bit PCM.
                            bound = 1 << 23
                            out = bytearray()
                            for i in range(0,len(chunk),3):
                                sample = int.from_bytes(chunk[i:i+3], 'little', signed=True)
                                value = max(-bound,min(bound-1,round(sample*volume/100)))
                                out.extend(value.to_bytes(3,'little',signed=True))
                            converted = out
                        writer.writeframesraw(converted)
            self._gain_cache[token] = target
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def set_volume(self, kind: str, volume: int) -> None:
        """Change preview gain without modifying the scene or source audio."""
        if kind not in self.volumes:
            raise ValueError("音声種別が不正です。")
        volume = max(0, min(100, int(float(volume))))
        if self.volumes[kind] == volume:
            return
        self.volumes[kind] = volume
        if self.is_windows:
            changed_natively = False
            need_reopen = False
            for handle, voice in list(self.voices.items()):
                if voice.kind != kind or voice.original is None:
                    continue
                # MPEGVideo MCI permits live volume updates, unlike waveaudio.
                if voice.original.suffix.casefold() == '.mp3':
                    try:
                        self._mci(f"setaudio {handle} volume to {volume * 10}")
                        changed_natively = True
                        continue
                    except OSError:
                        pass
                need_reopen = True
            if kind == 'bgm' and changed_natively and not need_reopen:
                return  # MP3 BGM changed in place: don't interrupt playback.
        # For WAV and ffplay, recreate the BGM voice at its playback position.
        # Debounce while the slider is being dragged; short SEs use the new
        # gain the next time they are triggered.
        if kind == 'bgm' and self._current_bgm is not None and (
                any(v.kind == 'bgm' for v in self.voices.values()) or self._workers):
            if self._volume_restart_job is not None:
                self.parent.after_cancel(self._volume_restart_job)
            self._volume_restart_job = self.parent.after(300, self._restart_bgm_volume)

    def _restart_bgm_volume(self) -> None:
        self._volume_restart_job = None
        if self._closed or self._current_bgm is None:
            return
        name, source = self._current_bgm
        resume_ms = 0
        for handle, voice in self.voices.items():
            if voice.kind != 'bgm':
                continue
            resume_ms = max(0,int((time.monotonic()-voice.started)*1000))
            if self.is_windows:
                try:
                    resume_ms = max(0,int(self._mci(f"status {handle} position")))
                except (OSError, ValueError):
                    pass
            break
        self.play('bgm', name, source, resume_ms=resume_ms)

    def play(self, kind: str, key: str, source: Path, *, resume_ms: int = 0) -> None:
        if kind not in ("se", "bgm"):
            raise ValueError("音声種別が不正です。")
        source = source.expanduser().resolve()
        if not source.is_file():
            messagebox.showerror(APP_NAME, f"音声ファイルが見つかりません:\n{source}", parent=self.parent)
            return
        if source.suffix.casefold() not in AUDIO_EXTENSIONS:
            messagebox.showerror(APP_NAME, f"未対応の音声形式です: {source.suffix}", parent=self.parent)
            return
        self.generation[kind] += 1
        gen = self.generation[kind]
        self._pending_play[kind] = (gen, key, source, max(0,int(resume_ms)))
        if kind == "bgm":
            self._current_bgm = (key, source)
            self._stop_voices("bgm")
        suffix = source.suffix.casefold()
        needs_vgmstream = suffix == ".hca"
        needs_ffmpeg = self.is_windows and suffix == ".ogg"
        needs_wav_gain = self.is_windows and suffix == '.wav' and self.volumes[kind] != 100
        if not self.is_windows and not self._ffplay:
            messagebox.showerror(APP_NAME, "音声プレビューにはffplay (FFmpeg) が必要です。", parent=self.parent)
            return
        if needs_vgmstream or needs_ffmpeg or needs_wav_gain:
            if needs_vgmstream:
                decoder = self._vgmstream_path()
                if decoder is None:
                    expected = Path(__file__).resolve().parent / VGMSTREAM_RELATIVE_EXE
                    messagebox.showerror(
                        APP_NAME + " - HCA再生",
                        "HCAの再生にはvgmstream-cli.exeが必要です。\n\n"
                        f"次の場所に配置してください:\n{expected}\n\n"
                        "vgmstream-win64フォルダーには配布物のDLL等もそのまま置いてください。\n"
                        "JSONへの音声登録・保存はこのまま使用できます。",
                        parent=self.parent,
                    )
                    return
                executable = str(decoder)
                label = "vgmstream"
            elif needs_ffmpeg:
                executable = self._ffmpeg_path(request_install=True)
                if not executable:
                    return
                label = "FFmpeg"
            else:
                executable = None
                label = 'WAV音量調整'
            self.status.set(f"音声処理中 ({label}): {kind}.{key}")
            self._workers += 1
            level = self.volumes[kind]
            def worker() -> None:
                converted: Optional[Path] = None
                error: Optional[str] = None
                try:
                    decoded = (self._decode(source, executable, use_vgmstream=needs_vgmstream)
                               if executable is not None else source)
                    converted = self._gain_wav(decoded, level) if self.is_windows else decoded
                except (OSError, ValueError, EOFError, wave.Error, subprocess.TimeoutExpired) as exc:
                    error = str(exc)
                finally:
                    self.queue.put((kind, gen, converted, kind == "bgm", error))
            threading.Thread(target=worker, daemon=True, name="SceneAudioGainDecode").start()
            self._ensure_poll()
            return
        try:
            self._start_voice(kind, source, loop=kind == "bgm", key=key,
                              original=source, resume_ms=resume_ms)
            self.status.set(f"音声再生: {kind}.{key}")
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"音声を再生できません:\n{exc}", parent=self.parent)
        self._ensure_poll()

    def _start_voice(self, kind: str, source: Path, *, loop: bool, key: str = '',
                     original: Optional[Path] = None, resume_ms: int = 0) -> None:
        if kind == "se":
            voices = sorted(((key, voice) for key, voice in self.voices.items()
                             if voice.kind == "se"), key=lambda pair: pair[1].started)
            if len(voices) >= AUDIO_SE_VOICE_LIMIT:
                self._close_voice(voices[0][0])
        handle = "sce_" + uuid.uuid4().hex[:16]
        original = original or source
        if self.is_windows:
            filename = str(source)
            # Decode and gain WAV in a separate file; do not use waveOutSetVolume,
            # which would change the entire device rather than only BGM or SE.
            try:
                self._mci(f'open "{filename}" alias {handle}')
            except OSError:
                if source.suffix.casefold() != ".mp3":
                    raise
                self._mci(f'open "{filename}" type mpegvideo alias {handle}')
            try:
                if source.suffix.casefold() == '.mp3':
                    # MPEGVideo supports 0..1000 per-voice volume.
                    self._mci(f"setaudio {handle} volume to {self.volumes[kind]*10}")
                if resume_ms > 0:
                    try:
                        self._mci(f"seek {handle} to {resume_ms}")
                    except OSError:
                        pass
                try:
                    self._mci(f"play {handle}" + (" repeat" if loop else ""))
                except OSError:
                    if not loop:
                        raise
                    self._mci(f"play {handle}")
            except OSError:
                self._mci(f"close {handle}")
                raise
            voice = _AudioVoice(kind, handle, looping=loop, original=original,key=key)
        else:
            assert self._ffplay is not None
            command = [self._ffplay, "-nostdin", "-nodisp", "-autoexit", "-loglevel", "error",
                       "-volume", str(self.volumes[kind])]
            if loop:
                command += ["-loop", "0"]
            if resume_ms > 0:
                command += ["-ss", f"{resume_ms/1000:.3f}"]
            command.append(str(source))
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=flags)
            voice = _AudioVoice(kind, handle, process=process, looping=loop,
                                original=original,key=key)
        self.voices[handle] = voice

    def _close_voice(self, handle: str) -> None:
        voice = self.voices.pop(handle, None)
        if voice is None:
            return
        if self.is_windows:
            try:
                self._mci(f"stop {handle}")
            except OSError:
                pass
            try:
                self._mci(f"close {handle}")
            except OSError:
                pass
        elif voice.process is not None and voice.process.poll() is None:
            voice.process.terminate()
            try:
                voice.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                voice.process.kill()

    def _stop_voices(self, kind: Optional[str]) -> None:
        for handle, voice in list(self.voices.items()):
            if kind is None or voice.kind == kind:
                self._close_voice(handle)

    def stop(self, kind: Optional[str] = None) -> None:
        if kind is not None and kind not in self.generation:
            raise ValueError("音声種別が不正です。")
        for current in ([kind] if kind else ["bgm", "se"]):
            self.generation[current] += 1  # Cancel pending decoded play jobs.
        self._stop_voices(kind)
        if kind is None or kind == 'bgm':
            self._current_bgm = None
            if self._volume_restart_job is not None:
                self.parent.after_cancel(self._volume_restart_job)
                self._volume_restart_job = None
        if not self._closed:
            self.status.set((kind.upper() if kind else "全音声") + " を停止しました")

    def _ensure_poll(self) -> None:
        if not self._closed and self._poll_job is None:
            self._poll_job = self.parent.after(AUDIO_POLL_MS, self._poll)

    def _poll(self) -> None:
        self._poll_job = None
        if self._closed:
            return
        while True:
            try:
                kind, gen, path, looping, error = self.queue.get_nowait()
            except queue.Empty:
                break
            self._workers = max(0, self._workers - 1)
            if gen != self.generation[kind]:
                continue  # An older decoded clip can never start after stop/new play.
            if error:
                messagebox.showerror(APP_NAME, "音声を変換できません:\n" + error, parent=self.parent)
            elif path is not None:
                try:
                    pending = self._pending_play.get(kind)
                    if pending is None or pending[0] != gen:
                        continue
                    _, key, original, resume_ms = pending
                    self._start_voice(kind, path, loop=looping, key=key,
                                      original=original, resume_ms=resume_ms)
                    self.status.set(f"再生中: {kind}")
                except (OSError, ValueError) as exc:
                    messagebox.showerror(APP_NAME, f"音声を再生できません:\n{exc}", parent=self.parent)
        for handle, voice in list(self.voices.items()):
            if not self.is_windows:
                if voice.process is not None and voice.process.poll() is not None:
                    self._close_voice(handle)
                continue
            try:
                mode = self._mci(f"status {handle} mode").lower()
                if mode == "stopped" and voice.looping:
                    self._mci(f"play {handle}")
                elif mode in ("stopped", "not ready") and not voice.looping:
                    self._close_voice(handle)
            except OSError:
                self._close_voice(handle)
        if self.voices or self._workers:
            self._ensure_poll()

    def shutdown(self) -> None:
        self._closed = True
        if self._volume_restart_job is not None:
            self.parent.after_cancel(self._volume_restart_job)
            self._volume_restart_job = None
        if self._poll_job is not None:
            self.parent.after_cancel(self._poll_job)
            self._poll_job = None
        self._stop_voices(None)
        # Cleanup only after in-flight workers complete; they are daemon threads.
        # Workers own the temp file paths and may finish during app teardown.
        if self._workers == 0:
            self.tmpdir.cleanup()


class ToggleEventWindow(tk.Toplevel):
    """Nonmodal event list: place nested assets with the normal editor tools."""
    ASSET_OPTIONS = (
        ("ウィンドウの追加（9スライス / PNG / JPG）", "window"),
        ("画像を配置（PNG / JPG / plist）", "image"),
        ("画像をUVで配置（PNG / JPG）", "image_uv"),
        ("ボタンをUVで配置（PNG / JPG）", "button_uv"),
        ("トグルボタンを配置（PNG / JPG / plist）", "toggle_uv"),
        ("ボタンを配置（plist）", "button"),
        ("アニメーションを配置（ExportJson）", "animation"),
    )

    def __init__(self, editor: "SceneEditor", owner_uid: str) -> None:
        super().__init__(editor)
        self.editor = editor
        self.owner_uid = owner_uid
        self.title("トグルボタン - ON状態のイベント")
        self.transient(editor)
        self.protocol("WM_DELETE_WINDOW", self._close)
        # Own the list widgets under their actual geometry parent.  The former
        # implementation created Treeview under `outer` and packed it with
        # `in_=tree_frame`, which could render inconsistently across Tk builds.
        # A labeled area and an explicit empty-state indicator make the event
        # list visible even before the first event is registered.
        self.minsize(640, 370)
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        owner = editor._asset_by_uid(owner_uid)
        ttk.Label(outer, text=f"{owner.name if owner else ''} が ON の間に適用するイベント",
                  font=("", 11, "bold")).pack(anchor="w")
        ttk.Label(outer, text="一覧内のイベントをドラッグすると実行順序を変更できます。追加したアセットの位置は編集画面で調整できます。",
                  foreground="#626a76", wraplength=710).pack(anchor="w", pady=(5, 9))

        self.event_list_frame = ttk.LabelFrame(outer, text="イベント一覧（0件）", padding=(8, 8))
        self.event_list_frame.pack(fill="both", expand=True)
        self.event_list_frame.columnconfigure(0, weight=1)
        self.event_list_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(self.event_list_frame, columns=("action", "target"),
                                 show="headings", height=11, selectmode="browse")
        self.tree.heading("action", text="イベント種類")
        self.tree.heading("target", text="対象アセット")
        self.tree.column("action", width=160, minwidth=140, stretch=False)
        self.tree.column("target", width=440, minwidth=210, stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(self.event_list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        # Display a prominent message *inside* the empty list, not in a dialog.
        self.empty_event_hint = tk.Label(
            self.event_list_frame,
            text="イベントはまだありません。\n下の「イベントの追加」から登録してください。",
            bg="#ffffff", fg="#566170", justify="center", padx=15, pady=12,
        )
        self._reorder_source: Optional[str] = None
        self._reorder_start_y = 0
        self._reorder_changed = False
        self.tree.bind("<ButtonPress-1>", self._reorder_press, add="+")
        self.tree.bind("<B1-Motion>", self._reorder_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._reorder_release, add="+")
        self.tree.bind("<Double-1>", lambda _e: self._edit_asset())

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(10, 0))
        add = ttk.Button(controls, text="イベントの追加 ▾", command=self._show_add_menu)
        add.pack(side="left")
        self.add_button = add
        ttk.Button(controls, text="選択イベントを削除", command=self._remove).pack(side="left", padx=6)
        self.restore_asset_button = ttk.Button(
            controls, text="アセットに戻す", command=self._restore_asset, state="disabled")
        self.restore_asset_button.pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="追加アセットを編集画面で選択", command=self._edit_asset).pack(side="left")
        self.tree.bind("<<TreeviewSelect>>", self._update_restore_button, add="+")
        ttk.Button(controls, text="閉じる", command=self._close).pack(side="right")
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        requested_width = min(750, max(640, screen_width - 50))
        requested_height = min(500, max(370, screen_height - 90))
        self.editor._place_child_window(self, editor, (requested_width, requested_height))
        self.refresh_list()

    def _close(self) -> None:
        self.editor._event_windows.pop(self.owner_uid, None)
        self.destroy()

    def refresh_list(self) -> None:
        selected = self.tree.selection()
        old_row = selected[0] if selected else ""
        self.tree.delete(*self.tree.get_children())
        owner = self.editor._asset_by_uid(self.owner_uid)
        if owner is None:
            self._close()
            return
        count = len(owner.events)
        self.event_list_frame.configure(text=f"イベント一覧（{count}件）")
        if count:
            self.empty_event_hint.place_forget()
        else:
            self.empty_event_hint.place(in_=self.tree, relx=0.5, rely=0.52, anchor="center")
            self.empty_event_hint.lift()
        for idx, entry in enumerate(owner.events):
            if entry["type"] == "show":
                asset = self.editor._asset_by_uid(entry["asset_uid"])
                detail = f"{asset.name}（{asset.kind}）" if asset else "（削除済み）"
                title = "アセットの追加表示"
            elif entry["type"] == "show_scroll_group":
                name = entry["group_name"]
                group = self.editor.scroll_groups.get(name)
                detail = f"{name}（所属 {len(group.members)} 件）" if group else f"{name}（削除済み）"
                title = "スクロールグループの表示"
            else:
                asset = self.editor._asset_by_uid(entry["target_uid"])
                detail = f"{asset.name}（{asset.full_key}）" if asset else "（削除済み）"
                title = "アセットの非表示"
            self.tree.insert("", "end", iid=str(idx), values=(title, detail))
        if old_row and self.tree.exists(old_row):
            self.tree.selection_set(old_row)
        self._update_restore_button()

    def _update_restore_button(self, _event: Any = None) -> None:
        """Only show events contain an asset that can return to scene roots."""
        owner = self.editor._asset_by_uid(self.owner_uid)
        index = self._selected_index()
        eligible = (owner is not None and index is not None and
                    0 <= index < len(owner.events) and
                    owner.events[index]["type"] == "show" and
                    self.editor._asset_by_uid(owner.events[index]["asset_uid"]) is not None)
        self.restore_asset_button.configure(state="normal" if eligible else "disabled")

    def _selected_index(self) -> Optional[int]:
        selected = self.tree.selection()
        return int(selected[0]) if selected else None

    def _show_add_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False)
        show_menu = tk.Menu(menu, tearoff=False)
        show_menu.add_command(label="既存アセットの追加（複数選択可）",
                              command=self._add_existing_show)
        show_menu.add_separator()
        for label, kind in self.ASSET_OPTIONS:
            show_menu.add_command(label=label,
                                  command=lambda k=kind: self._add_show(k))
        menu.add_cascade(label="アセットの追加表示", menu=show_menu)
        menu.add_command(label="スクロールグループの表示", command=self._add_scroll_group)
        menu.add_command(label="アセットの非表示", command=self._add_hide)
        x = self.add_button.winfo_rootx()
        y = self.add_button.winfo_rooty() + self.add_button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _add_show(self, kind: str) -> None:
        if self.editor._asset_by_uid(self.owner_uid) is None:
            return
        self.editor._pending_spawn = (0.0, 0.0)
        asset = self.editor.add_asset(kind, event_owner_uid=self.owner_uid)
        if asset is not None:
            self.refresh_list()
            self.editor.status_text.set(f"ON時の追加表示: {asset.name}。編集画面で配置を調整できます。")

    def _add_scroll_group(self) -> None:
        """Add references to existing scroll viewports without moving/duplicating members."""
        owner = self.editor._asset_by_uid(self.owner_uid)
        if owner is None:
            return
        registered = {event["group_name"] for event in owner.events
                      if event["type"] == "show_scroll_group"}
        candidates = [g for g in self.editor.scroll_groups.values()
                      if g.name not in registered and owner.uid not in g.members]
        if not candidates:
            messagebox.showinfo(APP_NAME,
                "追加可能なスクロールグループがありません。\n先にスクロールグループを作成してください。",
                parent=self)
            return
        popup = tk.Toplevel(self)
        popup.title("ON時に表示するスクロールグループ")
        popup.transient(self)
        outer = ttk.Frame(popup, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="トグルONの間だけ表示するグループを選択してください。\n"
                  "所属アセットは移動・複製されません。Ctrl / Shiftで複数選択できます。",
                  wraplength=460).pack(anchor="w")
        listing = tk.Listbox(outer, selectmode=tk.EXTENDED, exportselection=False,
                             height=min(12, max(5, len(candidates))), width=60)
        listing.pack(fill="both", expand=True, pady=(9, 6))
        for group in candidates:
            listing.insert(tk.END, f"{group.name}   / 所属 {len(group.members)} 件"
                           f"  X:{'ON' if group.scroll_x else 'OFF'}"
                           f"  Y:{'ON' if group.scroll_y else 'OFF'}")
        def confirm(_event: Any = None) -> None:
            indices = listing.curselection()
            if not indices:
                messagebox.showinfo(APP_NAME, "グループを選択してください。", parent=popup)
                return
            try:
                self.editor.add_scroll_group_show_events(
                    self.owner_uid, [candidates[i].name for i in indices])
            except ValueError as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=popup)
                return
            popup.destroy()
            self.refresh_list()
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(5, 0))
        ttk.Button(buttons, text="追加", command=confirm).pack(side="right", padx=(7, 0))
        ttk.Button(buttons, text="キャンセル", command=popup.destroy).pack(side="right")
        listing.bind("<Double-1>", confirm)
        self.editor._place_child_window(popup, self, (550, 350))
        popup.grab_set()
        listing.focus_set()

    def _add_existing_show(self) -> None:
        """Transfer selected root assets into this toggle's show events."""
        candidates = self.editor.eligible_existing_event_assets(self.owner_uid)
        if not candidates:
            messagebox.showinfo(APP_NAME,
                "追加できる通常配置アセットがありません。\n"
                "自身・既存イベント内アセット・登録済みイベント対象は候補から除外しています。",
                parent=self)
            return
        popup = tk.Toplevel(self)
        popup.title("既存アセットの追加（複数選択）")
        popup.transient(self)
        popup.minsize(420, 350)
        outer = ttk.Frame(popup, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="ON時に追加表示する既存アセットを選択してください。\n"
                  "元の通常配置からイベント内へ移動します（コピーではありません）。",
                  wraplength=520).pack(anchor="w")
        search_var = tk.StringVar()
        ttk.Entry(outer, textvariable=search_var).pack(fill="x", pady=(10, 5))
        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True)
        listing = tk.Listbox(list_frame, exportselection=False, selectmode=tk.EXTENDED, height=12)
        listing.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listing.yview)
        listing.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        mapping: list[str] = []
        selected_ids: set[str] = set()
        refreshing = False
        count_text = tk.StringVar(value="選択: 0 件（Ctrl / Shift で複数選択）")
        ttk.Label(outer, textvariable=count_text).pack(anchor="w", pady=(5, 0))

        def capture_selection(_event: Any = None) -> None:
            if refreshing:
                return
            selected_ids.difference_update(mapping)
            selected_ids.update(mapping[index] for index in listing.curselection())
            count_text.set(f"選択: {len(selected_ids)} 件（Ctrl / Shift で複数選択）")

        def update(*_args: Any) -> None:
            nonlocal refreshing
            refreshing = True
            try:
                listing.delete(0, tk.END)
                mapping.clear()
                query = search_var.get().strip().casefold()
                for asset in candidates:
                    kind_label = "ウィンドウ" if asset.kind == "window" else "アセット"
                    label = f"{asset.name}　({kind_label}: {asset.full_key})"
                    if query in label.casefold():
                        mapping.append(asset.uid)
                        listing.insert(tk.END, label)
                        if asset.uid in selected_ids:
                            listing.selection_set(len(mapping) - 1)
            finally:
                refreshing = False
        search_var.trace_add("write", update)
        listing.bind("<<ListboxSelect>>", capture_selection)
        update()

        def select_visible() -> None:
            listing.selection_set(0, tk.END)
            capture_selection()

        def confirm() -> None:
            capture_selection()
            if not selected_ids:
                messagebox.showinfo(APP_NAME, "追加するアセットを選択してください。", parent=popup)
                return
            ordered_ids = [asset.uid for asset in candidates if asset.uid in selected_ids]
            conflicts = self.editor.invalidated_hide_references(ordered_ids)
            if conflicts:
                count = sum(conflicts.values())
                if not messagebox.askyesno(APP_NAME,
                        f"移動対象を非表示にするイベントが他のトグルに {count} 件あります。\n"
                        "移動後は通常配置アセットではなくなるため、これらの非表示イベントは削除されます。\n"
                        "移動を続行しますか？", parent=popup):
                    return
            try:
                moved = self.editor.move_assets_to_toggle_event(self.owner_uid, ordered_ids)
            except ValueError as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=popup)
                return
            popup.destroy()
            self.refresh_list()
            self.editor.status_text.set(f"既存アセット {len(moved)} 件をON時の追加表示へ移動しました。")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="表示中をすべて選択", command=select_visible).pack(side="left")
        ttk.Button(buttons, text="追加して移動", command=confirm).pack(side="right", padx=(7, 0))
        ttk.Button(buttons, text="キャンセル", command=popup.destroy).pack(side="right")
        self.editor._place_child_window(popup, self, (590, 470))
        popup.grab_set()
        popup.focus_set()

    def _reorder_press(self, event: tk.Event) -> None:
        self._reorder_source = None
        self._reorder_changed = False
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        if row:
            self._reorder_source = row
            self._reorder_start_y = event.y

    def _reorder_motion(self, event: tk.Event) -> None:
        source = self._reorder_source
        if source is None or not self.tree.exists(source):
            return
        if not self._reorder_changed and abs(event.y - self._reorder_start_y) < 5:
            return
        rows = self.tree.get_children()
        if len(rows) < 2:
            return
        if event.y < 14:
            self.tree.yview_scroll(-1, "units")
        elif event.y > self.tree.winfo_height() - 14:
            self.tree.yview_scroll(1, "units")
        target = self.tree.identify_row(event.y)
        if not target:
            target = rows[0] if event.y < 0 else rows[-1]
        if target == source:
            return
        before = self.tree.index(source)
        desired = self.tree.index(target)
        self.tree.move(source, "", desired)
        if self.tree.index(source) != before:
            self._reorder_changed = True
            self.tree.selection_set(source)
            self.tree.focus(source)

    def _reorder_release(self, _event: tk.Event) -> None:
        source = self._reorder_source
        changed = self._reorder_changed
        self._reorder_source = None
        self._reorder_changed = False
        if source is None or not changed:
            return
        order = [int(row) for row in self.tree.get_children()]
        try:
            self.editor.reorder_toggle_events(self.owner_uid, order)
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            self.refresh_list()
            return
        self.refresh_list()
        # After refreshing, select the moved event at its new position.
        if int(source) in order:
            new_row = str(order.index(int(source)))
            if self.tree.exists(new_row):
                self.tree.selection_set(new_row)
                self.tree.focus(new_row)
                self.tree.see(new_row)

    def _add_hide(self) -> None:
        owner = self.editor._asset_by_uid(self.owner_uid)
        if owner is None:
            return
        candidates = [a for a in self.editor.assets
                      if a.uid != owner.uid and a.event_owner_uid is None and
                      not any(e["type"] == "hide" and e["target_uid"] == a.uid
                              for e in owner.events)]
        if not candidates:
            messagebox.showinfo(APP_NAME, "非表示にできる他の通常配置アセットがありません。", parent=self)
            return
        popup = tk.Toplevel(self)
        popup.title("非表示にする既存アセットを選択")
        popup.transient(self)
        outer = ttk.Frame(popup, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="トグルがONの間、非表示にするアセットを選択してください。", wraplength=510).pack(anchor="w")
        search_var = tk.StringVar()
        ttk.Entry(outer, textvariable=search_var).pack(fill="x", pady=(9, 5))
        listing = tk.Listbox(outer, exportselection=False, height=12)
        listing.pack(fill="both", expand=True)
        mapping: list[str] = []
        def update(*_args: Any) -> None:
            listing.delete(0, tk.END)
            mapping.clear()
            query = search_var.get().casefold()
            for asset in candidates:
                label = f"{asset.name}　({asset.full_key})"
                if query in label.casefold():
                    mapping.append(asset.uid)
                    listing.insert(tk.END, label)
            if mapping:
                listing.selection_set(0)
        search_var.trace_add("write", update)
        update()
        def confirm(_event: Any = None) -> None:
            chosen = listing.curselection()
            if not chosen:
                return
            try:
                self.editor.add_hide_event(self.owner_uid, mapping[chosen[0]])
            except ValueError as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=popup)
                return
            popup.destroy()
            self.refresh_list()
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="追加", command=confirm).pack(side="left")
        ttk.Button(buttons, text="キャンセル", command=popup.destroy).pack(side="right")
        listing.bind("<Double-1>", confirm)
        self.editor._place_child_window(popup, self, (565, 390))
        popup.grab_set()
        popup.focus_set()

    def _remove(self) -> None:
        index = self._selected_index()
        if index is not None:
            self.editor.remove_toggle_event(self.owner_uid, index)
            self.refresh_list()

    def _restore_asset(self) -> None:
        """Return a show-event child to normal scene placement without copying it."""
        index = self._selected_index()
        if index is None:
            return
        try:
            restored = self.editor.restore_toggle_event_asset(self.owner_uid, index)
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        self.refresh_list()
        self.editor.select(restored.uid)
        self.editor.status_text.set(
            f"{restored.name} を通常アセットに戻しました。Ctrl+Zで元に戻せます。")

    def _edit_asset(self) -> None:
        index = self._selected_index()
        owner = self.editor._asset_by_uid(self.owner_uid)
        if owner is None or index is None or index >= len(owner.events):
            return
        event = owner.events[index]
        if event["type"] == "show_scroll_group":
            name = event["group_name"]
            if name in self.editor.scroll_groups:
                self.editor.open_scroll_workspace(name)
            return
        uid = event.get("asset_uid") if event["type"] == "show" else event.get("target_uid")
        if self.editor._asset_by_uid(uid) is not None:
            self.editor.select(uid)
            self.editor.lift()
            self.editor.edit_canvas.focus_set()


class ButtonScrollEventWindow(tk.Toplevel):
    """Configure persistent scroll-group commands on a regular button press.

    Unlike toggle ON-gating, a normal button applies a command once per click.
    The last click wins until the preview state is reset.
    """
    ACTIONS = (("show_scroll_group", "スクロールグループを表示"),
               ("hide_scroll_group", "スクロールグループを非表示"),
               ("toggle_scroll_group", "表示／非表示を切替"))

    def __init__(self, editor: "SceneEditor", owner_uid: str) -> None:
        super().__init__(editor)
        self.editor = editor
        self.owner_uid = owner_uid
        self.title("通常ボタン - 押下時のスクロールイベント")
        self.transient(editor)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.minsize(480, 310)
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        owner = editor._asset_by_uid(owner_uid)
        ttk.Label(frame, text=f"{owner.name if owner else ''}：押下時のイベント",
                  font=("", 11, "bold")).pack(anchor="w")
        ttk.Label(frame, text="表示・非表示はプレビューで押した時に反映されます。"
                  " 別のボタンによる操作やプレビューの再読み込みまで保持します。",
                  wraplength=540).pack(anchor="w", pady=(5, 9))
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("action", "target"),
                                 show="headings", selectmode="browse", height=9)
        self.tree.heading("action", text="実行内容")
        self.tree.heading("target", text="対象グループ")
        self.tree.column("action", width=195, minwidth=160, stretch=False)
        self.tree.column("target", width=250, stretch=True)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(9, 0))
        ttk.Button(controls, text="イベントを追加…", command=self._add).pack(side="left")
        ttk.Button(controls, text="選択イベントを削除", command=self._remove).pack(side="left", padx=6)
        ttk.Button(controls, text="グループ配置画面", command=self._open_group).pack(side="left")
        ttk.Button(controls, text="閉じる", command=self._close).pack(side="right")
        self.tree.bind("<Double-1>", lambda _e: self._open_group())
        editor._place_child_window(self, editor, (625, 390))
        self.refresh_list()

    def _close(self) -> None:
        self.editor._event_windows.pop(self.owner_uid, None)
        self.destroy()

    def refresh_list(self) -> None:
        old = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        owner = self.editor._asset_by_uid(self.owner_uid)
        if owner is None or owner.kind not in ("button", "button_uv"):
            self._close()
            return
        labels = dict(self.ACTIONS)
        for i, event in enumerate(owner.events):
            kind = event.get("type")
            if kind in labels:
                name = event["group_name"]
                missing = "（削除済み）" if name not in self.editor.scroll_groups else ""
                self.tree.insert("", "end", iid=str(i), values=(labels[kind], name+missing))
        if old and self.tree.exists(old[0]):
            self.tree.selection_set(old[0])

    def _add(self) -> None:
        owner = self.editor._asset_by_uid(self.owner_uid)
        if owner is None:
            return
        candidates = [group for group in self.editor.scroll_groups.values()
                      if not any(e.get("group_name") == group.name for e in owner.events)]
        if not candidates:
            messagebox.showinfo(APP_NAME, "追加できるスクロールグループがありません。", parent=self)
            return
        popup = tk.Toplevel(self)
        popup.title("スクロールグループの操作を追加")
        popup.transient(self)
        frame = ttk.Frame(popup, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="押下時の操作を選択してください。", wraplength=480).pack(anchor="w")
        action_var = tk.StringVar(value="show_scroll_group")
        for value, label in self.ACTIONS:
            ttk.Radiobutton(frame, text=label, variable=action_var,
                            value=value).pack(anchor="w", pady=2)
        ttk.Label(frame, text="対象グループ（Ctrl / Shiftで複数選択）").pack(anchor="w", pady=(8, 3))
        listing = tk.Listbox(frame, selectmode=tk.EXTENDED, exportselection=False, height=9)
        listing.pack(fill="both", expand=True)
        for group in candidates:
            listing.insert(tk.END, f"{group.name}  / 所属 {len(group.members)} 件")
        def confirm(_event: Any = None) -> None:
            chosen = [candidates[i].name for i in listing.curselection()]
            if not chosen:
                messagebox.showinfo(APP_NAME, "グループを選択してください。", parent=popup)
                return
            try:
                self.editor.add_button_scroll_events(self.owner_uid, action_var.get(), chosen)
            except ValueError as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=popup)
                return
            popup.destroy()
            self.refresh_list()
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="追加", command=confirm).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="キャンセル", command=popup.destroy).pack(side="right")
        popup.bind("<Return>", confirm)
        self.editor._place_child_window(popup, self, (530, 450))
        popup.grab_set()
        listing.focus_set()

    def _remove(self) -> None:
        owner = self.editor._asset_by_uid(self.owner_uid)
        rows = self.tree.selection()
        if owner is None or not rows:
            return
        self.editor.remove_button_scroll_event(self.owner_uid, int(rows[0]))
        self.refresh_list()

    def _open_group(self) -> None:
        owner = self.editor._asset_by_uid(self.owner_uid)
        rows = self.tree.selection()
        if owner is None or not rows:
            return
        name = owner.events[int(rows[0])].get("group_name")
        if name in self.editor.scroll_groups:
            self.editor.open_scroll_workspace(name)


class ExclusiveToggleGroupDialog(tk.Toplevel):
    """Edit a mutually-exclusive family of independently editable toggle assets.

    Children have their own textures, normalized UVs, events, and transforms. Only
    the group identity and initial selection are shared. No scene records are
    mutated until the final confirmation button is pressed.
    """
    MAX_MEMBERS = 64

    def __init__(self, editor: "SceneEditor", group: Optional[str] = None) -> None:
        super().__init__(editor)
        self.editor = editor
        self.group = group
        self.result: Optional[tuple[str, list[Asset], set[str], int, dict[str, str]]] = None
        self.members = [editor._asset_state_copy(a) for a in editor.assets
                        if a.toggle_group == group and group is not None]
        self.original_uids = {a.uid for a in self.members}
        # Remember the source of each newly duplicated switch. Event children are
        # given new UIDs only on confirmation, keeping Cancel non-destructive.
        self.duplicate_sources: dict[str, str] = {}
        self.name_var = tk.StringVar(value=group or "toggle_group")
        self.initial_var = tk.StringVar(value=next((a.uid for a in self.members
                                        if a.toggle_default == "on"),
                                        self.members[0].uid if self.members else ""))
        self.title("複数トグルスイッチ - グループ設定")
        self.transient(editor)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.minsize(570, 390)
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="複数トグルスイッチ（ONは常に1個）",
                  font=("", 11, "bold")).pack(anchor="w")
        ttk.Label(outer, text="各行は個別のトグルボタンです。画像・UV・SEを個別設定できます。\n"
                  "確定後は編集画面で各スイッチの位置やサイズを調整できます。",
                  justify="left").pack(anchor="w", pady=(5, 10))
        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="グループ名:").pack(side="left")
        ttk.Entry(row, textvariable=self.name_var, width=28).pack(side="left", padx=8)
        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, columns=("default", "off", "on", "se"),
                                 show="tree headings", selectmode="browse", height=9)
        self.tree.heading("#0", text="スイッチ名")
        self.tree.heading("default", text="初期状態")
        self.tree.heading("off", text="OFF画像")
        self.tree.heading("on", text="ON画像")
        self.tree.heading("se", text="SE")
        self.tree.column("#0", width=140, minwidth=95)
        self.tree.column("default", width=75, minwidth=72, anchor="center", stretch=False)
        self.tree.column("off", width=120)
        self.tree.column("on", width=120)
        self.tree.column("se", width=95)
        self.tree.pack(side="left", fill="both", expand=True)
        self.table_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.table_scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=self.table_scrollbar.set)
        self.tree.bind("<Double-1>", lambda _event: self.edit_member())
        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(10, 0))
        first_controls = ttk.Frame(controls)
        first_controls.pack(fill="x", pady=(0, 5))
        ttk.Button(first_controls, text="スイッチを追加…", command=self.add_member).pack(side="left", padx=(0, 5))
        ttk.Button(first_controls, text="既存のトグルボタン追加", command=self.add_existing_member).pack(side="left", padx=5)
        ttk.Button(first_controls, text="選択スイッチを複製 (Ctrl+D)", command=self.duplicate_member).pack(side="left", padx=5)
        second_controls = ttk.Frame(controls)
        second_controls.pack(fill="x")
        ttk.Button(second_controls, text="名前を変更…", command=self.rename_member).pack(side="left", padx=(0, 5))
        ttk.Button(second_controls, text="画像・UV・SEを編集", command=self.edit_member).pack(side="left", padx=5)
        ttk.Button(second_controls, text="初期ONに指定", command=self.choose_default).pack(side="left", padx=5)
        ttk.Button(second_controls, text="グループから外す", command=self.remove_member).pack(side="left", padx=5)
        self.bind("<Control-d>", self._duplicate_shortcut)
        self.bind("<Control-D>", self._duplicate_shortcut)
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, text="2個以上必要です。外した既存アセットは単独トグルになります。",
                  wraplength=380).pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="キャンセル", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(footer, text="確定して配置", command=self.confirm).pack(side="right")
        requested_w = min(950, max(570, self.winfo_screenwidth() - 70))
        requested_h = min(530, max(390, self.winfo_screenheight() - 100))
        editor._place_child_window(self, editor, (requested_w, requested_h))
        self.refresh_list()
        self.grab_set()
        self.wait_window()

    def refresh_list(self) -> None:
        old = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for member in self.members:
            self.tree.insert("", "end", iid=member.uid, text=member.name,
                values=("ON" if member.uid == self.initial_var.get() else "OFF",
                        member.source.name,
                        (member.pressed_source or member.source).name,
                        member.button_se or "なし"))
        if old and self.tree.exists(old[0]):
            self.tree.selection_set(old[0])

    def _current(self) -> Optional[Asset]:
        sel = self.tree.selection()
        return next((a for a in self.members if sel and a.uid == sel[0]), None)

    def _unique_member_name(self, original: str) -> str:
        existing = {a.name for a in self.editor.assets} | {a.name for a in self.members}
        root = re.sub(r"[^\w.\-]", "_", original, flags=re.UNICODE) or "switch"
        if root not in existing:
            return root
        counter = 2
        while f"{root}_{counter}" in existing:
            counter += 1
        return f"{root}_{counter}"

    def _duplicate_shortcut(self, _event: tk.Event) -> str:
        self.duplicate_member()
        return "break"

    def duplicate_member(self) -> None:
        """Copy a switch's art, UV, SE, flags and event definitions into a new row."""
        current = self._current()
        if current is None:
            messagebox.showinfo(APP_NAME, "複製するスイッチを一覧から選んでください。", parent=self)
            return
        if len(self.members) >= self.MAX_MEMBERS:
            messagebox.showwarning(APP_NAME, "スイッチは最大64個までです。", parent=self)
            return
        clone = self.editor._asset_state_copy(current)
        clone.uid = uuid.uuid4().hex
        base_name = current.name
        numbered = re.fullmatch(r"(.+)_([2-9][0-9]*)", base_name)
        if numbered and any(a.name == numbered.group(1) for a in self.members):
            base_name = numbered.group(1)
        clone.name = self._unique_member_name(base_name)
        clone.toggle_group = None
        clone.toggle_default = "off"
        clone.event_owner_uid = None
        clone.px += 32.0
        clone.py -= 32.0
        self.duplicate_sources[clone.uid] = current.uid
        # Insert next to the source so successive duplicates stay easy to find.
        self.members.insert(self.members.index(current) + 1, clone)
        self.refresh_list()
        self.tree.selection_set(clone.uid)
        self.tree.focus(clone.uid)
        self.tree.see(clone.uid)

    def rename_member(self) -> None:
        member = self._current()
        if member is None:
            messagebox.showinfo(APP_NAME, "名前を変更するスイッチを選んでください。", parent=self)
            return
        requested = simpledialog.askstring(APP_NAME, "スイッチ名:",
                                            initialvalue=member.name, parent=self)
        if requested is None:
            return
        try:
            name = safe_asset_key(requested)
            if any(a.uid != member.uid and a.name == name and a.category == member.category
                   for a in self.editor.assets if a.uid not in self.original_uids):
                raise ValueError("この名前は他のアセットで使用されています。")
            if any(a.uid != member.uid and a.name == name for a in self.members):
                raise ValueError("この名前は別のスイッチで使用されています。")
            member.name = name
            self.refresh_list()
            self.tree.selection_set(member.uid)
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)

    def choose_default(self) -> None:
        member = self._current()
        if member is not None:
            self.initial_var.set(member.uid)
            self.refresh_list()

    def _select_uv(self, asset: Asset) -> bool:
        dialog = ButtonUVDialog(self, asset.source,
            normal=asset.uv_rect, pressed=asset.pressed_uv_rect,
            se=asset.button_se, se_keys=self.editor.audio["se"],
            pressed_path=asset.pressed_source or asset.source,
            toggle_mode=True, default_state="off")
        if dialog.result is None:
            return False
        normal, pushed, se, pushed_path, _ = dialog.result
        asset.source = dialog.state_sources["normal"]
        asset.uv_rect = normal
        asset.pressed_source = pushed_path
        asset.pressed_uv_rect = pushed
        asset.button_se = se
        load_visual(asset)
        return True

    def add_member(self) -> None:
        if len(self.members) >= self.MAX_MEMBERS:
            messagebox.showwarning(APP_NAME, "スイッチは最大64個までです。", parent=self)
            return
        selected = filedialog.askopenfilename(parent=self,
            title="スイッチのPNG/JPGまたはplistを選択",
            initialdir=self.editor._dialog_initial_dir("toggle_uv"),
            filetypes=[("画像 / plist", "*.png *.jpg *.jpeg *.plist")])
        if not selected:
            return
        try:
            source = ensure_asset_path(Path(selected))
            name_hint = source.stem
            off_rect: Optional[tuple[int, int, int, int]] = None
            on_rect: Optional[tuple[int, int, int, int]] = None
            on_path: Optional[Path] = None
            se: Optional[str] = None
            if source.suffix.casefold() == ".plist":
                atlas = load_atlas(source)
                if not atlas.frames:
                    raise ValueError("plistにスプライトがありません。")
                first = atlas.frames[0].name
                picker = PlistButtonStateDialog(self, source, first, first,
                                                 None, self.editor.audio["se"], toggle_mode=True)
                if picker.result is None:
                    return
                off_key, on_key, se, _ = picker.result
                by_name = {frame.name: frame for frame in atlas.frames}
                off_image = extract_sprite(atlas.image, by_name[off_key])
                on_image = extract_sprite(atlas.image, by_name[on_key])
                source = generated_sprite_png(off_image)
                on_path = generated_sprite_png(on_image)
                off_rect = (0, 0, *off_image.size)
                on_rect = (0, 0, *on_image.size)
                name_hint = Path(off_key).stem
            else:
                with Image.open(source) as image:
                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        raise ValueError("画像サイズが上限を超えています。")
                    off_rect = (0, 0, image.width, image.height)
                    on_rect = off_rect
                candidate = Asset(name="switch", kind="toggle_uv", source=source,
                                  uv_rect=off_rect, pressed_uv_rect=on_rect,
                                  pressed_source=source)
                if not self._select_uv(candidate):
                    return
                source, off_rect, on_rect = candidate.source, candidate.uv_rect, candidate.pressed_uv_rect
                on_path, se = candidate.pressed_source, candidate.button_se
            name = self._unique_member_name(name_hint)
            asset = Asset(name=name, kind="toggle_uv", source=source,
                          uv_rect=off_rect, pressed_uv_rect=on_rect,
                          pressed_source=on_path, button_se=se, toggle_default="off")
            load_visual(asset)
            if asset.missing:
                raise ValueError(asset.missing)
            self.members.append(asset)
            if len(self.members) == 1:
                self.initial_var.set(asset.uid)
            self.editor._remember_dialog_file("toggle_uv", source)
            self.refresh_list()
            self.tree.selection_set(asset.uid)
            self.tree.see(asset.uid)
        except (OSError, ValueError, TypeError, MemoryError, UnidentifiedImageError) as exc:
            messagebox.showerror(APP_NAME, f"スイッチの追加に失敗しました:\n{exc}", parent=self)

    def add_existing_member(self) -> None:
        """Enroll existing scene toggles in the exclusive group, without duplication.

        Only ungrouped scene-root toggles and members removed from this same
        group in the pending dialog are eligible.  Existing image/UV/SE,
        position, events, flags, and UID are preserved.  The scene is untouched
        until the enclosing group's confirmation.
        """
        space = self.MAX_MEMBERS - len(self.members)
        if space <= 0:
            messagebox.showwarning(APP_NAME, "スイッチは最大64個までです。", parent=self)
            return
        used = {a.uid for a in self.members}
        eligible = [a for a in self.editor.assets
                    if a.kind == "toggle_uv" and a.event_owner_uid is None
                    and a.toggle_group in (None, self.group) and a.uid not in used]
        if not eligible:
            messagebox.showinfo(APP_NAME,
                "追加できる既存のトグルボタンがありません。\n"
                "ほかの排他グループ所属や、イベント内のボタンは対象外です。", parent=self)
            return
        picker = tk.Toplevel(self)
        picker.title("既存のトグルボタン追加")
        picker.transient(self)
        picker.minsize(430, 300)
        body = ttk.Frame(picker, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="グループに追加する配置済みトグルボタンを選択",
                  font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(body, text="Ctrl / Shiftで複数選択できます。位置・UV・SE・イベントは維持されます。\n"
                  "別グループに所属するスイッチは表示しません。",
                  justify="left").pack(anchor="w", pady=(5, 8))
        table_frame = ttk.Frame(body)
        table_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(table_frame, columns=("off", "on", "events"),
                            show="tree headings", selectmode="extended", height=9)
        tree.heading("#0", text="配置済みスイッチ名")
        tree.heading("off", text="OFF画像")
        tree.heading("on", text="ON画像")
        tree.heading("events", text="イベント")
        tree.column("#0", width=165, minwidth=115)
        tree.column("off", width=110, minwidth=80)
        tree.column("on", width=110, minwidth=80)
        tree.column("events", width=75, minwidth=64, anchor="center", stretch=False)
        tree.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        bar.pack(side="right", fill="y")
        tree.configure(yscrollcommand=bar.set)
        for asset in eligible:
            tree.insert("", "end", iid=asset.uid, text=asset.name,
                        values=(asset.source.name,
                                (asset.pressed_source or asset.source).name,
                                len(asset.events)))
        selected: list[str] = []
        def accept(_event: Optional[tk.Event] = None) -> None:
            chosen = set(tree.selection())
            if not chosen:
                messagebox.showinfo(APP_NAME, "追加するスイッチを選択してください。", parent=picker)
                return
            if len(chosen) > space:
                messagebox.showwarning(APP_NAME,
                    f"あと{space}個まで追加できます。選択数を減らしてください。", parent=picker)
                return
            selected.extend(a.uid for a in eligible if a.uid in chosen)
            picker.destroy()
        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text=f"追加可能: {space}個 / 複数選択可").pack(side="left")
        ttk.Button(footer, text="キャンセル", command=picker.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(footer, text="選択したスイッチを追加", command=accept).pack(side="right")
        tree.bind("<Double-1>", accept)
        picker.bind("<Escape>", lambda _event: picker.destroy())
        picker.bind("<Return>", accept)
        self.editor._place_child_window(picker, self, (650, 410))
        picker.grab_set()
        picker.wait_window()
        # Restore the parent dialog's modal grab after the nested selector closes.
        if self.winfo_exists():
            self.grab_set()
        if not selected:
            return
        previous_count = len(self.members)
        for uid in selected:
            existing = self.editor._asset_by_uid(uid)
            if existing is None:
                continue
            member = self.editor._asset_state_copy(existing)
            self.members.append(member)
            # A reused root asset already has an identity and event children;
            # it must not be counted as new or have descendants duplicated.
            self.original_uids.add(uid)
        if previous_count == 0 and self.members:
            self.initial_var.set(self.members[0].uid)
        self.refresh_list()
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
            self.tree.focus(selected[0])
            self.tree.see(selected[0])

    def edit_member(self) -> None:
        member = self._current()
        if member is None:
            return
        try:
            self._select_uv(member)
            self.refresh_list()
            self.tree.selection_set(member.uid)
        except (OSError, ValueError, MemoryError) as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)

    def remove_member(self) -> None:
        member = self._current()
        if member is None:
            return
        self.members = [a for a in self.members if a.uid != member.uid]
        self.duplicate_sources.pop(member.uid, None)
        if self.initial_var.get() == member.uid:
            self.initial_var.set(self.members[0].uid if self.members else "")
        self.refresh_list()

    def confirm(self) -> None:
        try:
            name = safe_asset_key(self.name_var.get())
            existing_groups = {a.toggle_group for a in self.editor.assets if a.toggle_group}
            if name != self.group and name in existing_groups:
                raise ValueError(f"グループ名 '{name}' は既に使用されています。")
            if len(self.members) < 2:
                raise ValueError("スイッチを2個以上登録してください。")
            # Deep-copying event-show children also consumes asset slots. Verify
            # the full tree before mutating the editor in the confirmation step.
            templates = {a.uid: a for a in self.editor.assets}
            def child_count(member: Asset, path: frozenset[str] = frozenset()) -> int:
                if member.uid in path or len(path) > 32:
                    raise ValueError("トグルイベントの参照に循環または深すぎる階層があります。")
                path = path | {member.uid}
                count = 0
                for event in member.events:
                    if event.get("type") != "show":
                        continue
                    child = templates.get(event.get("asset_uid"))
                    if child is not None:
                        count += 1 + child_count(child, path)
                return count
            new_members = [m for m in self.members if m.uid not in self.original_uids]
            needed = len(new_members) + sum(child_count(m) for m in new_members)
            if len(self.editor.assets) + needed > MAX_ASSETS:
                raise ValueError("イベント内アセットを含めるとシーンのアセット数上限を超えます。")
            initial_index = next((index for index, a in enumerate(self.members)
                                  if a.uid == self.initial_var.get()), -1)
            if initial_index < 0:
                raise ValueError("初期ONのスイッチを指定してください。")
            for m in self.members:
                if m.kind != "toggle_uv":
                    raise ValueError("グループにはトグルボタンのみ指定できます。")
                if m.event_owner_uid is not None:
                    raise ValueError("イベント内のトグルは排他グループに登録できません。")
            self.result = (name, self.members, self.original_uids, initial_index,
                           dict(self.duplicate_sources))
            self.destroy()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)


class ScrollGroupDialog(tk.Toplevel):
    """Create/edit/remove scroll viewports; include nested toggle-event assets."""
    def __init__(self, editor: "SceneEditor", initial_name: Optional[str] = None,
                 initial_bounds: Optional[tuple[float, float, float, float]] = None) -> None:
        super().__init__(editor)
        self.editor = editor
        self.initial_bounds = initial_bounds
        self.title("スクロールグループの設定")
        self.geometry("670x650")
        self.minsize(560, 510)
        self.transient(editor)
        self.groups = {k: copy.deepcopy(v) for k, v in editor.scroll_groups.items()}
        self.available_assets = list(editor.assets)
        self.active_name: Optional[str] = None
        self.name_var = tk.StringVar()
        self.entries = {k: tk.StringVar() for k in ("px", "py", "width", "height")}
        self.allow_x = tk.BooleanVar(value=False)
        self.allow_y = tk.BooleanVar(value=True)
        self._construct()
        self._refresh_groups()
        if initial_name in self.groups:
            self._load_group(initial_name)
        else:
            self._start_new()
        self.grab_set()
        self.focus_set()

    def _construct(self) -> None:
        header = ttk.Frame(self, padding=10)
        header.pack(fill="x")
        ttk.Label(header, text="登録済みグループ").pack(side="left")
        self.group_select = ttk.Combobox(header, state="readonly", width=27)
        self.group_select.pack(side="left", padx=7)
        self.group_select.bind("<<ComboboxSelected>>", lambda _: self._load_group(self.group_select.get()))
        ttk.Button(header, text="新規", command=self._start_new).pack(side="left", padx=3)
        ttk.Button(header, text="削除", command=self._remove_group).pack(side="left", padx=3)
        form = ttk.LabelFrame(self, text="グループ名と表示範囲（中心座標・シーン単位）", padding=10)
        form.pack(fill="x", padx=10, pady=5)
        ttk.Label(form, text="グループ名").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.name_var).grid(row=0, column=1, columnspan=3, sticky="ew", pady=3)
        for i, (key, label) in enumerate((("px", "中心 X"), ("py", "中心 Y"),
                                         ("width", "幅"), ("height", "高さ"))):
            r=1 + i//2
            c=(i%2)*2
            ttk.Label(form, text=label).grid(row=r, column=c, sticky="w", pady=4)
            ttk.Entry(form, textvariable=self.entries[key], width=13).grid(row=r, column=c+1, sticky="ew", padx=(3,12), pady=4)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        checks=ttk.Frame(form)
        checks.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6,1))
        ttk.Checkbutton(checks, text="X軸スクロールを許可", variable=self.allow_x).pack(side="left", padx=(0,22))
        ttk.Checkbutton(checks, text="Y軸スクロールを許可", variable=self.allow_y).pack(side="left")
        ttk.Label(self, text="所属アセット（通常配置・トグルイベント内の両方に対応 / Ctrl・Shiftで複数選択）",
                  padding=(12,8)).pack(anchor="w")
        panel=ttk.Frame(self, padding=(12,0))
        panel.pack(fill="both", expand=True)
        self.members_box=tk.Listbox(panel, selectmode="extended", exportselection=False, activestyle="none")
        self.members_box.pack(side="left", fill="both", expand=True)
        bar=ttk.Scrollbar(panel, orient="vertical", command=self.members_box.yview)
        bar.pack(side="right", fill="y")
        self.members_box.configure(yscrollcommand=bar.set)
        asset_names = {a.uid: a.name for a in self.editor.assets}
        for a in self.available_assets:
            if a.event_owner_uid:
                owner_name = asset_names.get(a.event_owner_uid, "不明")
                label = f"{a.name}   [{a.full_key}]  — トグルイベント内: {owner_name}"
            else:
                label = f"{a.name}   [{a.full_key}]  — 通常配置"
            self.members_box.insert("end", label)
        note=ttk.Label(self, text="ホイール: Y / Shift+ホイール: X / ドラッグ: 許可した方向へ移動\n"
                  "表示範囲外の所属画像はプレビューで切り抜かれます。スクロール位置は保存しません。",
                  foreground="#66727d", padding=(12,8))
        note.pack(anchor="w")
        actions=ttk.Frame(self, padding=(12,7))
        actions.pack(fill="x")
        ttk.Button(actions, text="配置画面を開く", command=self._open_layout).pack(side="left", padx=5)
        ttk.Button(actions, text="適用して閉じる", command=self._apply).pack(side="right", padx=5)
        ttk.Button(actions, text="キャンセル", command=self.destroy).pack(side="right", padx=5)

    def _open_layout(self) -> None:
        name = self.name_var.get().strip()
        if name not in self.editor.scroll_groups:
            messagebox.showinfo(APP_NAME, "先に「適用して閉じる」でグループを保存してください。", parent=self)
            return
        self.destroy()
        self.editor.after_idle(lambda: self.editor.open_scroll_workspace(name))

    def _refresh_groups(self) -> None:
        self.group_select.configure(values=tuple(sorted(self.groups)))

    def _start_new(self) -> None:
        self.active_name = None
        self.group_select.set("")
        i=1
        while f"scroll_{i}" in self.groups:
            i+=1
        self.name_var.set(f"scroll_{i}")
        sx, sy = self.editor._pending_spawn
        if self.initial_bounds is not None:
            x0,y0,x1,y1 = self.initial_bounds
            defaults = (("px", (x0+x1)/2), ("py", (y0+y1)/2),
                        ("width", x1-x0), ("height", y1-y0))
            self.initial_bounds = None  # Used only when first opening the new-group form.
        else:
            defaults = (("px",sx),("py",sy),("width",640),("height",360))
        for k,v in defaults:
            self.entries[k].set(jnum(v))
        self.allow_x.set(False)
        self.allow_y.set(True)
        self.members_box.selection_clear(0, "end")
        for i,asset in enumerate(self.available_assets):
            if asset.uid in self.editor.selected_uids:
                self.members_box.selection_set(i)

    def _load_group(self, name: str) -> None:
        group=self.groups.get(name)
        if group is None:
            return
        self.active_name = name
        self.group_select.set(name)
        self.name_var.set(name)
        for k in self.entries:
            self.entries[k].set(jnum(getattr(group,k)))
        self.allow_x.set(group.scroll_x)
        self.allow_y.set(group.scroll_y)
        self.members_box.selection_clear(0,"end")
        for i,asset in enumerate(self.available_assets):
            if asset.uid in group.members:
                self.members_box.selection_set(i)

    def _remove_group(self) -> None:
        if self.active_name is None:
            return
        if not messagebox.askyesno(APP_NAME, f"グループ '{self.active_name}' を削除しますか？\nアセット自体は削除されません。", parent=self):
            return
        self.groups.pop(self.active_name, None)
        self._refresh_groups()
        self._commit()
        self.destroy()

    def _apply(self) -> None:
        name=self.name_var.get().strip()
        try:
            safe_asset_key(name)
            if name != self.active_name and name in self.groups:
                raise ValueError("同名のグループが既にあります。")
            values={k:scene_number(var.get(),k) for k,var in self.entries.items()}
            if not (1 <= values["width"] <= DESIGN_W*4 and 1 <= values["height"] <= DESIGN_H*4):
                raise ValueError("幅・高さは1以上、デザイン解像度の4倍以下にしてください。")
            members={self.available_assets[i].uid for i in self.members_box.curselection()}
        except (ValueError, TypeError) as exc:
            messagebox.showerror(APP_NAME, f"スクロール設定を確認してください:\n{exc}", parent=self)
            return
        if self.active_name:
            self.groups.pop(self.active_name, None)
        for group in self.groups.values():
            group.members.difference_update(members)
        self.groups[name] = ScrollGroup(name, **values, scroll_x=self.allow_x.get(),
                                         scroll_y=self.allow_y.get(), members=members)
        try:
            self._commit()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        self.destroy()

    def _commit(self) -> None:
        if self.editor.scroll_groups == self.groups:
            return
        # Reject self-referential gating, including changes to group memberships.
        for owner in self.editor.assets:
            if owner.kind not in ("toggle_uv", "button", "button_uv"):
                continue
            for event in owner.events:
                if event.get("type") not in ("show_scroll_group", "hide_scroll_group", "toggle_scroll_group"):
                    continue
                previous = event["group_name"]
                updated = (self.name_var.get().strip()
                           if self.active_name == previous and previous not in self.groups
                           and self.name_var.get().strip() in self.groups else previous)
                if (updated in self.groups and owner.uid in self.groups[updated].members
                        and event["type"] != "hide_scroll_group"):
                    raise ValueError(
                        f"{owner.name}: 自身の所属グループは表示・切替イベントで制御できません。")
        self.editor._push_undo()
        old_names = set(self.editor.scroll_groups)
        renamed_from = self.active_name
        renamed_to = self.name_var.get().strip()
        self.editor.scroll_groups = copy.deepcopy(self.groups)
        if (renamed_from and renamed_from != renamed_to and
                self.editor.selected_scroll_group_name == renamed_from and
                renamed_to in self.groups):
            self.editor.selected_scroll_group_name = renamed_to
        for owner in self.editor.assets:
            if owner.kind not in ("toggle_uv", "button", "button_uv"):
                continue
            repaired_events = []
            for event in owner.events:
                if event["type"] not in ("show_scroll_group", "hide_scroll_group", "toggle_scroll_group"):
                    repaired_events.append(event)
                    continue
                name = event["group_name"]
                if renamed_from == name and renamed_to in self.groups and renamed_to != name:
                    name = renamed_to
                if name in self.groups:
                    repaired_events.append({"type": event["type"], "group_name": name})
            owner.events = repaired_events
        self.editor.button_scroll_states = {
            (renamed_to if k == renamed_from and renamed_to in self.groups else k): v
            for k, v in self.editor.button_scroll_states.items()
            if (renamed_to if k == renamed_from and renamed_to in self.groups else k) in self.groups}
        self.editor._refresh_event_windows()
        if self.editor.selected_scroll_group_name not in self.editor.scroll_groups:
            self.editor.selected_scroll_group_name = None
        self.editor.scroll_offsets.clear()
        self.editor._drag_static_cache = None
        self.editor.refresh_tree()
        self.editor.request_draw("both")
        self.editor.status_text.set(f"スクロールグループ: {len(self.groups)}件（プレビューで操作可能）")



class ScrollWorkspacePicker(tk.Toplevel):
    """Choose which scrolling content area to edit, without changing the scene."""
    def __init__(self, editor: "SceneEditor") -> None:
        super().__init__(editor)
        self.result: Optional[str] = None
        self.title("配置を編集するスクロールグループ")
        self.transient(editor)
        self.resizable(False, False)
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="配置編集するグループを選んでください。", padding=(0,0,0,8)).pack(anchor="w")
        choice = ttk.Combobox(frame, state="readonly", width=36,
                              values=list(editor.scroll_groups))
        choice.pack(fill="x")
        choice.current(0)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12,0))
        def accept(_event: Any = None) -> None:
            self.result = choice.get()
            self.destroy()
        ttk.Button(buttons, text="配置画面を開く", command=accept).pack(side="left")
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(side="right")
        choice.bind("<Return>", accept)
        editor._place_child_window(self, editor, (420, 155))
        self.grab_set()
        self.wait_window()


class ScrollWorkspaceDialog(tk.Toplevel):
    """Unclipped, zoomable placement view for scroll-group children.

    Coordinates are the *original scene coordinates*.  Assets can be positioned
    beyond the 1920x1080 scene and the group clip without any scene-size clamp.
    The viewport and scene borders are guides only; runtime preview still clips.
    """
    MIN_ZOOM, MAX_ZOOM = 0.035, 5.0

    def __init__(self, editor: "SceneEditor", name: str) -> None:
        super().__init__(editor)
        self.editor = editor
        self.name = name
        group = editor.scroll_groups[name]
        self.center_x, self.center_y = group.px, group.py
        self.zoom = 0.78
        self.selected: set[str] = set()
        self.primary: Optional[str] = None
        self.drag: Optional[dict[str, Any]] = None
        self.images: list[ImageTk.PhotoImage] = []
        self._photo_cache: dict[tuple[Any,...], ImageTk.PhotoImage] = {}
        self._photo_pixels = 0
        self.polygons: dict[str,list[tuple[float,float]]] = {}
        self._drawing = False
        self._draw_job: Optional[str] = None
        self._syncing = False
        self._snap_axes: tuple[Optional[float], Optional[float]] = (None, None)
        self._spacing_guides: list[EqualGapSnap] = []
        self.group_var = tk.StringVar(value=name)
        self.details = tk.StringVar(value="クリックで選択、ドラッグで移動。表示範囲外にも配置できます。")
        self.numbers: dict[str,tk.StringVar] = {k:tk.StringVar() for k in ('px','py','sx','sy','rotation')}
        self.title(f"スクロールグループ配置画面 — {name}")
        self.geometry(f"{min(1220,max(800,editor.winfo_screenwidth()-70))}x"
                      f"{min(790,max(550,editor.winfo_screenheight()-85))}")
        self.minsize(800,550)
        self.transient(editor)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._construct()
        self._refresh_members()
        self._fit_job=self.after(100,self.fit_group)

    @property
    def group(self) -> ScrollGroup:
        return self.editor.scroll_groups[self.name]

    def _members(self) -> list[Asset]:
        uids = self.group.members
        # Event-owned assets also have editable scene-space transforms. Their
        # visibility is still conditional on the owning toggle in preview.
        return [a for a in self.editor.assets if a.uid in uids]

    def _construct(self) -> None:
        toolbar = ttk.Frame(self, padding=(10,8))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="グループ:").pack(side="left")
        self.choice = ttk.Combobox(toolbar, state="readonly", width=23,
                                   textvariable=self.group_var,
                                   values=list(self.editor.scroll_groups))
        self.choice.pack(side="left", padx=(5,12))
        self.choice.bind("<<ComboboxSelected>>",self.change_group)
        ttk.Button(toolbar,text="表示範囲に合わせる",command=self.fit_group).pack(side="left",padx=2)
        ttk.Button(toolbar,text="全アセットを表示",command=self.fit_all).pack(side="left",padx=2)
        ttk.Button(toolbar,text="＋",width=3,command=lambda:self.zoom_at(1.25)).pack(side="left",padx=(14,2))
        ttk.Button(toolbar,text="－",width=3,command=lambda:self.zoom_at(0.8)).pack(side="left",padx=2)
        self.zoom_text=ttk.Label(toolbar,text="100%",width=8)
        self.zoom_text.pack(side="left",padx=7)
        ttk.Button(toolbar,text="グループ設定…",command=self.edit_group).pack(side="right",padx=2)
        ttk.Button(toolbar,text="閉じる",command=self.close).pack(side="right",padx=2)
        snap_row = ttk.Frame(self, padding=(11, 2))
        snap_row.pack(fill='x')
        ttk.Checkbutton(snap_row, text='位置スナップ', variable=self.editor.snap_enabled,
                        command=self.request_draw).pack(side='left', padx=(0, 10))
        ttk.Checkbutton(snap_row, text='等間隔スナップ', variable=self.editor.spacing_snap_enabled,
                        command=self.request_draw).pack(side='left')
        hint = ttk.Label(self,text="左ドラッグ: 配置 / ハンドル: 拡縮・回転 / 空白ドラッグ: 範囲選択 / Ctrl・Shift: 複数選択 / ホイール: ズーム / 中・右ドラッグ: パン / 矢印: 移動",
                         foreground="#687887",padding=(11,4))
        hint.pack(anchor="w")
        panes=ttk.PanedWindow(self,orient="horizontal")
        panes.pack(fill="both",expand=True,padx=8,pady=(3,4))
        work=ttk.Frame(panes)
        side=ttk.Frame(panes,width=290)
        panes.add(work,weight=5)
        panes.add(side,weight=2)
        self.canvas=tk.Canvas(work,bg="#1b222c",highlightthickness=0,cursor="crosshair")
        self.canvas.pack(fill="both",expand=True)
        self.canvas.bind("<Configure>",lambda _e:self.request_draw())
        self.canvas.bind("<ButtonPress-1>",self.press)
        self.canvas.bind("<B1-Motion>",self.motion)
        self.canvas.bind("<ButtonRelease-1>",self.release)
        self.canvas.bind("<ButtonPress-2>",self.pan_press)
        self.canvas.bind("<B2-Motion>",self.pan_motion)
        self.canvas.bind("<ButtonRelease-2>",self.pan_release)
        self.canvas.bind("<ButtonPress-3>",self.pan_press)
        self.canvas.bind("<B3-Motion>",self.pan_motion)
        self.canvas.bind("<ButtonRelease-3>",self.pan_release)
        self.canvas.bind("<MouseWheel>",self.wheel)
        self.canvas.bind("<Button-4>",self.wheel)
        self.canvas.bind("<Button-5>",self.wheel)
        self.canvas.bind("<Escape>",self.escape)
        self.canvas.bind("<Control-z>",self.undo)
        for key in ("Left","Right","Up","Down"):
            self.canvas.bind(f"<{key}>",self.arrow)
            self.canvas.bind(f"<Shift-{key}>",self.arrow)
        ttk.Label(side,text="所属アセット（範囲外も表示）",padding=(6,5)).pack(anchor="w")
        lst_frame=ttk.Frame(side)
        lst_frame.pack(fill="both",expand=True,padx=4)
        self.member_list=tk.Listbox(lst_frame,selectmode="extended",exportselection=False,
                                    height=13,activestyle="none")
        self.member_list.pack(side="left",fill="both",expand=True)
        scroll=ttk.Scrollbar(lst_frame,orient="vertical",command=self.member_list.yview)
        scroll.pack(side="right",fill="y")
        self.member_list.config(yscrollcommand=scroll.set)
        self.member_list.bind("<<ListboxSelect>>",self.list_select)
        self.uid_list: list[str]=[]
        controls=ttk.LabelFrame(side,text="選択したアセットの変換",padding=7)
        controls.pack(fill="x",padx=5,pady=(12,5))
        for i,(key,label) in enumerate((('px','位置 X'),('py','位置 Y'),
                                         ('sx','拡大率 X'),('sy','拡大率 Y'),
                                         ('rotation','回転（度）'))):
            ttk.Label(controls,text=label).grid(row=i,column=0,sticky="w",pady=4)
            field=ttk.Entry(controls,textvariable=self.numbers[key],width=18)
            field.grid(row=i,column=1,sticky="ew",padx=6,pady=4)
            field.bind("<Return>",lambda _e:self.apply_numbers())
        controls.columnconfigure(1,weight=1)
        ttk.Button(controls,text="座標・倍率・回転を反映",command=self.apply_numbers).grid(
            row=5,column=0,columnspan=2,sticky="ew",pady=7)
        ttk.Button(side,text="画像・UV設定を編集…",command=self.edit_uv).pack(
            fill="x",padx=8,pady=3)
        ttk.Button(side,text="グループの所属アセットを編集…",command=self.edit_group).pack(
            fill="x",padx=8,pady=3)
        ttk.Label(side,text="青枠: スクロール表示範囲（編集ガイド）\n灰色破線: 通常シーンの境界\n範囲外への配置にも制限はありません。",
                  justify="left",foreground="#647888",padding=(9,10)).pack(anchor="w")
        ttk.Label(self,textvariable=self.details,padding=(10,5),anchor="w").pack(fill="x")

    def _map(self,x:float,y:float)->tuple[float,float]:
        return (self.canvas.winfo_width()/2+(x-self.center_x)*self.zoom,
                self.canvas.winfo_height()/2-(y-self.center_y)*self.zoom)

    def _world(self,x:float,y:float)->tuple[float,float]:
        return (self.center_x+(x-self.canvas.winfo_width()/2)/self.zoom,
                self.center_y-(y-self.canvas.winfo_height()/2)/self.zoom)

    def request_draw(self)->None:
        if self.winfo_exists() and self._draw_job is None:
            self._draw_job=self.after_idle(self.draw)

    def _outline(self,asset:Asset)->list[tuple[float,float]]:
        x0,y0,x1,y1=asset.bounds
        return [self._map(*transform_local(asset,x,y)) for x,y in
                ((x0,y1),(x1,y1),(x1,y0),(x0,y0))]

    def _handles(self,a:Asset)->dict[str,tuple[float,float]]:
        x0,y0,x1,y1=a.bounds
        mx,my=(x0+x1)/2,(y0+y1)/2
        loc={'nw':(x0,y1),'n':(mx,y1),'ne':(x1,y1),'e':(x1,my),
             'se':(x1,y0),'s':(mx,y0),'sw':(x0,y0),'w':(x0,my)}
        points={key:self._map(*transform_local(a,*xy)) for key,xy in loc.items()}
        above=ROTATION_HANDLE_DISTANCE/max(self.zoom*a.sy,0.01)
        points['rotate']=self._map(*transform_local(a,mx,y1+above))
        return points

    def draw(self)->None:
        self._draw_job=None
        if not self.winfo_exists() or self._drawing or self.name not in self.editor.scroll_groups:
            return
        self._drawing=True
        try:
            c=self.canvas
            c.delete('all')
            self.images=[]
            self.polygons={}
            cw,ch=max(1,c.winfo_width()),max(1,c.winfo_height())
            # Soft grid follows the world camera and is not a placement constraint.
            grid_step=200.0 if self.zoom>=0.3 else 500.0
            l,b=self._world(0,ch)
            r,t=self._world(cw,0)
            for i in range(math.floor(l/grid_step),math.ceil(r/grid_step)+1):
                x,_=self._map(i*grid_step,0)
                c.create_line(x,0,x,ch,fill='#2a3440')
            for i in range(math.floor(b/grid_step),math.ceil(t/grid_step)+1):
                _,y=self._map(0,i*grid_step)
                c.create_line(0,y,cw,y,fill='#2a3440')
            # Gray dotted scene border: assets are free to cross it.
            sleft,stop=self._map(-DESIGN_W/2,DESIGN_H/2)
            sright,sbottom=self._map(DESIGN_W/2,-DESIGN_H/2)
            c.create_rectangle(sleft,stop,sright,sbottom,outline='#738090',dash=(4,5))
            g=self.group
            x0,y0,x1,y1=g.bounds
            vx0,vy0=self._map(x0,y1)
            vx1,vy1=self._map(x1,y0)
            c.create_rectangle(vx0,vy0,vx1,vy1,outline='#23d1e8',dash=(7,4),width=2)
            c.create_text(vx0+6,vy0+5,anchor='nw',fill='#71e7f4',
                          text=f'スクロール表示範囲: {g.name}  (X:{g.scroll_x} / Y:{g.scroll_y})')
            for a in self._members():
                corners=self._outline(a)
                if not corners:
                    continue
                self.polygons[a.uid]=corners
                minx,maxx=min(pt[0] for pt in corners),max(pt[0] for pt in corners)
                miny,maxy=min(pt[1] for pt in corners),max(pt[1] for pt in corners)
                if maxx<-250 or minx>cw+250 or maxy<-250 or miny>ch+250:
                    continue
                try:
                    raster,_=asset_bitmap(a,0,self.zoom)
                    bx0,by0,bx1,by1=a.bounds
                    if a.kind=='toggle_uv' and a.toggle_default=='on' and a.pressed_image is not None:
                        raster=a.pressed_image
                        iw,ih=raster.size
                    elif a.kind=='window':
                        iw,ih=raster.size
                    else:
                        iw,ih=bx1-bx0,by1-by0
                    w=max(1,round(iw*self.zoom*(1 if a.kind=='window' else a.sx)))
                    h=max(1,round(ih*self.zoom*(1 if a.kind=='window' else a.sy)))
                    if w*h<=MAX_RENDER_PIXELS and w<=8192 and h<=8192:
                        key=(a.uid,id(raster),w,h,round(a.rotation,8),a.flip_x,a.flip_y)
                        photo=self._photo_cache.get(key)
                        if photo is None:
                            sampler=RESAMPLE_NEAREST if a.kind=='window' else RESAMPLE
                            scaled=raster.resize((w,h),sampler)
                            scaled=apply_scene_flips(scaled,a)
                            scaled=scaled.rotate(math.degrees(a.rotation),expand=True,resample=sampler)
                            photo=ImageTk.PhotoImage(scaled)
                            # Keep per-workspace texture cache bounded when editing
                            # scenes with hundreds of large scrolling sprites.
                            pixels=scaled.width*scaled.height
                            while self._photo_cache and (len(self._photo_cache)>=96 or
                                   self._photo_pixels+pixels>18_000_000):
                                oldest=next(iter(self._photo_cache))
                                removed=self._photo_cache.pop(oldest)
                                self._photo_pixels-=removed.width()*removed.height()
                            if pixels<=18_000_000:
                                self._photo_cache[key]=photo
                                self._photo_pixels+=pixels
                        self.images.append(photo)
                        mx,my=(bx0+bx1)/2,(by0+by1)/2
                        sx,sy=self._map(*transform_local(a,mx,my))
                        c.create_image(sx,sy,image=photo,anchor='center')
                except (OSError,ValueError,MemoryError,ZeroDivisionError) as exc:
                    self.details.set(f'{a.name}: 描画できません: {exc}')
                border=SELECTION_COLOR if a.uid in self.selected else '#718297'
                c.create_polygon(*sum(([x,y] for x,y in corners),[]),
                                 outline=border,fill='',width=2 if a.uid in self.selected else 1)
            # Display-viewport overlay is always on top of images for clarity.
            c.create_rectangle(vx0,vy0,vx1,vy1,outline='#28ddf0',dash=(7,4),width=2)
            primary=self._asset(self.primary)
            if primary is not None and len(self.selected)==1:
                handles=self._handles(primary)
                c.create_line(*handles['n'],*handles['rotate'],fill=SELECTION_COLOR,width=2)
                for label,(x,y) in handles.items():
                    if label=='rotate':
                        c.create_oval(x-6,y-6,x+6,y+6,outline=SELECTION_COLOR,fill='#263d54',width=2)
                    else:
                        c.create_rectangle(x-5,y-5,x+5,y+5,outline='#143446',fill='#f0fbff',width=1)
            if self.drag and self.drag.get('mode')=='marquee':
                x,y=self.drag['screen']
                ex,ey=self.drag['last']
                c.create_rectangle(x,y,ex,ey,outline='#ffcd76',dash=(4,3),width=2)
            guide_x, guide_y = self._snap_axes
            if self.drag is not None and self.editor.snap_enabled.get():
                if guide_x is not None:
                    guide_sx, _ = self._map(guide_x, 0)
                    c.create_line(guide_sx, 0, guide_sx, ch, fill=SNAP_GUIDE_COLOR,
                                  dash=SNAP_GUIDE_DASH, width=1)
                if guide_y is not None:
                    _, guide_sy = self._map(0, guide_y)
                    c.create_line(0, guide_sy, cw, guide_sy, fill=SNAP_GUIDE_COLOR,
                                  dash=SNAP_GUIDE_DASH, width=1)
            if self.drag is not None and self.editor.spacing_snap_enabled.get():
                for gline in self._spacing_guides:
                    for start, end in gline.intervals:
                        if gline.axis == 'x':
                            xa, ya = self._map(start, gline.perpendicular)
                            xb, _ = self._map(end, gline.perpendicular)
                        else:
                            xa, ya = self._map(gline.perpendicular, start)
                            _, yb = self._map(gline.perpendicular, end)
                        if gline.axis == 'x':
                            c.create_line(xa, ya, xb, ya, fill='#47d7c0', width=2,
                                          arrow=tk.BOTH)
                        else:
                            c.create_line(xa, ya, xa, yb, fill='#47d7c0', width=2,
                                          arrow=tk.BOTH)
            self.zoom_text.config(text=f'{self.zoom*100:.0f}%')
        finally:
            self._drawing=False

    def _asset(self,uid:Optional[str])->Optional[Asset]:
        return self.editor._asset_by_uid(uid) if uid else None

    def _refresh_members(self)->None:
        self._syncing=True
        try:
            self.member_list.delete(0,'end')
            self.uid_list=[]
            for a in self._members():
                self.uid_list.append(a.uid)
                self.member_list.insert('end',f'{a.name}  ({a.kind})')
            for i,uid in enumerate(self.uid_list):
                if uid in self.selected:
                    self.member_list.selection_set(i)
        finally:
            self._syncing=False
        self._sync_numbers()
        self.request_draw()

    def _select(self,uids:set[str],primary:Optional[str]=None)->None:
        available=set(self.uid_list)
        self.selected=set(uids)&available
        self.primary=(primary if primary in self.selected else next(iter(self.selected),None))
        self._syncing=True
        try:
            self.member_list.selection_clear(0,'end')
            for i,uid in enumerate(self.uid_list):
                if uid in self.selected:
                    self.member_list.selection_set(i)
        finally:
            self._syncing=False
        self._sync_numbers()
        self.request_draw()

    def _sync_numbers(self)->None:
        a=self._asset(self.primary)
        for key,v in self.numbers.items():
            value=(math.degrees(a.rotation) if key=='rotation' else getattr(a,key)) if a is not None else ''
            v.set(jnum(value) if a is not None else '')
        if a is not None:
            self.details.set(f'{a.name}: X={a.px:.1f}, Y={a.py:.1f} / 選択 {len(self.selected)}件')

    def list_select(self,_e:Any=None)->None:
        if self._syncing:
            return
        chosen={self.uid_list[i] for i in self.member_list.curselection() if i<len(self.uid_list)}
        self._select(chosen,next(iter(chosen),None))
        self.canvas.focus_set()

    def _hit(self,x:float,y:float)->Optional[Asset]:
        for a in reversed(self._members()):
            pts=self.polygons.get(a.uid)
            if pts and inside_polygon(x,y,pts):
                return a
        return None

    def _workspace_snap_refs(self, moving: set[str]) -> tuple[list[float],list[float],tuple[tuple[float,float,float,float],...]]:
        """Group viewport, scene edges and other group members are snap references."""
        gx0,gy0,gx1,gy1 = self.group.bounds
        ref_x = [-DESIGN_W/2, 0.0, DESIGN_W/2, gx0, (gx0+gx1)/2, gx1]
        ref_y = [-DESIGN_H/2, 0.0, DESIGN_H/2, gy0, (gy0+gy1)/2, gy1]
        others = []
        for asset in self._members():
            if asset.uid in moving:
                continue
            x0,y0,x1,y1 = asset_world_aabb(asset)
            ref_x.extend((x0,(x0+x1)/2,x1))
            ref_y.extend((y0,(y0+y1)/2,y1))
            others.append((x0,y0,x1,y1))
        return sorted(ref_x),sorted(ref_y),tuple(others)

    def _snap_workspace_position(self, original_bounds: tuple[float,float,float,float],
                                 dx: float, dy: float, drag:dict[str,Any]
                                 ) -> tuple[float,float]:
        x0,y0,x1,y1 = original_bounds
        box = (x0+dx,y0+dy,x1+dx,y1+dy)
        threshold = SNAP_DISTANCE_PX / max(self.zoom,0.001)
        cx=cy=0.0
        gx=gy=None
        self._spacing_guides=[]
        if self.editor.snap_enabled.get():
            cx,gx = nearest_snap((box[0],(box[0]+box[2])/2,box[2]),drag['refs_x'],threshold)
            cy,gy = nearest_snap((box[1],(box[1]+box[3])/2,box[3]),drag['refs_y'],threshold)
        if self.editor.spacing_snap_enabled.get():
            for axis in ('x','y'):
                found=equal_gap_snap(box,drag['spacing_boxes'],axis,threshold,
                                     proximity=threshold*2)
                if found is None:
                    continue
                if axis=='x' and (gx is None or abs(found.delta)<=abs(cx)):
                    cx,gx=found.delta,None
                    self._spacing_guides.append(found)
                elif axis=='y' and (gy is None or abs(found.delta)<=abs(cy)):
                    cy,gy=found.delta,None
                    self._spacing_guides.append(found)
        self._snap_axes=(gx,gy)
        return dx+cx,dy+cy

    def press(self,e:tk.Event)->None:
        self.canvas.focus_set()
        wxy=self._world(e.x,e.y)
        selected_asset=self._asset(self.primary) if len(self.selected)==1 else None
        handle=None
        if selected_asset is not None:
            for name,(x,y) in self._handles(selected_asset).items():
                if math.hypot(e.x-x,e.y-y)<9:
                    handle=name
                    break
        a=selected_asset if handle else self._hit(e.x,e.y)
        add=bool(e.state&0x1)
        toggle=bool(e.state&0x4)
        if a is None:
            self.drag={'mode':'marquee','screen':(e.x,e.y),'last':(e.x,e.y),
                       'add':add,'base':set(self.selected)}
            if not add:
                self._select(set())
            return
        if toggle:
            choice=set(self.selected)
            if a.uid in choice: choice.remove(a.uid)
            else: choice.add(a.uid)
            self._select(choice,a.uid)
            return
        if add:
            self._select(self.selected|{a.uid},a.uid)
            return
        if a.uid not in self.selected:
            self._select({a.uid},a.uid)
        changed=[item for item in self._members() if item.uid in self.selected]
        before=self.editor._snapshot()
        self._snap_axes=(None,None)
        self._spacing_guides=[]
        refs_x,refs_y,spacing_boxes=self._workspace_snap_refs({item.uid for item in changed})
        bounds=[asset_world_aabb(item) for item in changed]
        initial_box=(min(b[0] for b in bounds),min(b[1] for b in bounds),
                     max(b[2] for b in bounds),max(b[3] for b in bounds))
        drag={'refs_x':refs_x,'refs_y':refs_y,'spacing_boxes':spacing_boxes,
              'initial_box':initial_box,'mode':'rotate' if handle=='rotate' else 'resize' if handle else 'move',
              'start':wxy,'members':changed,
              'originals':{item.uid:(item.px,item.py,item.sx,item.sy,item.rotation) for item in changed},
              'before':before,'asset':a,'handle':handle,'changed':False}
        if handle=='rotate':
            drag['angle']=math.atan2(wxy[1]-a.py,wxy[0]-a.px)
        elif handle:
            x0,y0,x1,y1=a.bounds
            mx,my=(x0+x1)/2,(y0+y1)/2
            lx=x0 if 'w' in handle else x1 if 'e' in handle else mx
            ly=y1 if 'n' in handle else y0 if 's' in handle else my
            ox=x1 if 'w' in handle else x0 if 'e' in handle else mx
            oy=y0 if 'n' in handle else y1 if 's' in handle else my
            drag.update(local=(lx,ly),opposite=(ox,oy),
                        fixed=transform_local(a,ox,oy))
        self.drag=drag

    def motion(self,e:tk.Event)->None:
        d=self.drag
        if d is None:return
        if d['mode']=='marquee':
            d['last']=(e.x,e.y)
            self.request_draw()
            return
        world=self._world(e.x,e.y)
        mode=d['mode']
        self._snap_axes=(None,None)
        self._spacing_guides=[]
        if mode=='move':
            delta=self._snap_workspace_position(d['initial_box'],
                        world[0]-d['start'][0],world[1]-d['start'][1],d)
            for a in d['members']:
                px,py,_,_,_=d['originals'][a.uid]
                a.px=px+delta[0]
                a.py=py+delta[1]
        elif mode=='rotate':
            a=d['asset']
            px,py,_,_,rot=d['originals'][a.uid]
            angle=math.atan2(world[1]-py,world[0]-px)
            diff=math.atan2(math.sin(angle-d['angle']),math.cos(angle-d['angle']))
            a.rotation=rot+diff
            if e.state&0x1:
                a.rotation=round(a.rotation/(math.pi/12))*(math.pi/12)
        else:
            a=d['asset']
            if self.editor.snap_enabled.get() and not (e.state & 0x1):
                threshold=SNAP_DISTANCE_PX/max(self.zoom,0.001)
                cx,gx=nearest_snap((world[0],),d['refs_x'],threshold)
                cy,gy=nearest_snap((world[1],),d['refs_y'],threshold)
                world=(world[0]+cx,world[1]+cy)
                self._snap_axes=(gx,gy)
            px,py,sx,sy,rot=d['originals'][a.uid]
            ox,oy=d['fixed']
            cs,sn=math.cos(rot),math.sin(rot)
            dx,dy=world[0]-ox,world[1]-oy
            invx,invy=cs*dx+sn*dy,-sn*dx+cs*dy
            lx,ly=d['local']; olx,oly=d['opposite']
            nsx=sx if lx==olx else max(MIN_SCALE,min(MAX_SCALE,invx/(lx-olx)))
            nsy=sy if ly==oly else max(MIN_SCALE,min(MAX_SCALE,invy/(ly-oly)))
            if e.state&0x1 and lx!=olx and ly!=oly:
                f=max(nsx/sx,nsy/sy)
                nsx=max(MIN_SCALE,min(MAX_SCALE,sx*f))
                nsy=max(MIN_SCALE,min(MAX_SCALE,sy*f))
            if a.kind=='window':
                minx,miny=window_min_scales(a)
                nsx=max(nsx,minx);nsy=max(nsy,miny)
            a.sx,a.sy=nsx,nsy
            a.px=ox-(cs*nsx*olx-sn*nsy*oly)
            a.py=oy-(sn*nsx*olx+cs*nsy*oly)
        d['changed']=True
        self._sync_numbers()
        self.request_draw()

    def release(self,_e:Any=None)->None:
        d=self.drag
        self.drag=None
        self._snap_axes=(None,None)
        self._spacing_guides=[]
        if not d:return
        if d['mode']=='marquee':
            x0,y0=d['screen']; x1,y1=d['last']
            left,right=sorted((x0,x1));top,bottom=sorted((y0,y1))
            chosen=set(d['base']) if d['add'] else set()
            for uid,pts in self.polygons.items():
                if (min(x for x,y in pts)<=right and max(x for x,y in pts)>=left and
                    min(y for x,y in pts)<=bottom and max(y for x,y in pts)>=top):
                    chosen.add(uid)
            self._select(chosen)
            return
        changed=any((a.px,a.py,a.sx,a.sy,a.rotation)!=d['originals'][a.uid]
                    for a in d['members'])
        if changed:
            self.editor._push_undo(d['before'])
            self.editor._drag_static_cache=None
            self.editor.request_draw('both')
            self.editor.status_text.set(f'{self.name}: 配置を更新しました。Ctrl+Zで取り消せます。')
        self._sync_numbers()
        self.request_draw()

    def escape(self,_e:Any=None)->str:
        self._snap_axes=(None,None)
        self._spacing_guides=[]
        d=self.drag
        if d:
            for a in d.get('members',[]):
                a.px,a.py,a.sx,a.sy,a.rotation=d['originals'][a.uid]
            self.drag=None
            self.request_draw()
        else:
            self._select(set())
        return 'break'

    def pan_press(self,e:tk.Event)->None:
        self.drag={'mode':'pan','start':(e.x,e.y),'center':(self.center_x,self.center_y)}

    def pan_motion(self,e:tk.Event)->None:
        d=self.drag
        if not d or d['mode']!='pan':return
        self.center_x=d['center'][0]-(e.x-d['start'][0])/self.zoom
        self.center_y=d['center'][1]+(e.y-d['start'][1])/self.zoom
        self.request_draw()

    def pan_release(self,_e:Any=None)->None:
        if self.drag and self.drag.get('mode')=='pan':self.drag=None

    def zoom_at(self,factor:float,x:Optional[float]=None,y:Optional[float]=None)->None:
        cx=self.canvas.winfo_width()/2 if x is None else x
        cy=self.canvas.winfo_height()/2 if y is None else y
        before=self._world(cx,cy)
        self.zoom=max(self.MIN_ZOOM,min(self.MAX_ZOOM,self.zoom*factor))
        after=self._world(cx,cy)
        self.center_x+=before[0]-after[0]
        self.center_y+=before[1]-after[1]
        self.request_draw()

    def wheel(self,e:tk.Event)->str:
        delta=getattr(e,'delta',0)
        if not delta:
            delta=120 if getattr(e,'num',0)==4 else -120
        self.zoom_at(1.15 if delta>0 else 1/1.15,e.x,e.y)
        return 'break'

    def _fit_bounds(self,x0:float,y0:float,x1:float,y1:float)->None:
        width=max(x1-x0,1)
        height=max(y1-y0,1)
        cw=max(120,self.canvas.winfo_width())
        ch=max(120,self.canvas.winfo_height())
        self.center_x=(x0+x1)/2
        self.center_y=(y0+y1)/2
        self.zoom=max(self.MIN_ZOOM,min(self.MAX_ZOOM,min(cw/(width*1.4),ch/(height*1.4))))
        self.request_draw()

    def fit_group(self)->None:
        self._fit_bounds(*self.group.bounds)

    def fit_all(self)->None:
        b=list(self.group.bounds)
        for a in self._members():
            x0,y0,x1,y1=asset_world_aabb(a)
            b[0]=min(b[0],x0);b[1]=min(b[1],y0)
            b[2]=max(b[2],x1);b[3]=max(b[3],y1)
        self._fit_bounds(*b)

    def arrow(self,e:tk.Event)->str:
        if not self.selected:return 'break'
        step=10 if e.state&0x1 else 1
        dx=(step if e.keysym=='Right' else -step if e.keysym=='Left' else 0)
        dy=(step if e.keysym=='Up' else -step if e.keysym=='Down' else 0)
        if not dx and not dy:return 'break'
        self.editor._push_undo()
        for a in self._members():
            if a.uid in self.selected:
                a.px+=dx;a.py+=dy
        self.editor.request_draw('both')
        self._sync_numbers();self.request_draw()
        return 'break'

    def apply_numbers(self)->None:
        a=self._asset(self.primary)
        if a is None:return
        try:
            values={key:scene_number(var.get(),key) for key,var in self.numbers.items()}
            if not MIN_SCALE<=values['sx']<=MAX_SCALE or not MIN_SCALE<=values['sy']<=MAX_SCALE:
                raise ValueError('倍率が範囲外です。')
            if a.kind=='window':
                mx,my=window_min_scales(a)
                if values['sx']<mx or values['sy']<my:
                    raise ValueError('ウィンドウの角を維持できない倍率です。')
        except ValueError as exc:
            messagebox.showerror(APP_NAME,f'値が不正です: {exc}',parent=self)
            return
        updated=(values['px'],values['py'],values['sx'],values['sy'],math.radians(values['rotation']))
        if (a.px,a.py,a.sx,a.sy,a.rotation)==updated:return
        self.editor._push_undo()
        a.px,a.py,a.sx,a.sy,a.rotation=updated
        self.editor.request_draw('both')
        self._sync_numbers();self.request_draw()

    def edit_uv(self)->None:
        a=self._asset(self.primary)
        if a is None:return
        self.editor.select(a.uid)
        if a.kind=='window':self.editor.edit_selected_window()
        else:self.editor.edit_selected_uv()
        self.request_draw()

    def edit_group(self)->None:
        dialog=self.editor.edit_scroll_groups(self.name)
        self.wait_window(dialog)
        if not self.winfo_exists():
            return
        if self.name not in self.editor.scroll_groups:
            self.close()
            return
        self._photo_cache.clear()
        self._photo_pixels=0
        self._refresh_members()

    def change_group(self,_e:Any=None)->None:
        name=self.group_var.get()
        if name not in self.editor.scroll_groups:return
        self.name=name
        self.title(f'スクロールグループ配置画面 — {name}')
        self.selected.clear();self.primary=None
        self._refresh_members();self.fit_group()

    def undo(self,_e:Any=None)->str:
        self.editor.undo()
        if self.name not in self.editor.scroll_groups:
            self.close()
        else:
            self._refresh_members()
        return 'break'

    def close(self)->None:
        if getattr(self,'_fit_job',None) is not None:
            self.after_cancel(self._fit_job)
            self._fit_job=None
        if self.drag and self.drag.get('mode') in ('move','rotate','resize'):
            self.release()
        if self._draw_job is not None:
            self.after_cancel(self._draw_job)
            self._draw_job=None
        self._photo_cache.clear()
        self.editor._scroll_workspace=None
        self.destroy()


class SceneEditor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        try:
            ASSET_ROOT.mkdir(parents=True, exist_ok=True)
            prepare_scene_output_dir()
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_NAME,
                f"assets / out/scene フォルダーを準備できません。\n{exc}", parent=self)
            self.destroy()
            raise RuntimeError("フォルダーの準備に失敗しました") from exc
        self.geometry("1560x920")
        self.minsize(1080, 720)
        self.scene_name = tk.StringVar(value=DEFAULT_SCENE)
        self.status_text = tk.StringVar(value="編集画面を右クリックするとアセットを配置できます。")
        self.assets: list[Asset] = []
        self.scroll_groups: dict[str, ScrollGroup] = {}
        self.scroll_offsets: dict[str, tuple[float, float]] = {}
        self._scroll_press: Optional[dict[str, Any]] = None
        self._scroll_dispatch_click = False
        self.selected_scroll_group_name: Optional[str] = None  # Editor-only viewport selection.
        self.selection_mode = tk.StringVar(value="asset")  # Asset or scroll viewport hit testing.
        self._scroll_workspace: Optional[tk.Toplevel] = None
        self._scroll_create_mode = False  # Drag on canvas to define a new viewport.

        self.selected_uid: Optional[str] = None
        self.selected_uids: set[str] = set()
        self._syncing_tree_selection = False
        self._group_drag_photos: list[ImageTk.PhotoImage] = []
        self._group_drag_items: dict[str, int] = {}
        self.background_file: Optional[Path] = None
        self.background_image: Optional[Image.Image] = None
        self.background_critical = True
        self.background_prefetch = False
        self.bg_critical_var = tk.BooleanVar(value=True)
        self.bg_prefetch_var = tk.BooleanVar(value=False)
        self.audio: dict[str, dict[str, dict[str, str]]] = {"bgm": {}, "se": {}}
        self.audio_flags: dict[str, tuple[bool, bool]] = {}
        self.audio_player = AudioPlayer(self, self.status_text)
        self.preview_bgm_volume_var = tk.DoubleVar(value=self.audio_player.volumes['bgm'])
        self.preview_se_volume_var = tk.DoubleVar(value=self.audio_player.volumes['se'])
        self.preview_volume_text = {
            'bgm': tk.StringVar(value=f"{self.audio_player.volumes['bgm']}%"),
            'se': tk.StringVar(value=f"{self.audio_player.volumes['se']}%"),
        }
        self.saved_path: Optional[Path] = None
        # All scene saves are fixed to ./out/scene; asset dialogs keep per-type history.
        self._last_asset_dir: Optional[Path] = None
        self._last_asset_kind_dirs: dict[str, Path] = {}
        self._audio_window: Optional[tk.Toplevel] = None
        self._audio_window_xy: Optional[tuple[int, int]] = None
        self.edit_photo: Optional[ImageTk.PhotoImage] = None
        self.preview_photo: Optional[ImageTk.PhotoImage] = None
        self.viewports: dict[str, tuple[float, float, float, float, float]] = {}
        self.editor_shapes: dict[str, list[tuple[float, float]]] = {}
        self.preview_shapes: dict[str, list[tuple[float, float]]] = {}
        self._drag: Optional[dict[str, Any]] = None
        self.snap_enabled = tk.BooleanVar(value=True)
        self.spacing_snap_enabled = tk.BooleanVar(value=True)
        self.editor_hidden_uids: set[str] = set()  # Temporary editor visibility; never serialized.
        self.preview_sync_visibility = tk.BooleanVar(value=False)  # Manual editor mask -> preview.
        self.editor_sync_preview_visibility = tk.BooleanVar(value=False)  # Preview events/states -> edit viewport.
        self._pending_spawn = (0.0, 0.0)
        self._needs_draw: Optional[str] = None
        self._draw_job: Optional[str] = None
        self.playing: dict[str, float] = {}
        self.last_frame: dict[str, int] = {}
        self.pressed_button_uid: Optional[str] = None
        self.toggle_states: dict[str, str] = {}  # preview state; persisted initial state lives on Asset
        self.button_scroll_states: dict[str, bool] = {}  # preview-only latched button commands, by group name
        self._tick_job: Optional[str] = None
        self._property_updating = False
        # Property numbers are adjusted with the wheel rather than label dragging.

        # Editing state snapshots hold references to decoded images; never duplicate pixel buffers.
        self._undo_stack: list[dict[str, Any]] = []
        self._asset_clipboard: Optional[Asset] = None  # Internal clipboard; share decoded image buffers.
        self._asset_clipboard_scroll_group: Optional[str] = None
        self._asset_row_drag_source: Optional[str] = None
        self._asset_row_drag_start_y = 0
        self._asset_row_drag_changed = False
        self._paste_count = 0
        self._clipboard_descendants: dict[str, Asset] = {}
        self._event_windows: dict[str, tk.Toplevel] = {}
        self._drag_base_photo: Optional[ImageTk.PhotoImage] = None
        self._drag_upper_photo: Optional[ImageTk.PhotoImage] = None
        self._drag_asset_photo: Optional[ImageTk.PhotoImage] = None
        self._drag_asset_item: Optional[int] = None
        self._drag_visual_ready = False
        self._drag_static_cache: Optional[tuple[Any, ImageTk.PhotoImage]] = None
        self._last_transform_refresh = 0.0
        self._background_cache: dict[tuple[int, int, int], Image.Image] = {}
        self._audio_refresh: Optional[Any] = None
        self._make_ui()
        self.bind("<Control-z>", self.undo)
        self.bind("<Control-c>", self.copy_selected)
        self.bind("<Control-v>", self.paste_asset)
        self.bind("<Control-s>", lambda _: self.save_scene())
        self.bind("<Control-Shift-S>", lambda _: self.save_as_scene())
        self._startup_draw_job: Optional[str] = self.after(120, self._startup_draw)
        self.protocol("WM_DELETE_WINDOW", self.quit_app)

    def _make_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=(9, 8))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="シーン名:").pack(side="left")
        scene_entry = ttk.Entry(toolbar, textvariable=self.scene_name, width=18)
        scene_entry.pack(side="left", padx=(4, 12))
        ttk.Button(toolbar, text="新規", command=self.new_scene).pack(side="left", padx=2)
        ttk.Button(toolbar, text="元に戻す (Ctrl+Z)", command=self.undo).pack(side="left", padx=2)
        ttk.Button(toolbar, text="コピー", command=lambda: self.copy_selected(force=True)).pack(side="left", padx=2)
        ttk.Button(toolbar, text="貼り付け", command=lambda: self.paste_asset(force=True)).pack(side="left", padx=2)
        ttk.Button(toolbar, text="開く", command=self.open_scene).pack(side="left", padx=2)
        ttk.Button(toolbar, text="保存", command=self.save_scene).pack(side="left", padx=2)
        ttk.Button(toolbar, text="別名保存", command=self.save_as_scene).pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=9)
        ttk.Button(toolbar, text="背景を設定", command=self.pick_background).pack(side="left", padx=2)
        ttk.Button(toolbar, text="背景を削除", command=self.remove_background).pack(side="left", padx=2)
        ttk.Checkbutton(toolbar, text="背景 critical", variable=self.bg_critical_var,
                        command=self.apply_background_flags).pack(side="left", padx=(8, 1))
        ttk.Checkbutton(toolbar, text="prefetch", variable=self.bg_prefetch_var,
                        command=self.apply_background_flags).pack(side="left", padx=1)
        ttk.Button(toolbar, text="BGM / SE", command=self.edit_audio).pack(side="left", padx=(12, 2))
        ttk.Button(toolbar, text="BGM再生", command=self.play_primary_bgm).pack(side="left", padx=2)
        ttk.Button(toolbar, text="BGM停止", command=lambda: self.audio_player.stop("bgm")).pack(side="left", padx=2)

        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        left = ttk.Frame(main)
        right_shell = ttk.Frame(main)
        main.add(left, weight=7)
        main.add(right_shell, weight=4)
        # Scrollable inspector keeps all editing controls accessible on 720p screens.
        self.inspector_canvas = tk.Canvas(right_shell, highlightthickness=0, borderwidth=0,
                                          bg="#f0f0f0", width=445)
        inspector_scroll = ttk.Scrollbar(right_shell, orient="vertical",
                                         command=self.inspector_canvas.yview)
        inspector_scroll.pack(side="right", fill="y")
        self.inspector_canvas.pack(side="left", fill="both", expand=True)
        self.inspector_canvas.configure(yscrollcommand=inspector_scroll.set)
        right = ttk.Frame(self.inspector_canvas)
        right_id = self.inspector_canvas.create_window((0, 0), window=right, anchor="nw")
        right.bind("<Configure>", lambda _: self.inspector_canvas.configure(
            scrollregion=self.inspector_canvas.bbox("all")))
        self.inspector_canvas.bind("<Configure>", lambda e: self.inspector_canvas.itemconfigure(
            right_id, width=e.width))
        self.inspector_canvas.bind("<MouseWheel>", lambda e: self.inspector_canvas.yview_scroll(
            -1 if e.delta > 0 else 1, "units"))
        self.inspector_canvas.bind("<Button-4>", lambda _: self.inspector_canvas.yview_scroll(-1, "units"))
        self.inspector_canvas.bind("<Button-5>", lambda _: self.inspector_canvas.yview_scroll(1, "units"))

        edit_header = ttk.Frame(left)
        edit_header.pack(fill="x")
        ttk.Label(edit_header, text="編集画面（16:9） | 移動・拡縮・回転",
                  padding=(3, 5)).pack(side="left")
        mode_controls = ttk.Frame(left)
        mode_controls.pack(fill="x", padx=4, pady=(0, 3))
        ttk.Label(mode_controls, text="編集対象:").pack(side="left", padx=(0, 4))
        ttk.Radiobutton(mode_controls, text="アセット", value="asset",
                        variable=self.selection_mode, command=self._on_selection_mode_change).pack(side="left", padx=3)
        ttk.Radiobutton(mode_controls, text="スクロールグループ", value="scroll",
                        variable=self.selection_mode, command=self._on_selection_mode_change).pack(side="left", padx=3)
        ttk.Button(mode_controls, text="選択グループの配置画面を開く…",
                   command=self.open_scroll_workspace).pack(side="right", padx=2)
        ttk.Checkbutton(edit_header, text="プレビュー表示と同期",
                        variable=self.editor_sync_preview_visibility,
                        command=self._on_editor_visibility_sync_change).pack(side="right", padx=(5, 4))
        ttk.Checkbutton(edit_header, text="等間隔スナップ", variable=self.spacing_snap_enabled,
                        command=self._toggle_spacing_snap).pack(side="right", padx=(6, 4))
        ttk.Checkbutton(edit_header, text="位置スナップ", variable=self.snap_enabled,
                        command=self._toggle_snap).pack(side="right", padx=(6, 4))
        self.edit_canvas = tk.Canvas(left, bg="#181d26", highlightthickness=0, cursor="arrow")
        self.edit_canvas.pack(fill="both", expand=True)
        self.edit_canvas.bind("<Configure>", lambda _: self.request_draw("edit"))
        self.edit_canvas.bind("<ButtonPress-1>", self.editor_press)
        self.edit_canvas.bind("<B1-Motion>", self.editor_motion)
        self.edit_canvas.bind("<ButtonRelease-1>", self.editor_release)
        self.edit_canvas.bind("<Button-3>", self.editor_context)
        self.edit_canvas.bind("<Delete>", lambda _: self.delete_selected())
        self.edit_canvas.bind("<Control-d>", lambda _: self.duplicate_selected())
        self.edit_canvas.bind("<Escape>", self._escape_editor)
        self.edit_canvas.bind("<Motion>", self._editor_hover)
        for key in ("Left", "Right", "Up", "Down"):
            self.edit_canvas.bind(f"<{key}>", self.move_with_arrow)
            self.edit_canvas.bind(f"<Shift-{key}>", self.move_with_arrow)
        hint = ttk.Label(left, text="アセットモード: 通常UIの選択・移動 | グループモード: 水色枠の移動・範囲変更 | 所属UIの配置は別画面",
                         foreground="#6c7682", padding=(3, 6))
        hint.pack(anchor="w")

        preview_header = ttk.Frame(right)
        preview_header.pack(fill="x")
        ttk.Label(preview_header, text="プレビュー（16:9）", padding=(3, 5)).pack(side="left")
        ttk.Checkbutton(preview_header, text="編集表示と同期",
                        variable=self.preview_sync_visibility,
                        command=self._on_preview_visibility_sync_change).pack(side="right", padx=(2, 4))
        self.preview_canvas = tk.Canvas(right, bg="#181d26", height=285, highlightthickness=0)
        self.preview_canvas.pack(fill="x", padx=(5, 2))
        self.preview_canvas.bind("<Configure>", lambda _: self.request_draw("preview"))
        self.preview_canvas.bind("<ButtonPress-1>", self.preview_click)
        self.preview_canvas.bind("<ButtonRelease-1>", self.preview_release)
        self.preview_canvas.bind("<B1-Motion>", self.preview_drag_held)
        self.preview_canvas.bind("<MouseWheel>", self.preview_wheel)
        self.preview_canvas.bind("<Shift-MouseWheel>", self.preview_wheel)
        self.preview_canvas.bind("<Button-4>", self.preview_wheel)
        self.preview_canvas.bind("<Button-5>", self.preview_wheel)
        # Editor-local preview gain controls. These are never part of serialize().
        preview_audio_row = ttk.Frame(right, padding=(5, 3))
        preview_audio_row.pack(fill='x')
        for kind, caption, var in (
            ('bgm', 'BGM音量', self.preview_bgm_volume_var),
            ('se', 'SE音量', self.preview_se_volume_var),
        ):
            sub = ttk.Frame(preview_audio_row)
            sub.pack(side='left', fill='x', expand=True, padx=3)
            ttk.Label(sub, text=caption).pack(side='left')
            level = ttk.Scale(sub, from_=0, to=100, orient='horizontal',
                              variable=var, length=82,
                              command=lambda val, k=kind: self._change_preview_volume(k, val))
            level.pack(side='left', fill='x', expand=True, padx=(4, 2))
            ttk.Label(sub, textvariable=self.preview_volume_text[kind], width=5,
                      anchor='e').pack(side='left')
        ttk.Separator(right).pack(fill="x", padx=5, pady=8)
        asset_header = ttk.Frame(right, padding=(5, 1))
        asset_header.pack(fill="x")
        ttk.Label(asset_header, text="配置アセット（上ほど手前）  |  Ctrl / Shift で複数選択").pack(side="left")
        visibility_row = ttk.Frame(right, padding=(5, 2))
        visibility_row.pack(fill="x")
        self.visibility_buttons: dict[str, ttk.Button] = {}
        for key, caption, callback in (
            ("show", "選択を一括表示", lambda: self.set_selected_editor_visibility(True)),
            ("hide", "選択を一括非表示", lambda: self.set_selected_editor_visibility(False)),
            ("toggle", "選択の表示を反転", self.toggle_selected_editor_visibility),
        ):
            button = ttk.Button(visibility_row, text=caption, command=callback, state="disabled")
            button.pack(side="left", fill="x", expand=True, padx=2)
            self.visibility_buttons[key] = button
        scroll_row = ttk.Frame(right, padding=(5, 3))
        scroll_row.pack(fill="x")
        ttk.Button(scroll_row, text="スクロールグループを作成 / 管理…",
                   command=self.edit_scroll_groups).pack(side="left", fill="x", expand=True)
        ttk.Button(scroll_row, text="スクロール位置をリセット",
                   command=self.reset_scroll_offsets).pack(side="left", padx=(6,0))
        scroll_create_row = ttk.Frame(right, padding=(5, 1))
        scroll_create_row.pack(fill="x")
        self.scroll_create_button = ttk.Button(scroll_create_row, text="ドラッグでスクロール範囲を新規作成",
                                               command=self.toggle_scroll_create_mode)
        self.scroll_create_button.pack(side="left", fill="x", expand=True)
        ttk.Button(scroll_create_row, text="所属アセットを別画面で配置…",
                   command=self.open_scroll_workspace).pack(side="left", fill="x", expand=True, padx=(7,0))
        asset_panel = ttk.Frame(right)
        asset_panel.pack(fill="x", padx=5)
        self.tree = ttk.Treeview(asset_panel, columns=("visible", "category", "source"), show="tree headings", height=5, selectmode="extended")
        self.tree.heading("#0", text="アセット名")
        self.tree.heading("visible", text="編集")
        self.tree.column("visible", width=43, stretch=False, anchor="center")
        self.tree.heading("category", text="種類")
        self.tree.heading("source", text="ファイル")
        self.tree.column("#0", width=135, stretch=True)
        self.tree.column("category", width=68, stretch=False)
        self.tree.column("source", width=125, stretch=True)
        self.tree.pack(side="left", fill="x", expand=True)
        scrollbar = ttk.Scrollbar(asset_panel, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self.tree_select)
        self.tree.bind("<Delete>", lambda event: self.delete_selected())
        self.tree.bind("<Escape>", lambda event: self.select(None))
        self.tree.bind("<Double-1>", self._tree_double_click)
        self.tree.bind("<Button-3>", self._tree_context)
        self.tree.bind("<ButtonPress-1>", self._asset_row_press, add="+")
        self.tree.bind("<B1-Motion>", self._asset_row_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._asset_row_release, add="+")
        layer_row = ttk.Frame(right, padding=(5, 4))
        layer_row.pack(fill="x")
        self.layer_buttons: dict[str, ttk.Button] = {}
        for key, caption, action in (
            ("up", "▲ 前へ", lambda: self.shift_selected_layer(-1)),
            ("down", "▼ 後ろへ", lambda: self.shift_selected_layer(1)),
            ("top", "最前面", lambda: self.move_layer(True)),
            ("bottom", "最背面", lambda: self.move_layer(False)),
        ):
            control = ttk.Button(layer_row, text=caption, command=action, state="disabled")
            control.pack(side="left", fill="x", expand=True, padx=2)
            self.layer_buttons[key] = control
        ttk.Label(right, text="選択アセットのプロパティ", padding=(5, 8)).pack(anchor="w")
        props = ttk.Frame(right, padding=(7, 0))
        props.pack(fill="x")
        self.vars = {name: tk.StringVar() for name in ("name", "px", "py", "sx", "sy", "degrees")}
        self.flags = {name: tk.BooleanVar(value=False) for name in ("critical", "prefetch")}
        # Keep each logical pair on one horizontal row (X/Y and scale X/Y).
        # The name and angle occupy compact full-width rows instead of
        # shifting Y onto the next row beside an unrelated scale field.
        ttk.Label(props, text="名前").grid(row=0, column=0, sticky="w", pady=(4, 2))
        ttk.Entry(props, textvariable=self.vars["name"], width=18).grid(
            row=0, column=1, sticky="ew", padx=(0, 12), pady=(4, 2))
        for label_row, left_field, right_field in (
            (1, ("px", "X（px）"), ("py", "Y（px）")),
            (3, ("sx", "倍率 X"), ("sy", "倍率 Y")),
        ):
            for column, (key, label) in enumerate((left_field, right_field)):
                wheel_label = ttk.Label(props, text=f"{label} ◉", cursor="arrow")
                wheel_label.grid(row=label_row, column=column, sticky="w", padx=(0, 10), pady=(4, 1))
                wheel_entry = ttk.Entry(props, textvariable=self.vars[key], width=18)
                wheel_entry.grid(row=label_row + 1, column=column, sticky="ew", padx=(0, 12))
                for widget in (wheel_label, wheel_entry):
                    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                        widget.bind(sequence, lambda e, n=key: self._property_wheel(e, n))
        angle_label = ttk.Label(props, text="回転角（度） ◉", cursor="arrow")
        angle_label.grid(row=5, column=0, sticky="w", pady=(5, 2))
        angle_entry = ttk.Entry(props, textvariable=self.vars["degrees"], width=18)
        angle_entry.grid(row=5, column=1, sticky="ew", padx=(0, 12), pady=(5, 2))
        for widget in (angle_label, angle_entry):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(sequence, lambda e: self._property_wheel(e, "degrees"))
        props.columnconfigure(0, weight=1)
        props.columnconfigure(1, weight=1)
        check = ttk.Frame(right, padding=(7, 9))
        check.pack(fill="x")
        ttk.Checkbutton(check, text="critical", variable=self.flags["critical"], command=self.apply_flags).pack(side="left")
        ttk.Checkbutton(check, text="prefetch", variable=self.flags["prefetch"], command=self.apply_flags).pack(side="left", padx=(20, 0))
        row = ttk.Frame(right, padding=(6, 2))
        row.pack(fill="x")
        ttk.Button(row, text="プロパティを反映", command=self.apply_properties).pack(side="left", padx=2)
        ttk.Button(row, text="複製", command=self.duplicate_selected).pack(side="left", padx=2)
        ttk.Button(row, text="削除", command=self.delete_selected).pack(side="left", padx=2)
        self.uv_edit_button = ttk.Button(row, text="UV範囲を編集", command=self.edit_selected_uv)
        self.uv_edit_button.pack(side="left", padx=(10, 2))
        self.window_edit_button = ttk.Button(row, text="ウィンドウ設定", command=self.edit_selected_window)
        self.window_edit_button.pack(side="left", padx=(2, 0))
        sound_row = ttk.Frame(right, padding=(7, 3))
        sound_row.pack(fill="x")
        ttk.Label(sound_row, text="アニメーション / ボタンSE:").pack(side="left")
        self.animation_se_var = tk.StringVar(value="なし")
        self.animation_se_combo = ttk.Combobox(sound_row, textvariable=self.animation_se_var,
                                               state="disabled", width=24, values=["なし"])
        self.animation_se_combo.pack(side="left", fill="x", expand=True, padx=7)
        self.animation_se_combo.bind("<<ComboboxSelected>>", self.apply_animation_se)
        toggle_row = ttk.Frame(right, padding=(7, 3))
        toggle_row.pack(fill="x")
        ttk.Label(toggle_row, text="トグル初期状態:").pack(side="left")
        self.toggle_default_var = tk.StringVar(value="off")
        self.toggle_default_combo = ttk.Combobox(toggle_row, state="disabled", width=12,
                                                  values=("off", "on"), textvariable=self.toggle_default_var)
        self.toggle_default_combo.pack(side="left", padx=7)
        self.toggle_default_combo.bind("<<ComboboxSelected>>", self.apply_toggle_default)
        ttk.Label(right, text="アセット一覧をドラッグすると重なり順を変更できます。",
                  foreground="#6c7682", padding=(7, 4)).pack(anchor="w")
        ttk.Label(self, textvariable=self.status_text, padding=(10, 5), anchor="w").pack(fill="x")

    def _dialog_initial_dir(self, kind: str) -> str:
        """Scene opens default to ./out/scene; assets default to ./assets."""
        if kind == "scene":
            return str(SCENE_OUTPUT_DIR)
        # Start each asset category in assets/ unless that category has
        # a previously selected subfolder (never borrow another type's folder).
        candidates = [self._last_asset_kind_dirs.get(kind), ASSET_ROOT]
        for candidate in candidates:
            if candidate is None or not candidate.is_dir():
                continue
            if kind != "scene":
                try:
                    ensure_asset_path(candidate / "__directory_probe__")
                except ValueError:
                    continue
            return str(candidate)
        return str(ASSET_ROOT)

    def _remember_dialog_file(self, kind: str, filename: Path) -> None:
        folder = filename.expanduser().resolve().parent
        if kind == "scene":
            return  # Opening another scene never changes the fixed scene directory.
        ensure_asset_path(filename)
        if folder.is_dir():
            self._last_asset_kind_dirs[kind] = folder
            self._last_asset_dir = folder

    def _remember_loaded_asset_dirs(self, loaded_assets: list[Asset],
                                    background_path: Optional[Path],
                                    audio_data: dict[str, dict[str, dict[str, str]]]) -> None:
        """Seed asset directories from a scene without ever escaping ./assets."""
        entries: list[tuple[str, Path]] = [(asset.kind, asset.source)
                                          for asset in loaded_assets]
        entries.extend(("button_uv", asset.pressed_source) for asset in loaded_assets
                       if asset.pressed_source is not None)
        if background_path is not None:
            entries.append(("background", background_path))
        for audio_records in audio_data.values():
            for record in audio_records.values():
                entries.append(("audio", Path(record["file"])))
        for kind, path in entries:
            validated = ensure_asset_path(path)
            if validated.parent.is_dir():
                self._last_asset_kind_dirs[kind] = validated.parent
                self._last_asset_dir = validated.parent

    def get_selected(self) -> Optional[Asset]:
        return next((a for a in self.assets if a.uid == self.selected_uid), None)

    @staticmethod
    def _asset_state_copy(asset: Asset) -> Asset:
        copied = copy.copy(asset)
        copied.events = [dict(item) for item in asset.events]
        copied.raster_cache = {}
        return copied

    def _snapshot(self) -> dict[str, Any]:
        """Lightweight snapshot: share immutable decoded textures and animation models."""
        return {
            "assets": [self._asset_state_copy(a) for a in self.assets],
            "scroll_groups": copy.deepcopy(self.scroll_groups),
            "selected_uid": self.selected_uid,
            "selected_uids": set(self.selected_uids),
            "editor_hidden_uids": set(self.editor_hidden_uids),
            "scene": self.scene_name.get(),
            "background_file": self.background_file,
            "background_image": self.background_image,
            "background_critical": self.background_critical,
            "background_prefetch": self.background_prefetch,
            "audio": copy.deepcopy(self.audio),
            "audio_flags": dict(self.audio_flags),
        }

    def _push_undo(self, state: Optional[dict[str, Any]] = None) -> None:
        self._undo_stack.append(state if state is not None else self._snapshot())
        if len(self._undo_stack) > UNDO_MAX_STEPS:
            del self._undo_stack[0]

    def undo(self, _event: Any = None) -> str:
        """Undo a completed editing action, not each intermediate mouse-move event."""
        if self._drag is not None:
            # Complete the drag first; it creates exactly one undo record.
            self.editor_release(None)
        if not self._undo_stack:
            self.status_text.set("取り消せる編集操作はありません。")
            return "break"
        state = self._undo_stack.pop()
        self.stop_animations()
        self.audio_player.stop()
        # Re-copy the records so future edits cannot mutate a saved snapshot.
        self.assets = [self._asset_state_copy(a) for a in state["assets"]]
        self.scroll_groups = copy.deepcopy(state.get("scroll_groups", {}))
        workspace=self._scroll_workspace
        if workspace is not None and workspace.winfo_exists():
            if workspace.name not in self.scroll_groups:
                workspace.close()
            else:
                workspace._photo_cache.clear()
                workspace._photo_pixels=0
                workspace._refresh_members()
        if self.selected_scroll_group_name not in self.scroll_groups:
            self.selected_scroll_group_name = None
        self.scroll_offsets.clear()
        self._scroll_press = None
        self.selected_uid = state["selected_uid"]
        self.selected_uids = set(state.get("selected_uids", {self.selected_uid} if self.selected_uid else set()))
        self.editor_hidden_uids = set(state["editor_hidden_uids"])
        self.scene_name.set(state["scene"])
        self.background_file = state["background_file"]
        self.background_image = state["background_image"]
        self.background_critical = state["background_critical"]
        self.background_prefetch = state["background_prefetch"]
        self.bg_critical_var.set(self.background_critical)
        self.bg_prefetch_var.set(self.background_prefetch)
        self.audio = copy.deepcopy(state["audio"])
        self.audio_flags = dict(state["audio_flags"])
        self._background_cache.clear()
        self._reset_toggle_states()
        self._refresh_event_windows()
        self.refresh_tree()
        self.select(self.selected_uid, selection=self.selected_uids)
        if self._audio_refresh is not None:
            self._audio_refresh()
        self.request_draw("both")
        self.status_text.set("直前の編集操作を取り消しました。")
        return "break"

    def _asset_by_uid(self, uid: str) -> Optional[Asset]:
        return next((item for item in self.assets if item.uid == uid), None)

    def _active_scroll_groups(self, visible_uids: set[str]) -> set[str]:
        """Groups without show events are always visible; referenced ones are ON-gated.

        Several toggles may reference one viewport (logical OR).  Only active,
        visible toggle owners can enable their group.  No asset duplication.
        """
        references: dict[str, set[str]] = {}
        for owner in self.assets:
            if owner.kind != "toggle_uv":
                continue
            if self.toggle_states.get(owner.uid, owner.toggle_default) != "on":
                # An OFF owner's reference still makes the group event-controlled.
                for event in owner.events:
                    if event["type"] == "show_scroll_group":
                        references.setdefault(event["group_name"], set())
                continue
            for event in owner.events:
                if event["type"] == "show_scroll_group":
                    references.setdefault(event["group_name"], set()).add(owner.uid)
        button_gated = {e["group_name"] for owner in self.assets
                        if owner.kind in ("button", "button_uv")
                        for e in owner.events if e["type"] in ("show_scroll_group", "toggle_scroll_group")}
        unconditional = set(self.scroll_groups) - set(references) - button_gated
        enabled = set(self.scroll_groups)
        # An event trigger can itself be in *another* gated scroll group.
        # Iterate until a hidden trigger cannot keep a different group visible.
        for _ in range(len(self.scroll_groups) + 1):
            accessible = set(visible_uids)
            for group in self.scroll_groups.values():
                if group.name not in enabled:
                    accessible.difference_update(group.members)
            next_enabled = unconditional | {
                name for name, owners in references.items()
                if any(uid in accessible for uid in owners)
            }
            # Button events are latched on click; the most recent button command
            # for this group overrides its initial toggle/button visibility.
            next_enabled = {name for name in next_enabled
                            if self.button_scroll_states.get(name, True)}
            next_enabled.update(name for name, value in self.button_scroll_states.items()
                                if value and name in self.scroll_groups)
            if next_enabled == enabled:
                break
            enabled = next_enabled
        return enabled

    def _preview_visible_uids(self) -> set[str]:
        """Evaluate toggle events, optionally masking editor-hidden assets and owners."""
        lookup = {a.uid: a for a in self.assets}
        cached: dict[str, bool] = {}
        editor_mask = self.editor_hidden_uids if self.preview_sync_visibility.get() else set()

        def owner_chain_active(item: Asset, visiting: frozenset[str] = frozenset()) -> bool:
            if item.uid in cached:
                return cached[item.uid]
            if item.uid in editor_mask:
                cached[item.uid] = False
                return False
            owner_id = item.event_owner_uid
            if owner_id is None:
                cached[item.uid] = True
                return True
            owner = lookup.get(owner_id)
            if owner is None or owner.uid in visiting or owner.kind != "toggle_uv":
                cached[item.uid] = False
                return False
            active = (owner_chain_active(owner, visiting | {item.uid}) and
                      self.toggle_states.get(owner.uid, owner.toggle_default) == "on")
            cached[item.uid] = active
            return active

        shown = {item.uid for item in self.assets if owner_chain_active(item)}
        hidden: set[str] = set()
        for owner in self.assets:
            if (owner.kind == "toggle_uv" and owner.uid in shown and
                    self.toggle_states.get(owner.uid, owner.toggle_default) == "on"):
                for event in owner.events:
                    if event["type"] == "hide":
                        hidden.add(event["target_uid"])
        changed = True
        while changed:
            changed = False
            for item in self.assets:
                if item.event_owner_uid in hidden and item.uid not in hidden:
                    hidden.add(item.uid)
                    changed = True
        visible = shown - hidden
        active_scroll_groups = self._active_scroll_groups(visible)
        for group in self.scroll_groups.values():
            if group.name not in active_scroll_groups:
                visible.difference_update(group.members)
        return visible

    def _editor_visible_uids(self) -> set[str]:
        """Main editor only: scroll members live in their own spacious workspace.

        View filters never mutate the scene, preview, visibility flags or JSON.
        """
        result = {a.uid for a in self.assets if a.uid not in self.editor_hidden_uids}
        if self.editor_sync_preview_visibility.get():
            result.intersection_update(self._preview_visible_uids())
        for scroll_group in self.scroll_groups.values():
            result.difference_update(scroll_group.members)
        return result

    def _preview_visibility_changed(self) -> None:
        """Redraw editor too if its visible layers mirror runtime preview."""
        if self.editor_sync_preview_visibility.get():
            self._drag_static_cache = None
            self.refresh_tree()
            self.request_draw("both")
        else:
            self.request_draw("preview")

    def _close_event_windows(self) -> None:
        for window in list(self._event_windows.values()):
            if window.winfo_exists():
                window.destroy()
        self._event_windows.clear()

    def edit_button_scroll_events(self, button_uid: Optional[str] = None) -> None:
        owner = self._asset_by_uid(button_uid or self.selected_uid or "")
        if owner is None or owner.kind not in ("button", "button_uv"):
            return
        existing = self._event_windows.get(owner.uid)
        if existing is not None and existing.winfo_exists():
            existing.refresh_list()
            existing.lift()
            existing.focus_force()
            return
        window = ButtonScrollEventWindow(self, owner.uid)
        self._event_windows[owner.uid] = window

    def add_button_scroll_events(self, button_uid: str, kind: str,
                                 names: Sequence[str]) -> None:
        owner = self._asset_by_uid(button_uid)
        if owner is None or owner.kind not in ("button", "button_uv"):
            raise ValueError("通常ボタンが見つかりません。")
        if kind not in ("show_scroll_group", "hide_scroll_group", "toggle_scroll_group"):
            raise ValueError("不正なイベント操作です。")
        names = list(names)
        if not names or len(names) != len(set(names)):
            raise ValueError("重複せずにグループを選択してください。")
        if len(owner.events) + len(names) > 500:
            raise ValueError("イベントは500件以下にしてください。")
        existing = {e.get("group_name") for e in owner.events}
        for name in names:
            group = self.scroll_groups.get(name)
            if group is None or name in existing:
                raise ValueError(f"{name}: 対象がないか登録済みです。")
            if owner.uid in group.members and kind != "hide_scroll_group":
                raise ValueError(f"{name}: ボタン自身の所属グループは表示・切替できません。")
        self._push_undo()
        owner.events.extend({"type": kind, "group_name": name} for name in names)
        self._refresh_event_windows()
        self._preview_visibility_changed()
        self.status_text.set(f"{owner.name}: スクロールグループの押下イベントを{len(names)}件追加しました。")

    def remove_button_scroll_event(self, button_uid: str, index: int) -> None:
        owner = self._asset_by_uid(button_uid)
        if owner is None or owner.kind not in ("button", "button_uv") or not 0 <= index < len(owner.events):
            return
        self._push_undo()
        owner.events.pop(index)
        self._refresh_event_windows()
        self._preview_visibility_changed()

    def _run_button_scroll_events(self, owner: Asset) -> None:
        """Apply only on a completed click; no undo or JSON changes."""
        if owner.kind not in ("button", "button_uv"):
            return
        changed = False
        for event in owner.events:
            kind = event["type"]
            if kind not in ("show_scroll_group", "hide_scroll_group", "toggle_scroll_group"):
                continue
            name = event["group_name"]
            if name not in self.scroll_groups:
                continue
            if kind == "show_scroll_group":
                new_value = True
            elif kind == "hide_scroll_group":
                new_value = False
            else:
                new_value = name not in self._active_scroll_groups(self._preview_visible_uids())
            self.button_scroll_states[name] = new_value
            if not new_value:
                self.scroll_offsets.pop(name, None)
            changed = True
        if changed:
            self.status_text.set(f"ボタン {owner.name}: スクロールグループの表示を変更しました。")
            self._preview_visibility_changed()

    def edit_toggle_events(self, toggle_uid: Optional[str] = None) -> None:
        owner = self._asset_by_uid(toggle_uid or self.selected_uid or "")
        if owner is None or owner.kind != "toggle_uv":
            return
        existing = self._event_windows.get(owner.uid)
        if existing is not None and existing.winfo_exists():
            existing.refresh_list()
            existing.lift()
            existing.focus_force()
            return
        window = ToggleEventWindow(self, owner.uid)
        self._event_windows[owner.uid] = window

    def _refresh_event_windows(self) -> None:
        for uid, window in list(self._event_windows.items()):
            if self._asset_by_uid(uid) is None:
                if window.winfo_exists():
                    window.destroy()
                self._event_windows.pop(uid, None)
            elif window.winfo_exists():
                window.refresh_list()

    def eligible_existing_event_assets(self, toggle_uid: str) -> list[Asset]:
        """Root-only candidates, excluding the toggle and its existing event targets."""
        owner = self._asset_by_uid(toggle_uid)
        if owner is None or owner.kind != "toggle_uv":
            return []
        excluded = {toggle_uid}
        for entry in owner.events:
            target = entry.get("asset_uid") if entry["type"] == "show" else entry.get("target_uid")
            if target:
                excluded.add(target)
        movable_kinds = frozenset({"image", "button", "button_uv", "toggle_uv", "animation", "window"})
        return [item for item in self.assets
                if item.kind in movable_kinds
                and item.event_owner_uid is None and item.uid not in excluded]

    def invalidated_hide_references(self, moved_ids: Sequence[str]) -> dict[str, int]:
        """Count hide events which cannot target assets once they become event children."""
        moved = set(moved_ids)
        return {item.uid: sum(1 for event in item.events
                              if event["type"] == "hide" and event["target_uid"] in moved)
                for item in self.assets
                if item.kind == "toggle_uv" and any(
                    event["type"] == "hide" and event["target_uid"] in moved
                    for event in item.events)}

    def move_assets_to_toggle_event(self, toggle_uid: str,
                                    asset_uids: Sequence[str]) -> list[Asset]:
        """Transfer existing scene roots to show events atomically, preserving layers.

        UIDs, transforms, references, and nested toggle events remain untouched.
        A hide event cannot target a child in the JSON schema, so incompatible
        hide events are removed along with the move in the same undo snapshot.
        """
        owner = self._asset_by_uid(toggle_uid)
        if owner is None or owner.kind != "toggle_uv":
            raise ValueError("対象のトグルボタンが見つかりません。")
        uids = list(asset_uids)
        if not uids or len(uids) != len(set(uids)):
            raise ValueError("移動するアセットを重複なく選択してください。")
        available = {item.uid: item for item in self.eligible_existing_event_assets(toggle_uid)}
        if any(uid not in available for uid in uids):
            raise ValueError("移動できないアセットが選択されています。")
        if len(owner.events) + len(uids) > 500:
            raise ValueError("トグルイベントは500件までです。")
        # Nested toggle events may be up to 8 levels deep. The moved root's
        # own show descendants must fit under the prospective new owner.
        def depth_to_root(asset: Asset) -> int:
            depth = 0
            cursor = asset
            seen: set[str] = set()
            while cursor.event_owner_uid is not None:
                if cursor.uid in seen:
                    raise ValueError("イベントに循環参照があります。")
                seen.add(cursor.uid)
                parent = self._asset_by_uid(cursor.event_owner_uid)
                if parent is None:
                    raise ValueError("イベントの親アセットがありません。")
                cursor = parent
                depth += 1
            return depth

        def descendants_depth(asset: Asset, visiting: frozenset[str] = frozenset()) -> int:
            if asset.uid in visiting:
                raise ValueError("イベントに循環参照があります。")
            children = [item for item in self.assets if item.event_owner_uid == asset.uid]
            return max((1 + descendants_depth(child, visiting | {asset.uid})
                        for child in children), default=0)

        owner_depth = depth_to_root(owner)
        for uid in uids:
            if owner_depth + 1 + descendants_depth(available[uid]) > 8:
                raise ValueError("移動後のトグルイベントが最大ネスト8階層を超えます。")
        # Keep scroll membership when transferring an already placed asset
        # into a toggle's show event. Its UID/full_key and world transform
        # remain stable, so no additional JSON migration is required.
        self._push_undo()
        moved = [available[uid] for uid in uids]
        moved_set = set(uids)
        for item in self.assets:
            if item.kind == "toggle_uv":
                item.events = [entry for entry in item.events
                               if not (entry["type"] == "hide" and
                                       entry["target_uid"] in moved_set)]
        for asset in moved:
            # Event children cannot belong to a root-level exclusive group.
            asset.toggle_group = None
            asset.event_owner_uid = toggle_uid
            owner.events.append({"type": "show", "asset_uid": asset.uid})
        self._repair_toggle_groups()
        # The layer order is held by self.assets; do not remove/reinsert records.
        self._drag_static_cache = None
        self._refresh_event_windows()
        self.refresh_tree()
        self.request_draw("both")
        return moved

    def reorder_toggle_events(self, toggle_uid: str, order: Sequence[int]) -> None:
        """Apply a Treeview drag as a single undoable permutation of events."""
        owner = self._asset_by_uid(toggle_uid)
        if owner is None or owner.kind != "toggle_uv":
            raise ValueError("対象のトグルボタンが見つかりません。")
        indices = list(order)
        count = len(owner.events)
        if len(indices) != count or sorted(indices) != list(range(count)):
            raise ValueError("イベントの並べ替え情報が不正です。")
        if indices == list(range(count)):
            return
        self._push_undo()
        existing = owner.events
        owner.events = [existing[index] for index in indices]
        self._refresh_event_windows()
        self.status_text.set("イベントの順序を変更しました。Ctrl+Zで元に戻せます。")

    def add_scroll_group_show_events(self, toggle_uid: str,
                                     group_names: Sequence[str]) -> None:
        """Gate one or more existing scroll viewports behind a toggle's ON state."""
        owner = self._asset_by_uid(toggle_uid)
        if owner is None or owner.kind != "toggle_uv":
            raise ValueError("対象のトグルボタンが見つかりません。")
        names = list(group_names)
        if not names or len(names) != len(set(names)):
            raise ValueError("追加するグループを重複なく選択してください。")
        existing = {e["group_name"] for e in owner.events
                    if e["type"] == "show_scroll_group"}
        for name in names:
            group = self.scroll_groups.get(name)
            if group is None:
                raise ValueError(f"スクロールグループ '{name}' が見つかりません。")
            if name in existing:
                raise ValueError(f"{name} は既にイベントへ登録されています。")
            if owner.uid in group.members:
                raise ValueError(f"{name}: トグル自身が属するグループは制御できません。")
        if len(owner.events) + len(names) > 500:
            raise ValueError("トグルイベントは500件以下にしてください。")
        self._push_undo()
        for name in names:
            owner.events.append({"type": "show_scroll_group", "group_name": name})
        self._drag_static_cache = None
        self._refresh_event_windows()
        self._preview_visibility_changed()
        self.status_text.set(f"スクロールグループ {len(names)} 件をON時の表示イベントへ追加しました。")

    def add_hide_event(self, toggle_uid: str, target_uid: str) -> None:
        owner = self._asset_by_uid(toggle_uid)
        target = self._asset_by_uid(target_uid)
        if owner is None or owner.kind != "toggle_uv" or target is None:
            raise ValueError("対象のトグルボタンまたは非表示アセットが見つかりません。")
        if (target.uid == owner.uid or target.event_owner_uid is not None):
            raise ValueError("別の通常配置アセットを選択してください。")
        if any(e["type"] == "hide" and e["target_uid"] == target_uid for e in owner.events):
            raise ValueError("このアセットは既に非表示イベントへ登録されています。")
        self._push_undo()
        owner.events.append({"type": "hide", "target_uid": target_uid})
        self._drag_static_cache = None
        self._refresh_event_windows()
        self._preview_visibility_changed()

    def restore_toggle_event_asset(self, owner_uid: str, index: int) -> Asset:
        """Make a show-event asset a scene root, with one undoable operation.

        Preserve its UID, transform, layers and any nested toggle events.
        Hide events only reference existing root assets and cannot be restored.
        """
        owner = self._asset_by_uid(owner_uid)
        if owner is None or owner.kind != "toggle_uv":
            raise ValueError("対象のトグルボタンが見つかりません。")
        if not 0 <= index < len(owner.events):
            raise ValueError("イベントを選択してください。")
        event = owner.events[index]
        if event["type"] != "show":
            raise ValueError("『アセットの追加表示』イベントだけを通常アセットへ戻せます。")
        asset = self._asset_by_uid(event["asset_uid"])
        if asset is None or asset.event_owner_uid != owner.uid:
            raise ValueError("イベントに紐づくアセットが見つかりません。")

        # Root entries must have unique group-qualified names for JSON output.
        # Event-local assets can otherwise have names that collide with roots.
        root_keys = {other.full_key for other in self.assets
                     if other.event_owner_uid is None and other.uid != asset.uid}
        restored_name = asset.name
        if asset.full_key in root_keys:
            suffix = 2
            while True:
                candidate = f"{asset.name}_{suffix}"
                original_name = asset.name
                asset.name = candidate
                key_available = asset.full_key not in root_keys
                asset.name = original_name
                if key_available:
                    restored_name = candidate
                    break
                suffix += 1

        self._push_undo()
        owner.events.pop(index)
        asset.event_owner_uid = None
        asset.event_layer = None
        asset.name = restored_name
        # self.assets already carries the absolute stack order; retain its slot.
        self._drag_static_cache = None
        self._refresh_event_windows()
        self.refresh_tree()
        self.request_draw("both")
        return asset

    def remove_toggle_event(self, owner_uid: str, index: int) -> None:
        owner = self._asset_by_uid(owner_uid)
        if owner is None or not 0 <= index < len(owner.events):
            return
        self._push_undo()
        event = owner.events.pop(index)
        if event["type"] == "show":
            self._remove_asset_tree(event["asset_uid"])
        self._repair_toggle_groups()
        self._drag_static_cache = None
        self._refresh_event_windows()
        self.refresh_tree()
        self.request_draw("both")

    def _remove_asset_tree(self, asset_uid: str) -> None:
        """Remove a main/event asset and all nested show children and hide references."""
        removal = {asset_uid}
        changed = True
        while changed:
            changed = False
            for item in self.assets:
                if item.event_owner_uid in removal and item.uid not in removal:
                    removal.add(item.uid)
                    changed = True
        self.assets = [a for a in self.assets if a.uid not in removal]
        for group in self.scroll_groups.values():
            group.members.difference_update(removal)
        self.editor_hidden_uids.difference_update(removal)
        for item in self.assets:
            item.events = [dict(e) for e in item.events if
                           e.get("asset_uid") not in removal and e.get("target_uid") not in removal]
        for uid in removal:
            self.toggle_states.pop(uid, None)
            self.playing.pop(uid, None)
            self.last_frame.pop(uid, None)
            if self.pressed_button_uid == uid:
                self.pressed_button_uid = None

    def _unique_asset_name(self, original: Asset) -> str:
        existing = {a.name for a in self.assets if a.category == original.category}
        number = 2
        while f"{original.name}_{number}" in existing:
            number += 1
        return f"{original.name}_{number}"

    def _clone_event_descendants(self, source: Asset, target: Asset,
                                 templates: dict[str, Asset], offset: float,
                                 y_offset: Optional[float] = None) -> None:
        """Copy a toggle's event tree, remapping stable UIDs to fresh ones."""
        if y_offset is None:
            y_offset = -offset  # Historical paste/duplicate offset.
        target.events = []
        for old_event in source.events:
            if old_event["type"] in ("hide", "show_scroll_group", "hide_scroll_group", "toggle_scroll_group"):
                target.events.append(dict(old_event))
            elif old_event["type"] == "show":
                old_child = templates.get(old_event["asset_uid"])
                if old_child is None:
                    continue
                if len(self.assets) >= MAX_ASSETS:
                    raise ValueError("アセット数が上限に達しました。")
                child = self._asset_state_copy(old_child)
                child.uid = uuid.uuid4().hex
                child.event_owner_uid = target.uid
                child.toggle_group = None
                child.name = self._unique_asset_name(child)
                child.px += offset
                child.py += y_offset
                self.assets.append(child)
                if child.kind == "toggle_uv":
                    self.toggle_states[child.uid] = child.toggle_default
                target.events.append({"type": "show", "asset_uid": child.uid})
                self._clone_event_descendants(old_child, child, templates, offset, y_offset)

    def _reset_toggle_states(self) -> None:
        """Restore saved initial states and clear preview-only button scroll commands."""
        self.toggle_states = {asset.uid: asset.toggle_default for asset in self.assets
                              if asset.kind == "toggle_uv"}
        self.button_scroll_states.clear()

    def apply_toggle_default(self, _event: Any = None) -> None:
        asset = self.get_selected()
        if asset is None or asset.kind != "toggle_uv" or self._property_updating:
            return
        new_state = self.toggle_default_var.get()
        if new_state not in ("off", "on"):
            return
        if asset.toggle_group:
            if new_state == "off":
                # An exclusive group must always contain exactly one ON member.
                self.toggle_default_var.set(asset.toggle_default)
                return
            members = [a for a in self.assets if a.toggle_group == asset.toggle_group]
            if asset.toggle_default != "on":
                self._push_undo()
                for member in members:
                    member.toggle_default = "on" if member.uid == asset.uid else "off"
                    self.toggle_states[member.uid] = member.toggle_default
                self.request_draw("both")
            return
        if asset.toggle_default != new_state:
            self._push_undo()
            asset.toggle_default = new_state
            self.toggle_states[asset.uid] = new_state
            self._preview_visibility_changed()

    def _startup_draw(self) -> None:
        self._startup_draw_job = None
        self.request_draw("both")

    def request_draw(self, which: str = "both") -> None:
        if self._needs_draw is None:
            self._needs_draw = which
            self._draw_job = self.after(12, self._flush_draw)
        elif self._needs_draw != which:
            self._needs_draw = "both"

    def _flush_draw(self) -> None:
        self._draw_job = None
        which = self._needs_draw
        self._needs_draw = None
        if which in ("edit", "both") and self._drag is None:
            self.draw_canvas("edit")
        if which in ("preview", "both"):
            self.draw_canvas("preview")

    def viewport(self, canvas: tk.Canvas) -> tuple[float, float, float, float, float]:
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        content_w = max(1, min(width - 12, (height - 12) * ASPECT))
        content_h = content_w / ASPECT
        start_x = (width - content_w) / 2
        start_y = (height - content_h) / 2
        return (start_x, start_y, content_w, content_h, content_w / DESIGN_W)

    def world_to_screen(self, key: str, x: float, y: float) -> tuple[float, float]:
        vx, vy, w, h, scale = self.viewports[key]
        return (vx + w / 2 + x * scale, vy + h / 2 - y * scale)

    def screen_to_world(self, key: str, x: float, y: float) -> tuple[float, float]:
        vx, vy, w, h, scale = self.viewports[key]
        return ((x - vx - w / 2) / scale, -(y - vy - h / 2) / scale)

    def polygon(self, asset: Asset, key: str) -> list[tuple[float, float]]:
        x0, y0, x1, y1 = asset.bounds
        return [self.world_to_screen(key, *transform_local(asset, x, y)) for x, y in
                ((x0, y1), (x1, y1), (x1, y0), (x0, y0))]

    def render_composite(self, width: int, height: int, scale: float,
                         preview: bool, skip_uid: Optional[str] = None,
                         assets_override: Optional[list[Asset]] = None,
                         transparent: bool = False) -> Image.Image:
        output = Image.new("RGBA", (width, height),
                           (0, 0, 0, 0) if transparent else BG_COLOR)
        if not transparent and self.background_image is not None:
            cache_key = (id(self.background_image), width, height)
            background = self._background_cache.get(cache_key)
            if background is None:
                background = ImageOps.fit(self.background_image, (width, height),
                                          method=RESAMPLE, centering=(0.5, 0.5))
                if len(self._background_cache) >= 4:
                    self._background_cache.clear()
                self._background_cache[cache_key] = background
            output.alpha_composite(background)
        shown = self._preview_visible_uids() if preview else self._editor_visible_uids()
        for asset in self.assets if assets_override is None else assets_override:
            if asset.uid not in shown:
                continue
            if asset.uid == skip_uid:
                continue
            frame = self.last_frame.get(asset.uid, 0) if preview else 0
            try:
                raster, density = asset_bitmap(asset, frame, scale)
                x0, y0, x1, y1 = asset.bounds
                # Held visual is preview-only. Center and scale its crop independently;
                # the regular hit region stays stable even if UV sizes differ.
                if (asset.kind == "toggle_uv" and
                        (self.toggle_states.get(asset.uid, asset.toggle_default)
                         if preview or self.editor_sync_preview_visibility.get()
                         else asset.toggle_default) == "on" and
                        asset.pressed_image is not None):
                    raster = asset.pressed_image
                    intrinsic_w, intrinsic_h = raster.size
                elif (preview and asset.uid == self.pressed_button_uid
                        and getattr(self, "_button_pointer_inside", True)
                        and asset.kind in ("button_uv", "button")
                        and asset.pressed_image is not None):
                    raster = asset.pressed_image
                    intrinsic_w, intrinsic_h = raster.size
                else:
                    intrinsic_w, intrinsic_h = x1 - x0, y1 - y0
                if asset.kind == "window":
                    intrinsic_w, intrinsic_h = raster.size
                    target_w = max(1, min(8192, round(intrinsic_w * scale)))
                    target_h = max(1, min(8192, round(intrinsic_h * scale)))
                else:
                    target_w = max(1, min(8192, round(intrinsic_w * scale * asset.sx)))
                    target_h = max(1, min(8192, round(intrinsic_h * scale * asset.sy)))
                if target_w * target_h > MAX_RENDER_PIXELS:
                    self.status_text.set(f"{asset.name}: 描画サイズの上限を超えています。")
                    continue
                sampler = RESAMPLE_NEAREST if asset.kind == "window" else RESAMPLE
                resized = raster.resize((target_w, target_h), sampler)
                resized = apply_scene_flips(resized, asset)
                rotated = resized.rotate(math.degrees(asset.rotation), expand=True, resample=sampler)
                mx, my = (x0 + x1) / 2, (y0 + y1) / 2
                wx, wy = transform_local(asset, mx, my)
                scroll_group = self._scroll_group_of(asset.uid) if preview else None
                if scroll_group is not None:
                    dx,dy=self.scroll_offsets.get(scroll_group.name,(0.0,0.0))
                    wx+=dx
                    wy+=dy
                sx = width / 2 + wx * scale
                sy = height / 2 - wy * scale
                left = round(sx - rotated.width / 2)
                top = round(sy - rotated.height / 2)
                # Clip to viewport before compositing; avoids version-dependent Pillow bounds errors.
                ix0, iy0 = max(0, left), max(0, top)
                ix1, iy1 = min(width, left + rotated.width), min(height, top + rotated.height)
                if scroll_group is not None:
                    gx0,gy0,gx1,gy1=scroll_group.bounds
                    # World viewport -> screen image pixels; Y is up in world space.
                    ix0=max(ix0,math.ceil(width/2+gx0*scale))
                    ix1=min(ix1,math.floor(width/2+gx1*scale))
                    iy0=max(iy0,math.ceil(height/2-gy1*scale))
                    iy1=min(iy1,math.floor(height/2-gy0*scale))
                if ix1 > ix0 and iy1 > iy0:
                    part = rotated.crop((ix0 - left, iy0 - top, ix1 - left, iy1 - top))
                    output.alpha_composite(part, (ix0, iy0))
            except (OSError, ValueError, MemoryError, ZeroDivisionError) as exc:
                self.status_text.set(f"描画エラー ({asset.name}): {exc}")
        return output

    def draw_canvas(self, key: str) -> None:
        canvas = self.edit_canvas if key == "edit" else self.preview_canvas
        viewport = self.viewport(canvas)
        self.viewports[key] = viewport
        vx, vy, content_w, content_h, scale = viewport
        canvas.delete("all")
        width, height = max(1, round(content_w)), max(1, round(content_h))
        try:
            image = self.render_composite(width, height, scale, key == "preview")
            photo = ImageTk.PhotoImage(image)
        except (OSError, ValueError, MemoryError) as exc:
            self.status_text.set(f"描画できません: {exc}")
            return
        if key == "edit":
            self.edit_photo = photo
        else:
            self.preview_photo = photo
        canvas.create_image(vx, vy, image=photo, anchor="nw")
        canvas.create_rectangle(vx, vy, vx + content_w, vy + content_h, outline="#566173")
        shapes = {}
        shown = self._preview_visible_uids() if key == "preview" else self._editor_visible_uids()
        for asset in self.assets:
            if asset.uid not in shown:
                continue
            polygon = self.polygon(asset, key)
            group=self._scroll_group_of(asset.uid) if key=="preview" else None
            if group is not None:
                dx,dy=self.scroll_offsets.get(group.name,(0.0,0.0))
                polygon=[(x+dx*scale,y-dy*scale) for x,y in polygon]
            shapes[asset.uid] = polygon
            if key == "edit":
                border = SELECTION_COLOR if asset.uid in self.selected_uids else OUTLINE_COLOR
                canvas.create_polygon(*sum(([x, y] for x, y in polygon), []),
                                      outline=border, fill="", width=2 if asset.uid in self.selected_uids else 1)
                if asset.uid == self.selected_uid and len(self.selected_uids) == 1:
                    self._draw_handles(canvas, asset, key)
        if key == "edit":
            self._draw_scroll_group_overlays()
            self.editor_shapes = shapes
        else:
            self.preview_shapes = shapes

    def _handles(self, asset: Asset, key: str) -> dict[str, tuple[float, float]]:
        x0, y0, x1, y1 = asset.bounds
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        local = {
            "nw": (x0, y1), "n": (mx, y1), "ne": (x1, y1),
            "e": (x1, my), "se": (x1, y0), "s": (mx, y0),
            "sw": (x0, y0), "w": (x0, my),
        }
        result = {label: self.world_to_screen(key, *transform_local(asset, x, y))
                  for label, (x, y) in local.items()}
        scale = self.viewports[key][4]
        above = ROTATION_HANDLE_DISTANCE / max(scale * asset.sy, 0.01)
        result["rotate"] = self.world_to_screen(key, *transform_local(asset, mx, y1 + above))
        return result

    def _draw_handles(self, canvas: tk.Canvas, asset: Asset, key: str, tags: str = "") -> None:
        handles = self._handles(asset, key)
        top = handles["n"]
        rotate = handles["rotate"]
        canvas.create_line(top[0], top[1], rotate[0], rotate[1], fill=SELECTION_COLOR, width=2, tags=tags)
        for label, (x, y) in handles.items():
            if label == "rotate":
                canvas.create_oval(x - 6, y - 6, x + 6, y + 6,
                                   outline=SELECTION_COLOR, fill="#253245", width=2, tags=tags)
            else:
                canvas.create_rectangle(x - HANDLE_RADIUS, y - HANDLE_RADIUS,
                                        x + HANDLE_RADIUS, y + HANDLE_RADIUS,
                                        outline="#253245", fill=SELECTION_COLOR, tags=tags)

    def refresh_tree(self) -> None:
        previous = set(self.selected_uids)
        scroll = self.tree.yview()
        self._syncing_tree_selection = True
        try:
            for child in self.tree.get_children():
                self.tree.delete(child)
            preview_shown = (self._preview_visible_uids()
                             if self.editor_sync_preview_visibility.get() else None)
            for a in reversed(self.assets):
                owner = self._asset_by_uid(a.event_owner_uid) if a.event_owner_uid else None
                label = f"↳ {a.name} [追加表示: {owner.name if owner else '不明'}]" if owner else a.name
                if a.toggle_group:
                    label += f" [排他: {a.toggle_group}]"
                scroll_group=self._scroll_group_of(a.uid)
                if scroll_group is not None:
                    label += f" [スクロール: {scroll_group.name}]"
                manual_hidden = a.uid in self.editor_hidden_uids
                preview_hidden = preview_shown is not None and a.uid not in preview_shown
                status = ("非表示（手動+プレビュー）" if manual_hidden and preview_hidden else
                          "非表示" if manual_hidden else
                          "非表示（プレビュー）" if preview_hidden else "表示")
                self.tree.insert("", "end", iid=a.uid, text=label,
                                 values=(status, "動作" if a.category == "animation" else "UI", a.source.name),
                                 tags=("hidden",) if manual_hidden or preview_hidden else ())
            self.tree.tag_configure("hidden", foreground="#8c939e")
            retained = [uid for uid in previous if self.tree.exists(uid)]
            if retained:
                self.tree.selection_set(retained)
        finally:
            self._syncing_tree_selection = False
        if scroll:
            self.tree.yview_moveto(scroll[0])
        self._update_layer_buttons()

    def _update_layer_buttons(self) -> None:
        """Disable unavailable layer and visibility operations."""
        if hasattr(self, "visibility_buttons"):
            selection = self.selected_uids
            any_visible = any(a.uid in selection and a.uid not in self.editor_hidden_uids
                              for a in self.assets)
            any_hidden = any(a.uid in selection and a.uid in self.editor_hidden_uids
                             for a in self.assets)
            for key, active in (("show", any_hidden), ("hide", any_visible),
                                ("toggle", bool(selection))):
                self.visibility_buttons[key].configure(state="normal" if active else "disabled")
        if not hasattr(self, "layer_buttons"):
            return
        ordered = list(reversed(self.assets))
        index = next((i for i, item in enumerate(ordered)
                      if item.uid == self.selected_uid), -1)
        count = len(ordered)
        for key, enabled in (
            ("up", index > 0), ("top", index > 0),
            ("down", 0 <= index < count - 1),
            ("bottom", 0 <= index < count - 1),
        ):
            self.layer_buttons[key].configure(state="normal" if enabled else "disabled")

    def _apply_asset_stack_order(self, top_first_uids: Sequence[str],
                                 *, moved_uid: Optional[str] = None) -> bool:
        """Commit one valid top-to-bottom permutation as one Undo operation.

        Runtime drawing uses self.assets back-to-front; the UI displays the
        reverse order. Event children stay attached to their original owners,
        and their absolute layer index is serialized separately.
        """
        uids = list(top_first_uids)
        current = [item.uid for item in reversed(self.assets)]
        if len(uids) != len(current) or len(set(uids)) != len(uids) or set(uids) != set(current):
            raise ValueError("配置アセットの並べ替え情報が不正です。")
        if uids == current:
            return False
        lookup = {item.uid: item for item in self.assets}
        self._push_undo()
        self.assets = [lookup[uid] for uid in reversed(uids)]
        self._drag_static_cache = None
        if moved_uid is not None:
            self.selected_uid = moved_uid
        self.refresh_tree()
        self._update_layer_buttons()
        self.request_draw("both")
        selected = lookup.get(moved_uid) if moved_uid is not None else self.get_selected()
        self.status_text.set(f"重なり順を変更しました: {selected.name if selected else 'アセット'}（Ctrl+Zで取り消し可能）")
        return True

    def shift_selected_layer(self, direction: int) -> None:
        """Move the selected asset one row; negative is toward the foreground."""
        if direction not in (-1, 1) or self.selected_uid is None:
            return
        uids = [item.uid for item in reversed(self.assets)]
        try:
            old_index = uids.index(self.selected_uid)
        except ValueError:
            return
        new_index = old_index + direction
        if not 0 <= new_index < len(uids):
            return
        uids.insert(new_index, uids.pop(old_index))
        self._apply_asset_stack_order(uids, moved_uid=self.selected_uid)
        self.tree.see(self.selected_uid)

    def _asset_row_press(self, event: tk.Event) -> None:
        """Record a candidate row; click and double-click remain ordinary selection."""
        self._asset_row_drag_source = None
        self._asset_row_drag_changed = False
        if event.state & (0x1 | 0x4) or len(self.selected_uids) > 1:
            return  # Preserve Ctrl/Shift multi-selection instead of reordering rows.
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return
        uid = self.tree.identify_row(event.y)
        if uid and self.tree.exists(uid):
            self._asset_row_drag_source = uid
            self._asset_row_drag_start_y = event.y

    def _asset_row_motion(self, event: tk.Event) -> None:
        source = self._asset_row_drag_source
        if source is None or not self.tree.exists(source):
            return
        if not self._asset_row_drag_changed and abs(event.y - self._asset_row_drag_start_y) < 6:
            return
        items = self.tree.get_children()
        if len(items) < 2:
            return
        if event.y < 14:
            self.tree.yview_scroll(-1, "units")
        elif event.y > self.tree.winfo_height() - 14:
            self.tree.yview_scroll(1, "units")
        target = self.tree.identify_row(event.y)
        if not target:
            target = items[0] if event.y < 0 else items[-1]
        if target == source:
            return
        old_index = self.tree.index(source)
        self.tree.move(source, "", self.tree.index(target))
        if old_index != self.tree.index(source):
            self._asset_row_drag_changed = True
            self.tree.selection_set(source)
            self.tree.focus(source)

    def _asset_row_release(self, _event: tk.Event) -> None:
        source = self._asset_row_drag_source
        changed = self._asset_row_drag_changed
        self._asset_row_drag_source = None
        self._asset_row_drag_changed = False
        if source is None or not changed:
            return
        order = list(self.tree.get_children())
        try:
            self._apply_asset_stack_order(order, moved_uid=source)
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            self.refresh_tree()
            return
        if self.tree.exists(source):
            self.tree.selection_set(source)
            self.tree.see(source)

    def _on_preview_visibility_sync_change(self) -> None:
        """Editor manual visibility -> preview (existing v39 option)."""
        self.pressed_button_uid = None
        self._button_pointer_inside = False
        self._preview_visibility_changed()
        if self.preview_sync_visibility.get():
            self.status_text.set("編集→プレビュー同期ON: 手動で隠したアセットをプレビューでも非表示にします。")
        else:
            self.status_text.set("編集→プレビュー同期OFF: プレビューは編集画面の手動非表示を参照しません。")

    def _on_editor_visibility_sync_change(self) -> None:
        """Preview toggle/event visibility -> editor, retaining manual hidden preferences."""
        self._drag_static_cache = None
        self.refresh_tree()
        self.request_draw("edit")
        if self.editor_sync_preview_visibility.get():
            self.status_text.set("プレビュー→編集同期ON: トグルイベントの表示状態を編集画面にも反映します（手動非表示は維持）。")
        else:
            self.status_text.set("プレビュー→編集同期OFF: 従来の編集画面表示に戻しました。")

    def set_editor_asset_visibility(self, uid: str, visible: bool) -> None:
        """Single-target convenience function backed by undoable batch visibility."""
        self._change_editor_visibility({uid}, visible=visible)

    def _change_editor_visibility(self, uids: set[str], *, visible: Optional[bool]) -> None:
        """Apply one undo record for an arbitrary selection of assets.

        visible=None means invert the current visibility independently for each UID.
        """
        existing = {a.uid for a in self.assets}
        targets = uids & existing
        if not targets:
            return
        before = set(self.editor_hidden_uids)
        if visible is True:
            updated = before - targets
        elif visible is False:
            updated = before | targets
        else:
            updated = before ^ targets
        if before == updated:
            return
        self._push_undo()
        self.editor_hidden_uids = updated
        self._drag_static_cache = None
        self.refresh_tree()
        self.request_draw("both" if self.preview_sync_visibility.get() else "edit")
        changed = len((before ^ updated) & targets)
        label = "表示" if visible is True else "非表示" if visible is False else "表示/非表示を反転"
        self.status_text.set(f"{changed}件のアセットの編集表示を{label}しました（Ctrl+Zで取り消し可能）。")

    def set_selected_editor_visibility(self, visible: bool) -> None:
        """Show or hide all currently selected assets in a single operation."""
        self._change_editor_visibility(set(self.selected_uids), visible=visible)

    def toggle_selected_editor_visibility(self) -> None:
        """Invert each selected asset's visibility (mixed selections supported)."""
        self._change_editor_visibility(set(self.selected_uids), visible=None)

    def _tree_double_click(self, event: tk.Event) -> Optional[str]:
        uid = self.tree.identify_row(event.y)
        if uid and self.tree.identify_column(event.x) == "#1":
            self.set_editor_asset_visibility(uid, uid in self.editor_hidden_uids)
            return "break"
        return None

    def _tree_context(self, event: tk.Event) -> None:
        uid = self.tree.identify_row(event.y)
        if not uid:
            return
        # Keep an existing multi-selection intact when opening its context menu.
        if uid in self.selected_uids:
            self.select(uid, selection=set(self.selected_uids))
        else:
            self.select(uid)
        asset = self._asset_by_uid(uid)
        if asset is None:
            return
        menu = tk.Menu(self, tearoff=False)
        if len(self.selected_uids) > 1:
            menu.add_command(label=f"選択した{len(self.selected_uids)}件を一括表示",
                             command=lambda: self.set_selected_editor_visibility(True))
            menu.add_command(label=f"選択した{len(self.selected_uids)}件を一括非表示",
                             command=lambda: self.set_selected_editor_visibility(False))
            menu.add_command(label="選択したアセットの表示を反転",
                             command=self.toggle_selected_editor_visibility)
            menu.add_separator()
        if asset.kind == "toggle_uv" and asset.toggle_group:
            menu.add_command(label="複数トグルスイッチのグループを編集",
                command=lambda group=asset.toggle_group: self.edit_exclusive_toggle_group(group))
            menu.add_separator()
        if asset.kind in ("button", "button_uv"):
            menu.add_command(label="押下時のスクロールイベント…",
                             command=lambda: self.edit_button_scroll_events(uid))
            menu.add_separator()
        elif asset.kind == "toggle_uv":
            menu.add_command(label="イベントの追加", command=lambda: self.edit_toggle_events(uid))
            menu.add_separator()
        hidden = uid in self.editor_hidden_uids
        asset = self._asset_by_uid(uid)
        if asset is not None:
            menu.add_command(label="画像の左右反転" + ("（適用中）" if asset.flip_x else ""),
                             command=lambda: self.toggle_asset_flip(uid, "x"))
            menu.add_command(label="画像の上下反転" + ("（適用中）" if asset.flip_y else ""),
                             command=lambda: self.toggle_asset_flip(uid, "y"))
            menu.add_separator()
        menu.add_command(label="編集画面で再表示" if hidden else "編集画面で非表示",
                         command=lambda: self.set_editor_asset_visibility(uid, hidden))
        menu.add_separator()
        menu.add_command(label="1つ手前へ", command=lambda: self.shift_selected_layer(-1))
        menu.add_command(label="1つ奥へ", command=lambda: self.shift_selected_layer(1))
        menu.add_command(label="最前面へ", command=lambda: self.move_layer(True))
        menu.add_command(label="最背面へ", command=lambda: self.move_layer(False))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def tree_select(self, _event: Any = None) -> None:
        if self._syncing_tree_selection:
            return
        selected = set(self.tree.selection())
        if selected != self.selected_uids:
            primary = self.tree.focus()
            if primary not in selected:
                primary = self.selected_uid if self.selected_uid in selected else next(iter(selected), None)
            self.select(primary, selection=selected)

    def select(self, uid: Optional[str], *, selection: Optional[set[str]] = None) -> None:
        """Keep canvas selection, Treeview, and single-asset inspector consistent."""
        valid_uids = {item.uid for item in self.assets}
        chosen = (set(selection) if selection is not None else ({uid} if uid else set())) & valid_uids
        if uid not in chosen:
            uid = next((a.uid for a in reversed(self.assets) if a.uid in chosen), None)
        self.selected_uids = chosen
        self.selected_uid = uid
        asset = self.get_selected()
        self._property_updating = True
        try:
            if asset:
                values = {"name": asset.name, "px": jnum(asset.px), "py": jnum(asset.py),
                          "sx": jnum(asset.sx), "sy": jnum(asset.sy),
                          "degrees": jnum(math.degrees(asset.rotation))}
                for field_name, value in values.items():
                    self.vars[field_name].set(value)
                self.flags["critical"].set(asset.critical)
                self.flags["prefetch"].set(asset.prefetch)
            else:
                for variable in self.vars.values():
                    variable.set("")
                for variable in self.flags.values():
                    variable.set(False)
            self._refresh_animation_se_choices(asset)
            self.toggle_default_combo.configure(state="readonly" if asset is not None
                                                and asset.kind == "toggle_uv" else "disabled")
            self.toggle_default_var.set(asset.toggle_default if asset is not None
                                        and asset.kind == "toggle_uv" else "off")
            self.window_edit_button.configure(state="normal" if asset is not None and asset.kind == "window" else "disabled")
            self.uv_edit_button.configure(state="normal" if asset is not None
                and asset.kind in ("image", "button_uv", "toggle_uv")
                and asset.source.suffix.casefold() in (".png", ".jpg", ".jpeg")
                else "disabled")
        finally:
            self._property_updating = False
        self._update_layer_buttons()
        self._syncing_tree_selection = True
        try:
            self.tree.selection_set([item for item in self.selected_uids if self.tree.exists(item)])
            if uid and self.tree.exists(uid):
                self.tree.focus(uid)
                self.tree.see(uid)
        finally:
            self._syncing_tree_selection = False
        if len(self.selected_uids) > 1:
            self.status_text.set(f"{len(self.selected_uids)} 個のアセットを選択中（一括移動・表示切替・削除が可能）")
        self.request_draw("edit")

    def _refresh_animation_se_choices(self, asset: Optional[Asset] = None) -> None:
        if asset is None:
            asset = self.get_selected()
        options = ["なし"] + [f"se.{name}" for name in sorted(self.audio["se"])]
        linked = None
        if asset is not None:
            linked = asset.animation_se if asset.kind == "animation" else (
                asset.button_se if asset.kind in ("button_uv", "button", "toggle_uv") else None)
        if linked and f"se.{linked}" not in options:
            options.append(f"se.{linked} (未登録)")
        self.animation_se_combo.configure(values=options,
            state="readonly" if asset is not None and asset.kind in ("animation", "button_uv", "button", "toggle_uv") else "disabled")
        self.animation_se_var.set(f"se.{linked}" if linked else "なし")

    def apply_animation_se(self, _event: Any = None) -> None:
        asset = self.get_selected()
        if asset is None or asset.kind not in ("animation", "button_uv", "button", "toggle_uv") or self._property_updating:
            return
        selected = self.animation_se_var.get()
        field_name = "animation_se" if asset.kind == "animation" else "button_se"
        if selected == "なし":
            next_se = None
        elif selected.startswith("se.") and selected[3:] in self.audio["se"]:
            next_se = selected[3:]
        else:
            messagebox.showwarning(APP_NAME, "SEをBGM / SEの管理画面で登録してください。", parent=self)
            self._refresh_animation_se_choices(asset)
            return
        if getattr(asset, field_name) != next_se:
            self._push_undo()
            setattr(asset, field_name, next_se)

    def _change_preview_volume(self, kind: str, value: str) -> None:
        """Only preview mixer state changes. No undo or scene serialization."""
        try:
            volume = max(0, min(100, round(float(value))))
        except (ValueError, TypeError):
            return
        self.preview_volume_text[kind].set(f"{volume}%")
        self.audio_player.set_volume(kind, volume)

    def play_primary_bgm(self) -> None:
        collection = self.audio["bgm"]
        if not collection:
            messagebox.showinfo(APP_NAME, "BGMが未登録です。BGM / SEから追加してください。", parent=self)
            return
        name = "main" if "main" in collection else next(iter(collection))
        self.audio_player.play("bgm", name, Path(collection[name]["file"]))

    def apply_flags(self) -> None:
        asset = self.get_selected()
        if asset and not self._property_updating:
            values = (self.flags["critical"].get(), self.flags["prefetch"].get())
            if (asset.critical, asset.prefetch) != values:
                self._push_undo()
                asset.critical, asset.prefetch = values

    def apply_properties(self) -> None:
        asset = self.get_selected()
        if asset is None:
            return
        try:
            name = safe_asset_key(self.vars["name"].get())
            if any(other is not asset and other.name == name and other.category == asset.category
                   for other in self.assets):
                raise ValueError("同じカテゴリに同名のアセットが存在します。")
            px = checked_float(self.vars["px"].get(), "X")
            py = checked_float(self.vars["py"].get(), "Y")
            sx = checked_float(self.vars["sx"].get(), "倍率 X", min_value=MIN_SCALE, max_value=MAX_SCALE)
            sy = checked_float(self.vars["sy"].get(), "倍率 Y", min_value=MIN_SCALE, max_value=MAX_SCALE)
            degrees = checked_float(self.vars["degrees"].get(), "回転角", min_value=-360_000, max_value=360_000)
            if asset.kind == "window":
                minimum_x, minimum_y = window_min_scales(asset)
                if sx < minimum_x or sy < minimum_y:
                    raise ValueError("ウィンドウの倍率が小さすぎます。四隅と中央1pxを確保してください。")
                if round(asset.window_width * sx) * round(asset.window_height * sy) > MAX_RENDER_PIXELS:
                    raise ValueError("ウィンドウの描画サイズが上限を超えています。")
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        next_values = (name, px, py, sx, sy, math.radians(degrees),
                       self.flags["critical"].get(), self.flags["prefetch"].get())
        old_values = (asset.name, asset.px, asset.py, asset.sx, asset.sy, asset.rotation,
                      asset.critical, asset.prefetch)
        if next_values != old_values:
            self._push_undo()
        (asset.name, asset.px, asset.py, asset.sx, asset.sy, asset.rotation,
         asset.critical, asset.prefetch) = next_values
        self.refresh_tree()
        self._refresh_event_windows()
        self.select(asset.uid)
        self.request_draw("both")

    def _property_wheel(self, event: tk.Event, field_name: str) -> str:
        steps = wheel_steps(event)
        if not steps or field_name not in ("px", "py", "sx", "sy", "degrees"):
            return "break"
        asset = self.get_selected()
        if asset is None:
            return "break"
        # Read the current entry so mouse-wheel adjustments respect pending
        # numeric edits. Validate first and never silently overwrite bad input.
        try:
            old = checked_float(self.vars[field_name].get(), field_name)
            step = 0.01 if field_name in ("sx", "sy") else 1.0
            value = old + steps * step
            if field_name in ("sx", "sy"):
                minimum_x, minimum_y = window_min_scales(asset)
                value = max(minimum_x if field_name == "sx" else minimum_y,
                            min(MAX_SCALE, value))
            elif field_name == "degrees":
                value = max(-360_000, min(360_000, value))
            else:
                value = max(-2**31 + 1, min(2**31 - 1, value))
            if not math.isfinite(value):
                return "break"
        except (TypeError, ValueError):
            return "break"
        if math.isclose(old, value, abs_tol=1e-10):
            return "break"
        # Apply the pending property edits and new wheel value in one undo step.
        next_values = {}
        for key in ("px", "py", "sx", "sy", "degrees"):
            raw = value if key == field_name else self.vars[key].get()
            minimum = MIN_SCALE if key in ("sx", "sy") else -NUMBER_MAX
            maximum = MAX_SCALE if key in ("sx", "sy") else NUMBER_MAX
            try:
                next_values[key] = checked_float(raw, key, min_value=minimum, max_value=maximum)
            except (ValueError, TypeError):
                return "break"
        if asset.kind == "window":
            min_x, min_y = window_min_scales(asset)
            if next_values["sx"] < min_x or next_values["sy"] < min_y:
                return "break"
            if (round(asset.window_width * next_values["sx"]) *
                    round(asset.window_height * next_values["sy"]) > MAX_RENDER_PIXELS):
                return "break"
        self._push_undo()
        asset.px = next_values["px"]
        asset.py = next_values["py"]
        asset.sx = next_values["sx"]
        asset.sy = next_values["sy"]
        asset.rotation = math.radians(next_values["degrees"])
        self.vars[field_name].set(jnum(value))
        self._drag_static_cache = None
        self.request_draw("both")
        return "break"

    def set_button_se_from_menu(self, uid: str, se_name: Optional[str]) -> None:
        """Change selected button SE from the context submenu, one undo step."""
        asset = self._asset_by_uid(uid)
        if asset is None or asset.kind not in ("button", "button_uv", "toggle_uv"):
            return
        if se_name is not None and se_name not in self.audio["se"]:
            messagebox.showerror(APP_NAME, "指定SEが登録されていません。", parent=self)
            return
        if asset.button_se == se_name:
            return
        self._push_undo()
        asset.button_se = se_name
        if asset.uid == self.selected_uid:
            self._refresh_animation_se_choices(asset)
        self.status_text.set(f"{asset.name} のSE: {se_name or 'なし'}")

    def toggle_asset_flip(self, uid: str, axis: str) -> None:
        """Undoable flip applied to every visual state without modifying source images."""
        asset = self._asset_by_uid(uid)
        if asset is None or axis not in ("x", "y"):
            return
        self._push_undo()
        if axis == "x":
            asset.flip_x = not asset.flip_x
        else:
            asset.flip_y = not asset.flip_y
        self._drag_static_cache = None
        self.request_draw("both")
        self.status_text.set(
            f"{asset.name}: {'左右' if axis == 'x' else '上下'}反転を"
            f"{'ON' if (asset.flip_x if axis == 'x' else asset.flip_y) else 'OFF'}にしました。"
        )

    def editor_context(self, event: tk.Event) -> None:
        """Show commands for the asset *under the pointer*, not a stale selection."""
        self.edit_canvas.focus_set()
        self._pending_spawn = self.screen_to_world("edit", event.x, event.y)
        target = self._hit_asset("edit", event.x, event.y) if self.selection_mode.get() == "asset" else None
        group_hit = self._scroll_group_hit(event.x, event.y, True) if self.selection_mode.get() == "scroll" else None
        if group_hit is not None and not self._scroll_create_mode:
            self.selected_scroll_group_name = group_hit[0]
            self._draw_scroll_group_overlays()
            menu = tk.Menu(self, tearoff=False)
            menu.add_command(label=f"スクロール範囲: {group_hit[0]}", state="disabled")
            menu.add_command(label="表示範囲・所属を編集…",
                             command=lambda name=group_hit[0]: self.edit_scroll_groups(name))
            menu.add_command(label="所属アセットを別画面で配置…",
                             command=lambda name=group_hit[0]: self.open_scroll_workspace(name))
            menu.add_command(label="ドラッグで新しい範囲を作成", command=self.enable_scroll_create_mode)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return
        menu = tk.Menu(self, tearoff=False)
        if target is not None:
            if target.uid in self.selected_uids:
                self.select(target.uid, selection=set(self.selected_uids))
            else:
                self.select(target.uid)
            if len(self.selected_uids) > 1:
                menu.add_command(label=f"選択した{len(self.selected_uids)}件を一括表示",
                                 command=lambda: self.set_selected_editor_visibility(True))
                menu.add_command(label=f"選択した{len(self.selected_uids)}件を一括非表示",
                                 command=lambda: self.set_selected_editor_visibility(False))
                menu.add_command(label="選択したアセットの表示を反転",
                                 command=self.toggle_selected_editor_visibility)
                menu.add_separator()
            sg=self._scroll_group_of(target.uid)
            menu.add_command(label=("スクロールグループを編集" if sg else "スクロールグループに所属…"),
                             command=lambda g=sg: self.edit_scroll_groups(g.name if g else None))
            if target.kind == "window":
                menu.add_command(label="ウィンドウ設定を編集", command=self.edit_selected_window)
            if target.kind == "image":
                menu.add_command(label="ボタンへの変更", command=self.convert_selected_to_button)
            elif target.kind in ("button_uv", "button", "toggle_uv"):
                image_menu = tk.Menu(menu, tearoff=False)
                image_menu.add_command(label="OFFを画像へ変更" if target.kind == "toggle_uv" else "押下前を画像へ変更",
                                       command=lambda: self.convert_selected_to_image("normal"))
                image_menu.add_command(label="ONを画像へ変更" if target.kind == "toggle_uv" else "押下中を画像へ変更",
                                       command=lambda: self.convert_selected_to_image("pressed"))
                menu.add_cascade(label="画像へ変更", menu=image_menu)
            if target.kind in ("button_uv", "button"):
                menu.add_command(label="トグルボタンへの変更",
                                 command=lambda uid=target.uid: self.convert_button_toggle(uid, True))
            elif target.kind == "toggle_uv":
                menu.add_command(label="ボタンへ変更",
                                 command=lambda uid=target.uid: self.convert_button_toggle(uid, False))
            if target.kind == "toggle_uv" and target.toggle_group:
                menu.add_command(label="複数トグルスイッチのグループを編集",
                                 command=lambda g=target.toggle_group: self.edit_exclusive_toggle_group(g))
            if target.kind == "toggle_uv":
                menu.add_command(label="イベントの追加",
                                 command=lambda uid=target.uid: self.edit_toggle_events(uid))
            elif target.kind in ("button", "button_uv"):
                menu.add_command(label="押下時のスクロールイベント…",
                                 command=lambda uid=target.uid: self.edit_button_scroll_events(uid))
            if target.kind in ("image", "button_uv", "toggle_uv") and target.source.suffix.casefold() in (".png", ".jpg", ".jpeg"):
                menu.add_command(label="UV範囲を編集", command=self.edit_selected_uv)
            if target.kind in ("button_uv", "button", "toggle_uv"):
                sound_menu = tk.Menu(menu, tearoff=False)
                sound_menu.add_command(label="なし", command=lambda uid=target.uid: self.set_button_se_from_menu(uid, None))
                if self.audio["se"]:
                    sound_menu.add_separator()
                    for sound_key in sorted(self.audio["se"]):
                        sound_menu.add_command(label=sound_key,
                            command=lambda uid=target.uid, name=sound_key: self.set_button_se_from_menu(uid, name))
                else:
                    sound_menu.add_command(label="（登録済みSEなし）", state="disabled")
                menu.add_cascade(label="SEの変更", menu=sound_menu)
            menu.add_separator()
            menu.add_command(label="画像の左右反転" + ("（適用中）" if target.flip_x else ""),
                             command=lambda uid=target.uid: self.toggle_asset_flip(uid, "x"))
            menu.add_command(label="画像の上下反転" + ("（適用中）" if target.flip_y else ""),
                             command=lambda uid=target.uid: self.toggle_asset_flip(uid, "y"))
            menu.add_separator()
            menu.add_command(label="編集画面で非表示",
                             command=lambda uid=target.uid: self.set_editor_asset_visibility(uid, False))
            menu.add_command(label="複製", command=self.duplicate_selected)
            menu.add_command(label="削除", command=self.delete_selected)
        else:
            menu.add_command(label="スクロールグループを作成 / 管理…", command=self.edit_scroll_groups)
            menu.add_separator()
            menu.add_command(label="複数トグルスイッチを配置（2個以上・排他）",
                             command=lambda: self.edit_exclusive_toggle_group())
            menu.add_command(label="ウィンドウの追加（9スライス / PNG / JPG）", command=lambda: self.add_asset("window"))
            menu.add_command(label="画像を配置（PNG / JPG / plist）", command=lambda: self.add_asset("image"))
            menu.add_command(label="画像をUVで配置（PNG / JPG）", command=lambda: self.add_asset("image_uv"))
            menu.add_command(label="ボタンをUVで配置（PNG / JPG）", command=lambda: self.add_asset("button_uv"))
            menu.add_command(label="トグルボタンを配置（PNG / JPG / plist）", command=lambda: self.add_asset("toggle_uv"))
            menu.add_command(label="ボタンを配置（plist）", command=lambda: self.add_asset("button"))
            menu.add_command(label="アニメーションを配置（ExportJson）", command=lambda: self.add_asset("animation"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def convert_selected_to_button(self) -> None:
        """Change image into a two-state button; dialog Cancel does not mutate anything."""
        asset = self.get_selected()
        if asset is None or asset.kind != "image":
            return
        try:
            extension = asset.source.suffix.casefold()
            if extension == ".plist":
                dialog = PlistButtonStateDialog(
                    self, asset.source, asset.key, asset.key, None, self.audio["se"])
                if dialog.result is None:
                    return
                normal_key, pressed_key, button_se = dialog.result
                self._push_undo()
                asset.kind = "button"
                asset.key = normal_key
                asset.pressed_key = pressed_key
                asset.button_se = button_se
                asset.uv_rect = None
                asset.pressed_uv_rect = None
            elif extension in (".png", ".jpg", ".jpeg"):
                dialog = ButtonUVDialog(self, asset.source, asset.uv_rect,
                                        asset.uv_rect, None, self.audio["se"])
                if dialog.result is None:
                    return
                normal, pressed, button_se, pressed_source = dialog.result
                normal_source = dialog.state_sources["normal"]
                self._push_undo()
                asset.kind = "button_uv"
                asset.source = normal_source
                asset.pressed_source = pressed_source
                asset.uv_rect = normal
                asset.pressed_uv_rect = pressed
                asset.pressed_key = None
                asset.button_se = button_se
            else:
                raise ValueError("ボタンへの変更はPNG / JPG / plist画像でのみ使用できます。")
            load_visual(asset)
            if asset.missing:
                raise ValueError(asset.missing)
        except (OSError, ValueError, TypeError, MemoryError, UnidentifiedImageError,
                json.JSONDecodeError) as exc:
            # Revert a failed conversion, including its just-created undo record.
            if self._undo_stack and asset.kind in ("button", "button_uv"):
                self.undo()
            messagebox.showerror(APP_NAME, f"ボタンへの変更に失敗しました:\n{exc}", parent=self)
            return
        self._drag_static_cache = None
        self.refresh_tree()
        self.select(asset.uid)
        self.request_draw("both")
        self.status_text.set(f"ボタンへ変更: {asset.name}")

    def convert_button_toggle(self, uid: str, to_toggle: bool) -> None:
        """Switch button type while preserving states, SE, transform and layer.

        A plist button is extracted to state PNGs because toggles use separate
        per-state file/UV records. Event deletion is confirmed and undoable.
        """
        asset = self._asset_by_uid(uid)
        if asset is None:
            return
        if to_toggle:
            if asset.kind not in ("button", "button_uv"):
                return
        elif asset.kind != "toggle_uv":
            return
        if to_toggle and any(e["type"] in ("hide_scroll_group", "toggle_scroll_group")
                             for e in asset.events):
            accepted = messagebox.askyesno(
                APP_NAME,
                "トグルボタンには『非表示』『表示／非表示の切替』イベントがありません。\n"
                "変換するとこれらのイベントは削除されます。続行しますか？",
                parent=self)
            if not accepted:
                return
        if not to_toggle and asset.events:
            accepted = messagebox.askyesno(
                APP_NAME,
                "通常ボタンへ変更すると、トグル専用のイベントと追加表示アセットが削除されます。\n"
                "非表示イベントも解除されます。続行しますか？",
                parent=self)
            if not accepted:
                return
        try:
            # Complete potentially failing texture conversion before editing the model.
            if to_toggle and asset.kind == "button":
                atlas = load_atlas(ensure_asset_path(asset.source))
                by_name = {frame.name: frame for frame in atlas.frames}
                normal_frame = by_name.get(asset.key)
                pushed_frame = by_name.get(asset.pressed_key or asset.key)
                if normal_frame is None or pushed_frame is None:
                    raise ValueError("変換元のplistスプライトが見つかりません。")
                idle_image = extract_sprite(atlas.image, normal_frame)
                on_image = extract_sprite(atlas.image, pushed_frame)
                new_source = generated_sprite_png(idle_image)
                new_pressed = generated_sprite_png(on_image)
                normal_rect = (0, 0, *idle_image.size)
                pushed_rect = (0, 0, *on_image.size)
            self._push_undo()
            if to_toggle:
                if asset.kind == "button":
                    asset.source = new_source
                    asset.pressed_source = new_pressed
                    asset.uv_rect = normal_rect
                    asset.pressed_uv_rect = pushed_rect
                    asset.key = ""
                    asset.pressed_key = None
                asset.kind = "toggle_uv"
                asset.toggle_default = "off"
                asset.events = [dict(e) for e in asset.events if e["type"] == "show_scroll_group"]
                self.toggle_states[asset.uid] = "off"
            else:
                # Children must disappear together with the event that owns them.
                for toggle_event in list(asset.events):
                    if toggle_event["type"] == "show":
                        self._remove_asset_tree(toggle_event["asset_uid"])
                asset.events.clear()
                asset.kind = "button_uv"
                asset.toggle_group = None
                asset.toggle_default = "off"
                self.toggle_states.pop(asset.uid, None)
            load_visual(asset)
            if asset.missing:
                raise ValueError(asset.missing)
        except (OSError, ValueError, TypeError, MemoryError, UnidentifiedImageError,
                json.JSONDecodeError) as exc:
            # Roll back partial type changes, events and preview toggle state.
            if self._undo_stack and 'asset' in locals() and (
                    (to_toggle and asset.kind == "toggle_uv") or
                    (not to_toggle and asset.kind == "button_uv")):
                self.undo()
            messagebox.showerror(APP_NAME, f"ボタン種別の変更に失敗しました:\n{exc}", parent=self)
            return
        self._repair_toggle_groups()
        self._drag_static_cache = None
        self._refresh_event_windows()
        self.refresh_tree()
        self.select(uid)
        self.request_draw("both")
        self.status_text.set(f"{'トグルボタン' if to_toggle else 'ボタン'}へ変更: {asset.name}")

    def convert_selected_to_image(self, state: str) -> None:
        """Keep the requested visible state, preserving position/size/rotation/flags."""
        asset = self.get_selected()
        if asset is None or asset.kind not in ("button_uv", "button", "toggle_uv"):
            return
        original_kind = asset.kind
        if state not in ("normal", "pressed"):
            raise ValueError("変換する画像の状態が不正です。")
        if asset.events:
            if not messagebox.askyesno(APP_NAME,
                    "画像へ変更すると、ボタンのイベントも削除されます。\n続行しますか？",
                    parent=self):
                return
        self._push_undo()
        if asset.events:
            for event in list(asset.events):
                if event["type"] == "show":
                    self._remove_asset_tree(event["asset_uid"])
            asset.events.clear()
            self._refresh_event_windows()
        if asset.kind in ("button_uv", "toggle_uv"):
            selected_uv = asset.uv_rect if state == "normal" else asset.pressed_uv_rect
            if selected_uv is None:
                self.undo()
                messagebox.showerror(APP_NAME, "UV範囲が設定されていません。", parent=self)
                return
            if state == "pressed" and asset.pressed_source is not None:
                asset.source = asset.pressed_source
            asset.uv_rect = selected_uv
            asset.pressed_uv_rect = None
            asset.pressed_source = None
        else:
            if state == "pressed":
                asset.key = asset.pressed_key or asset.key
            asset.pressed_key = None
        asset.kind = "image"
        asset.toggle_group = None
        self._repair_toggle_groups()
        self.toggle_states.pop(asset.uid, None)
        asset.toggle_default = "off"
        asset.button_se = None
        load_visual(asset)
        if asset.missing:
            self.undo()
            messagebox.showerror(APP_NAME, f"画像への変更に失敗しました:\n{asset.missing}", parent=self)
            return
        self._drag_static_cache = None
        self.refresh_tree()
        self.select(asset.uid)
        self.request_draw("both")
        label = ("OFF" if state == "normal" else "ON") if original_kind == "toggle_uv" else ("押下前" if state == "normal" else "押下中")
        self.status_text.set(f"画像へ変更 ({label}): {asset.name}")

    def _repair_toggle_groups(self) -> None:
        """Preserve a single ON switch after deleting or converting group members."""
        groups: dict[str, list[Asset]] = {}
        for a in self.assets:
            if a.toggle_group and (a.kind != "toggle_uv" or a.event_owner_uid is not None):
                a.toggle_group = None
            if a.toggle_group:
                groups.setdefault(a.toggle_group, []).append(a)
        for members in groups.values():
            if len(members) < 2:
                members[0].toggle_group = None
                continue
            active = next((a for a in members if a.toggle_default == "on"), members[0])
            preview_active = next((a for a in members if self.toggle_states.get(a.uid) == "on"), active)
            for a in members:
                a.toggle_default = "on" if a.uid == active.uid else "off"
                self.toggle_states[a.uid] = "on" if a.uid == preview_active.uid else "off"

    def edit_exclusive_toggle_group(self, group: Optional[str] = None) -> None:
        dialog = ExclusiveToggleGroupDialog(self, group)
        if dialog.result is None:
            return
        name, members, previous_uids, initial_index, duplicate_sources = dialog.result
        self._push_undo()
        member_uids = {a.uid for a in members}
        # Excluded existing switches become standalone toggles, never deleted.
        for asset in self.assets:
            if asset.toggle_group == group and group is not None:
                asset.toggle_group = None
        cumulative_width = sum(max(1.0, m.bounds[2] - m.bounds[0]) for m in members)
        gap = 24.0
        span = cumulative_width + gap * (len(members) - 1)
        cursor = self._pending_spawn[0] - span / 2
        for index, updated in enumerate(members):
            updated.toggle_group = name
            updated.toggle_default = "on" if index == initial_index else "off"
            actual = self._asset_by_uid(updated.uid)
            if actual is None:
                # New switches are laid out side-by-side at placement position.
                # Record the source transform before changing its position, so
                # cloned event children retain their relative arrangement.
                source_uid = duplicate_sources.get(updated.uid)
                source_asset = self._asset_by_uid(source_uid) if source_uid else None
                previous_px = source_asset.px if source_asset is not None else updated.px - 32.0
                previous_py = source_asset.py if source_asset is not None else updated.py + 32.0
                # For a duplicate of another newly added row, use that row's
                # committed descendants rather than stale references copied
                # while the dialog was open.
                source_events = source_asset if source_asset is not None else updated
                event_template = (self._asset_state_copy(source_events)
                                  if source_events.events else None)
                updated.px = cursor + (updated.bounds[2] - updated.bounds[0]) / 2
                updated.py = self._pending_spawn[1]
                self.assets.append(updated)
                if event_template is not None:
                    templates = {a.uid: a for a in self.assets}
                    self._clone_event_descendants(event_template, updated, templates,
                                                  updated.px - previous_px,
                                                  updated.py - previous_py)
            else:
                # Keep stable UID, transform, flags and event ownership, and replace visuals.
                actual.name = updated.name
                actual.source = updated.source
                actual.pressed_source = updated.pressed_source
                actual.uv_rect = updated.uv_rect
                actual.pressed_uv_rect = updated.pressed_uv_rect
                actual.button_se = updated.button_se
                actual.toggle_default = updated.toggle_default
                actual.toggle_group = name
                load_visual(actual)
            cursor += max(1.0, updated.bounds[2] - updated.bounds[0]) + gap
        for a in self.assets:
            if a.toggle_group == name:
                self.toggle_states[a.uid] = a.toggle_default
        self._repair_toggle_groups()
        self._drag_static_cache = None
        self.refresh_tree()
        self.select(members[initial_index].uid)
        self.request_draw("both")
        self.status_text.set(f"排他トグルグループ {name}: {len(members)} 個 / 初期ON={members[initial_index].name}")

    def add_asset(self, kind: str, event_owner_uid: Optional[str] = None) -> Optional[Asset]:
        if len(self.assets) >= MAX_ASSETS:
            messagebox.showwarning(APP_NAME, "アセット数が上限に達しました。", parent=self)
            return None
        owner = self._asset_by_uid(event_owner_uid) if event_owner_uid else None
        if event_owner_uid and (owner is None or owner.kind != "toggle_uv"):
            messagebox.showerror(APP_NAME, "イベント元のトグルボタンが見つかりません。", parent=self)
            return None
        options = {
            "window": [("ウィンドウ画像", "*.png *.jpg *.jpeg")],
            "image": [("画像 / アトラス", "*.png *.jpg *.jpeg *.plist")],
            "image_uv": [("画像（UV切り出し）", "*.png *.jpg *.jpeg")],
            "button_uv": [("ボタン画像（UV切り出し）", "*.png *.jpg *.jpeg")],
            "toggle_uv": [("トグルボタン画像 / plistアトラス", "*.png *.jpg *.jpeg *.plist")],
            "button": [("アトラス", "*.plist")],
            "animation": [("Cocos Studio ExportJson", "*.ExportJson *.exportjson *.json")],
        }
        dialog_parent = self._event_windows.get(event_owner_uid, self) if event_owner_uid else self
        selected = filedialog.askopenfilename(
            title="アセットファイルを選択", initialdir=self._dialog_initial_dir(kind),
            filetypes=options[kind] + [("すべて", "*.*")], parent=dialog_parent)
        if not selected:
            return
        try:
            source = ensure_asset_path(Path(selected))
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        self._remember_dialog_file(kind, source)
        key = ""
        name_hint: Optional[str] = None
        uv_rect: Optional[tuple[int, int, int, int]] = None
        pressed_uv_rect: Optional[tuple[int, int, int, int]] = None
        button_se: Optional[str] = None
        pressed_source: Optional[Path] = None
        pressed_key: Optional[str] = None
        toggle_default = "off"
        window_settings: Optional[tuple[int, int, tuple[int, int, int, int], str, str]] = None
        try:
            if kind == "window":
                picker_window = WindowNineSliceDialog(dialog_parent, source)
                if picker_window.result is None:
                    return
                window_settings = picker_window.result
                source = picker_window.source_path
            elif kind in ("button_uv", "toggle_uv"):
                if kind == "toggle_uv" and source.suffix.casefold() == ".plist":
                    # State records require file+UV. Preserve trimmed/rotated
                    # atlas frames by exporting each chosen sprite as a PNG.
                    atlas = load_atlas(source)
                    if not atlas.frames:
                        raise ValueError("plist内に選択できるスプライトがありません。")
                    initial_key = atlas.frames[0].name
                    picker_plist = PlistButtonStateDialog(
                        dialog_parent, source, initial_key, initial_key,
                        None, self.audio["se"], toggle_mode=True)
                    if picker_plist.result is None:
                        return
                    off_key, on_key, button_se, toggle_default = picker_plist.result
                    frames_by_name = {frame.name: frame for frame in atlas.frames}
                    off_image = extract_sprite(atlas.image, frames_by_name[off_key])
                    on_image = extract_sprite(atlas.image, frames_by_name[on_key])
                    name_hint = Path(off_key).stem
                    source = generated_sprite_png(off_image)
                    pressed_source = generated_sprite_png(on_image)
                    uv_rect = (0, 0, *off_image.size)
                    pressed_uv_rect = (0, 0, *on_image.size)
                else:
                    if source.suffix.casefold() not in (".png", ".jpg", ".jpeg"):
                        raise ValueError("UVボタンにはPNG / JPG、トグルボタンにはplistも指定できます。")
                    picker_button = ButtonUVDialog(dialog_parent, source, se_keys=self.audio["se"],
                                                   toggle_mode=(kind == "toggle_uv"))
                    if picker_button.result is None:
                        return
                    if kind == "toggle_uv":
                        uv_rect, pressed_uv_rect, button_se, pressed_source, toggle_default = picker_button.result
                    else:
                        uv_rect, pressed_uv_rect, button_se, pressed_source = picker_button.result
                    source = picker_button.state_sources["normal"]
            elif kind == "image_uv":
                if source.suffix.casefold() not in (".png", ".jpg", ".jpeg"):
                    raise ValueError("UVで配置できる画像はPNG / JPGのみです。")
                picker_uv = UVSelectionDialog(dialog_parent, source)
                if picker_uv.result is None:
                    return
                uv_rect = picker_uv.result
                source = picker_uv.path
            elif source.suffix.casefold() == ".plist" and kind != "button":
                atlas = load_atlas(source)
                if not atlas.frames:
                    raise ValueError("選択可能なスプライトがありません。")
                picker = Picker(dialog_parent, "配置するスプライトを選択", [f.name for f in atlas.frames],
                                lambda name: extract_sprite(atlas.image,
                                    next(f for f in atlas.frames if f.name == name)))
                if picker.result is None:
                    return
                key = picker.result
            elif kind == "animation":
                model = load_animation_file(source)
                options_by_name = {movement.name: movement for movement in model.movements}
                def thumbnail(name: str) -> Image.Image:
                    movement = options_by_name[name]
                    if isinstance(model, UIAnimationModel):
                        w, h = model.width, model.height
                        scale = min(300 / w, 310 / h)
                        return render_ui_export(model, movement, 0,
                            max(1, round(w * scale)), max(1, round(h * scale)),
                            scale, camera=(0.0, 0.0))[0]
                    x0, y0, x1, y1 = pose_bounds(model, movement)
                    scale = min(300 / max(1, x1 - x0), 310 / max(1, y1 - y0))
                    return render_armature(model, movement, 0,
                        max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale)),
                        scale, camera=((x0 + x1) / 2, (y0 + y1) / 2))[0]
                picker = Picker(dialog_parent, "配置するアニメーションを選択", list(options_by_name), thumbnail)
                if picker.result is None:
                    return
                key = picker.result
            elif kind == "button":
                atlas = load_atlas(source)
                if not atlas.frames:
                    raise ValueError("選択できるスプライトがありません。")
                initial_key = atlas.frames[0].name
                dialog = PlistButtonStateDialog(dialog_parent, source, initial_key,
                                                initial_key, None, self.audio["se"])
                if dialog.result is None:
                    return
                key, pressed_key, button_se = dialog.result
            elif source.suffix.casefold() not in (".png", ".jpg", ".jpeg"):
                raise ValueError("画像は .png / .jpg / .plist のみ対応しています。")
        except (OSError, ValueError, MemoryError, TypeError, json.JSONDecodeError) as exc:
            messagebox.showerror(APP_NAME, f"読み込みに失敗しました:\n{exc}", parent=self)
            return
        basename = name_hint or (Path(key).stem if key else source.stem)
        candidate = re.sub(r"[^\w.\-]", "_", basename, flags=re.UNICODE) or kind
        used = {a.name for a in self.assets if a.category == ("animation" if kind == "animation" else "ui")}
        name = candidate
        count = 2
        while name in used:
            name = f"{candidate}_{count}"
            count += 1
        px, py = self._pending_spawn
        asset = Asset(name=name, kind="image" if kind == "image_uv" else kind, source=source,
                      window_width=window_settings[0] if window_settings else 0,
                      window_height=window_settings[1] if window_settings else 0,
                      window_slices=window_settings[2] if window_settings else (1, 1, 1, 1),
                      window_mode=window_settings[3] if window_settings else "repeat",
                      window_alpha_mode="source",
                      key=key, uv_rect=uv_rect, pressed_uv_rect=pressed_uv_rect,
                      button_se=button_se, pressed_source=pressed_source,
                      pressed_key=pressed_key, toggle_default=toggle_default,
                      event_owner_uid=event_owner_uid,
                      px=round(px, 2), py=round(py, 2))
        load_visual(asset)
        if asset.missing:
            messagebox.showwarning(APP_NAME, f"画像の一部を読み込めませんでした:\n{asset.missing}", parent=self)
        self._push_undo()
        if owner is not None:
            position = self.assets.index(owner) + 1
            # Insert above the existing event children associated with this parent.
            while position < len(self.assets) and self.assets[position].event_owner_uid == owner.uid:
                position += 1
            self.assets.insert(position, asset)
            owner.events.append({"type": "show", "asset_uid": asset.uid})
        else:
            self.assets.append(asset)
        if asset.kind == "toggle_uv":
            self.toggle_states[asset.uid] = asset.toggle_default
        self.refresh_tree()
        self.select(asset.uid)
        self.request_draw("both")
        self.status_text.set(f"配置: {asset.name} ({source.name})")
        self._refresh_event_windows()
        return asset

    def edit_selected_window(self) -> None:
        asset = self.get_selected()
        if asset is None or asset.kind != "window":
            return
        dialog = WindowNineSliceDialog(self, asset.source,
                    settings=(asset.window_width, asset.window_height,
                              asset.window_slices, asset.window_mode,
                              asset.window_alpha_mode))
        if dialog.result is None:
            return
        w, h, borders, mode, alpha_mode = dialog.result
        if (asset.source, asset.window_width, asset.window_height,
                asset.window_slices, asset.window_mode, asset.window_alpha_mode) == (
                    dialog.source_path, w, h, borders, mode, alpha_mode):
            return
        self._push_undo()
        asset.source = dialog.source_path
        asset.window_width, asset.window_height = w, h
        asset.window_slices, asset.window_mode = borders, mode
        asset.window_alpha_mode = alpha_mode
        load_visual(asset)
        self._drag_static_cache = None
        self.select(asset.uid)
        self.request_draw("both")

    def edit_selected_uv(self) -> None:
        """Adjust an existing PNG/JPG source region without affecting its scene transform."""
        asset = self.get_selected()
        if asset is None or asset.kind not in ("image", "button_uv", "toggle_uv") or asset.source.suffix.casefold() not in (".png", ".jpg", ".jpeg"):
            return
        try:
            if asset.kind in ("button_uv", "toggle_uv"):
                dialog = ButtonUVDialog(self, asset.source, asset.uv_rect,
                                        asset.pressed_uv_rect, asset.button_se, self.audio["se"],
                                        pressed_path=asset.pressed_source,
                                        toggle_mode=(asset.kind == "toggle_uv"),
                                        default_state=asset.toggle_default)
            else:
                dialog = UVSelectionDialog(self, asset.source, asset.uv_rect)
        except (OSError, ValueError, TypeError, MemoryError, UnidentifiedImageError) as exc:
            messagebox.showerror(APP_NAME, f"UV編集画面を開けませんでした:\n{exc}", parent=self)
            return
        if dialog.result is None:
            return
        if asset.kind in ("button_uv", "toggle_uv"):
            toggle = asset.kind == "toggle_uv"
            if toggle:
                normal, pressed, selected_se, pressed_source, default_state = dialog.result
            else:
                normal, pressed, selected_se, pressed_source = dialog.result
                default_state = asset.toggle_default
            normal_source = dialog.state_sources["normal"]
            if (normal_source, normal, pressed, selected_se, pressed_source, default_state) == (
                    asset.source, asset.uv_rect, asset.pressed_uv_rect, asset.button_se,
                    asset.pressed_source or asset.source, asset.toggle_default):
                return
            self._push_undo()
            asset.source = normal_source
            asset.uv_rect, asset.pressed_uv_rect, asset.button_se = normal, pressed, selected_se
            asset.pressed_source = pressed_source
            asset.toggle_default = default_state
            if toggle:
                self.toggle_states[asset.uid] = default_state
                self.toggle_default_var.set(default_state)
            self._refresh_animation_se_choices(asset)
        else:
            if dialog.result == asset.uv_rect and dialog.path == asset.source:
                return
            self._push_undo()
            asset.source = dialog.path
            asset.uv_rect = dialog.result
        load_visual(asset)
        self._drag_static_cache = None
        self._remember_dialog_file("button_uv" if asset.kind in ("button_uv", "toggle_uv") else "image_uv",
                                   asset.source)
        if asset.missing:
            messagebox.showwarning(APP_NAME, f"UV画像の読み込みに失敗しました:\n{asset.missing}", parent=self)
        self.request_draw("both")

    def _hit_asset(self, key: str, x: float, y: float) -> Optional[Asset]:
        polygons = self.editor_shapes if key == "edit" else self.preview_shapes
        shown = self._preview_visible_uids() if key == "preview" else self._editor_visible_uids()
        for asset in reversed(self.assets):
            if asset.uid not in shown:
                continue
            group=self._scroll_group_of(asset.uid) if key=="preview" else None
            if group is not None:
                gx0,gy0,gx1,gy1=group.bounds
                wx,wy=self.screen_to_world("preview",x,y)
                if not (gx0<=wx<=gx1 and gy0<=wy<=gy1):
                    continue
            if inside_polygon(x, y, polygons.get(asset.uid, [])) if polygons.get(asset.uid) else False:
                return asset
        return None

    def _begin_drag_visual(self, asset: Asset) -> None:
        """Rasterize the *unmoving* scene once, and draw the active asset separately."""
        self._drag_visual_ready = False
        viewport = self.viewport(self.edit_canvas)
        self.viewports["edit"] = viewport
        vx, vy, content_w, content_h, scale = viewport
        width, height = max(1, round(content_w)), max(1, round(content_h))
        try:
            # The selected asset is excluded from this background. Drag events will
            # never re-render any of the other (possibly expensive) armatures.
            # Reuse the passive scene when consecutively moving the same asset.
            # The key includes the state of every other asset, preventing stale images.
            selected_index = self.assets.index(asset)
            below = self.assets[:selected_index]
            above = self.assets[selected_index + 1:]
            editor_shown = self._editor_visible_uids()
            signature = (asset.uid, selected_index, width, height,
                id(self.background_image),
                tuple((other.uid, other.px, other.py, other.sx, other.sy,
                       other.rotation, other.flip_x, other.flip_y,
                       other.toggle_default, other.uid not in editor_shown,
                       id(other.image), id(other.model))
                      for other in self.assets if other.uid != asset.uid))
            if self._drag_static_cache is not None and self._drag_static_cache[0] == signature:
                self._drag_base_photo = self._drag_static_cache[1]
                self._drag_upper_photo = self._drag_static_cache[2]
            else:
                base = self.render_composite(width, height, scale, False,
                                             assets_override=below)
                self._drag_base_photo = ImageTk.PhotoImage(base)
                self._drag_upper_photo = None
                if above:
                    upper = self.render_composite(width, height, scale, False,
                                                  assets_override=above, transparent=True)
                    self._drag_upper_photo = ImageTk.PhotoImage(upper)
                self._drag_static_cache = (signature, self._drag_base_photo, self._drag_upper_photo)
            canvas = self.edit_canvas
            canvas.delete("all")
            canvas.create_image(vx, vy, image=self._drag_base_photo, anchor="nw")
            canvas.create_rectangle(vx, vy, vx + content_w, vy + content_h,
                                    outline="#566173")
            for other in self.assets:
                if other.uid != asset.uid and other.uid in editor_shown:
                    polygon = self.polygon(other, "edit")
                    canvas.create_polygon(*[n for xy in polygon for n in xy],
                                          outline=OUTLINE_COLOR, fill="", width=1)
            self._drag_asset_item = None
            self._drag_asset_photo = None
            self._drag_visual_ready = True
            self._last_transform_refresh = 0.0
            self._update_drag_visual(asset, rebuild_bitmap=True)
            # Overlay higher-Z assets *after* the active sprite, but keep editing
            # handles above everything, matching the full compositor's draw order.
            if self._drag_upper_photo is not None:
                canvas.create_image(vx, vy, image=self._drag_upper_photo, anchor="nw")
                canvas.tag_raise("active_outline")
                canvas.tag_raise("active_handle")
        except (OSError, ValueError, MemoryError, tk.TclError) as exc:
            self._drag_visual_ready = False
            self.status_text.set(f"操作プレビューを開始できません: {exc}")

    def _drag_bitmap(self, asset: Asset) -> ImageTk.PhotoImage:
        scale = self.viewports["edit"][4]
        raster, _density = asset_bitmap(asset, 0, scale)
        if (asset.kind == "toggle_uv" and
                (self.toggle_states.get(asset.uid, asset.toggle_default)
                 if self.editor_sync_preview_visibility.get() else asset.toggle_default) == "on"
                and asset.pressed_image is not None):
            raster = asset.pressed_image
            intrinsic_w, intrinsic_h = raster.size
        else:
            x0, y0, x1, y1 = asset.bounds
            intrinsic_w, intrinsic_h = x1 - x0, y1 - y0
        if asset.kind == "window":
            intrinsic_w, intrinsic_h = raster.size
            target_w = max(1, min(8192, round(intrinsic_w * scale)))
            target_h = max(1, min(8192, round(intrinsic_h * scale)))
        else:
            target_w = max(1, min(8192, round(intrinsic_w * scale * asset.sx)))
            target_h = max(1, min(8192, round(intrinsic_h * scale * asset.sy)))
        if target_w * target_h > MAX_RENDER_PIXELS:
            raise ValueError("操作中の画像が描画上限を超えています。")
        sampler = RESAMPLE_NEAREST if asset.kind == "window" else RESAMPLE
        resized = raster.resize((target_w, target_h), sampler)
        resized = apply_scene_flips(resized, asset)
        rotated = resized.rotate(math.degrees(asset.rotation),
                                 expand=True, resample=sampler)
        return ImageTk.PhotoImage(rotated)

    def _update_drag_visual(self, asset: Asset, *, rebuild_bitmap: bool = False,
                            throttle_bitmap: bool = False) -> None:
        """Translate the Tk item directly during movement; only transform its bitmap on resize/rotation."""
        if not self._drag_visual_ready:
            return
        try:
            canvas = self.edit_canvas
            now = time.monotonic()
            if throttle_bitmap and self._drag_asset_photo is not None and (
                    now - self._last_transform_refresh < DRAG_TRANSFORM_INTERVAL):
                rebuild_bitmap = False
            if rebuild_bitmap or self._drag_asset_photo is None:
                photo = self._drag_bitmap(asset)
                self._last_transform_refresh = now
                self._drag_asset_photo = photo
                if self._drag_asset_item is not None:
                    canvas.itemconfigure(self._drag_asset_item, image=photo)
            x0, y0, x1, y1 = asset.bounds
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            wx, wy = transform_local(asset, mx, my)
            center_x, center_y = self.world_to_screen("edit", wx, wy)
            if self._drag_asset_item is None:
                self._drag_asset_item = canvas.create_image(center_x, center_y,
                                                image=self._drag_asset_photo, anchor="center")
            else:
                canvas.coords(self._drag_asset_item, center_x, center_y)
            canvas.delete("active_outline")
            canvas.delete("active_handle")
            polygon = self.polygon(asset, "edit")
            self.editor_shapes[asset.uid] = polygon
            canvas.create_polygon(*[n for xy in polygon for n in xy],
                                  outline=SELECTION_COLOR, fill="", width=2,
                                  tags="active_outline")
            self._draw_handles(canvas, asset, "edit", tags="active_handle")
        except (OSError, ValueError, MemoryError, tk.TclError) as exc:
            self._drag_visual_ready = False
            self.status_text.set(f"操作中の描画に失敗しました: {exc}")

    def _toggle_spacing_snap(self) -> None:
        self.edit_canvas.delete("spacing_guide")
        self.status_text.set("等間隔スナップ: " + ("ON" if self.spacing_snap_enabled.get() else "OFF"))

    def _paint_spacing_guides(self, guides: Sequence[EqualGapSnap]) -> None:
        """Show matched distances only while moving, without repainting textures."""
        canvas = self.edit_canvas
        canvas.delete("spacing_guide")
        scrolling = (self._drag is not None and self._drag.get("mode") in
                     ("scroll_move", "scroll_resize", "scroll_create"))
        if not (self._drag_visual_ready or scrolling) or not self.spacing_snap_enabled.get():
            return
        for guide in guides:
            for start, end in guide.intervals:
                if end < start or end - start < 1e-7:
                    continue
                if guide.axis == "x":
                    x0, y0 = self.world_to_screen("edit", start, guide.perpendicular)
                    x1, _ = self.world_to_screen("edit", end, guide.perpendicular)
                    canvas.create_line(x0, y0, x1, y0, fill="#47d7c0", width=2,
                                       arrow=tk.BOTH, tags="spacing_guide")
                    canvas.create_text((x0 + x1) / 2, y0 - 10, text=f"{guide.gap:g}",
                                       fill="#47d7c0", font=("TkDefaultFont", 9), tags="spacing_guide")
                else:
                    x0, y0 = self.world_to_screen("edit", guide.perpendicular, start)
                    _, y1 = self.world_to_screen("edit", guide.perpendicular, end)
                    canvas.create_line(x0, y0, x0, y1, fill="#47d7c0", width=2,
                                       arrow=tk.BOTH, tags="spacing_guide")
                    canvas.create_text(x0 + 18, (y0 + y1) / 2, text=f"{guide.gap:g}",
                                       fill="#47d7c0", font=("TkDefaultFont", 9), tags="spacing_guide")
        canvas.tag_raise("spacing_guide")
        canvas.tag_raise("active_outline")
        canvas.tag_raise("active_handle")

    def _toggle_snap(self) -> None:
        """Editor-only preference: it does not alter the saved scene JSON."""
        if not self.snap_enabled.get():
            self.edit_canvas.delete("snap_guide")
        self.status_text.set("スナップ: ON（シーンと他アセットの端・中心）" if self.snap_enabled.get()
                             else "スナップ: OFF（自由移動）")

    def _paint_snap_guides(self, guide_x: Optional[float],
                           guide_y: Optional[float]) -> None:
        """Lightweight, non-interactive reference lines on the editor only."""
        canvas = self.edit_canvas
        canvas.delete("snap_guide")
        scrolling = (self._drag is not None and self._drag.get("mode") in
                     ("scroll_move", "scroll_resize", "scroll_create"))
        if not (self._drag_visual_ready or scrolling) or not self.snap_enabled.get():
            return
        vx, vy, vw, vh, _scale = self.viewports["edit"]
        if guide_x is not None:
            screen_x, _ = self.world_to_screen("edit", guide_x, 0.0)
            canvas.create_line(screen_x, vy, screen_x, vy + vh,
                               fill=SNAP_GUIDE_COLOR, width=1, dash=SNAP_GUIDE_DASH,
                               tags="snap_guide")
        if guide_y is not None:
            _, screen_y = self.world_to_screen("edit", 0.0, guide_y)
            canvas.create_line(vx, screen_y, vx + vw, screen_y,
                               fill=SNAP_GUIDE_COLOR, width=1, dash=SNAP_GUIDE_DASH,
                               tags="snap_guide")
        canvas.tag_raise("snap_guide")
        canvas.tag_raise("active_outline")
        canvas.tag_raise("active_handle")

    def _snap_resized_handle(self, asset: Asset, drag: dict[str, Any],
                             event: tk.Event) -> tuple[Optional[float], Optional[float]]:
        """Align active resize handle with references while keeping the opposite anchor.

        A corner can adjust both scales; a side handle adjusts its single axis.
        Shift's proportional-resize mode retains priority over snapping.
        """
        if event.state & 0x1:
            return None, None
        lx, ly = drag["handle_local"]
        ox, oy = drag["opposite_local"]
        dx, dy = lx - ox, ly - oy
        hx, hy = transform_local(asset, lx, ly)
        threshold = SNAP_DISTANCE_PX / max(self.viewports["edit"][4], 0.001)
        correction_x, guide_x = nearest_snap((hx,), drag["snap_x"], threshold)
        correction_y, guide_y = nearest_snap((hy,), drag["snap_y"], threshold)
        cs, sn = math.cos(drag["rotation"]), math.sin(drag["rotation"])
        # Single-axis handles can only adjust scale along their local axis.
        if dx == 0 or dy == 0:
            component_x = cs * dx if dx else -sn * dy
            component_y = sn * dx if dx else cs * dy
            choices: list[tuple[float, float, str]] = []
            if guide_x is not None and abs(component_x) > 1e-8:
                choices.append((abs(correction_x), correction_x / component_x, "x"))
            if guide_y is not None and abs(component_y) > 1e-8:
                choices.append((abs(correction_y), correction_y / component_y, "y"))
            if not choices:
                return None, None
            _distance, adjustment, axis = min(choices, key=lambda item: item[0])
            if dx:
                candidate = asset.sx + adjustment
                if not MIN_SCALE <= candidate <= MAX_SCALE:
                    return None, None
                asset.sx = candidate
            else:
                candidate = asset.sy + adjustment
                if not MIN_SCALE <= candidate <= MAX_SCALE:
                    return None, None
                asset.sy = candidate
            guide_x = guide_x if axis == "x" else None
            guide_y = guide_y if axis == "y" else None
        else:
            if guide_x is None and guide_y is None:
                return None, None
            # For two available degrees of freedom, solve the displacement
            # in local scale directions (inverse of the rotation matrix).
            if guide_x is not None and guide_y is not None:
                adjustment_x = (cs * correction_x + sn * correction_y) / dx
                adjustment_y = (-sn * correction_x + cs * correction_y) / dy
            elif guide_x is not None:
                # Adjust the scale with the strongest influence on world X.
                if abs(cs * dx) >= abs(sn * dy) and abs(cs * dx) > 1e-8:
                    adjustment_x, adjustment_y = correction_x / (cs * dx), 0.0
                elif abs(sn * dy) > 1e-8:
                    adjustment_x, adjustment_y = 0.0, -correction_x / (sn * dy)
                else:
                    return None, None
            else:
                if abs(sn * dx) >= abs(cs * dy) and abs(sn * dx) > 1e-8:
                    adjustment_x, adjustment_y = correction_y / (sn * dx), 0.0
                elif abs(cs * dy) > 1e-8:
                    adjustment_x, adjustment_y = 0.0, correction_y / (cs * dy)
                else:
                    return None, None
            next_sx = asset.sx + adjustment_x
            next_sy = asset.sy + adjustment_y
            if not (MIN_SCALE <= next_sx <= MAX_SCALE and MIN_SCALE <= next_sy <= MAX_SCALE):
                return None, None
            asset.sx, asset.sy = next_sx, next_sy
        fixed_x, fixed_y = drag["opposite_world"]
        asset.px = fixed_x - (cs * asset.sx * ox - sn * asset.sy * oy)
        asset.py = fixed_y - (sn * asset.sx * ox + cs * asset.sy * oy)
        actual_x, actual_y = transform_local(asset, lx, ly)
        # Only display lines that truly intersect the transformed active handle.
        if guide_x is not None and abs(actual_x - guide_x) > 1e-5:
            guide_x = None
        if guide_y is not None and abs(actual_y - guide_y) > 1e-5:
            guide_y = None
        return guide_x, guide_y

    def _clear_drag_visual(self) -> None:
        self._drag_visual_ready = False
        self._drag_base_photo = None
        self._drag_upper_photo = None
        self._drag_asset_photo = None
        self._drag_asset_item = None
        self._group_drag_photos = []
        self._group_drag_items = {}

    def _begin_group_drag_visual(self, members: list[Asset]) -> None:
        """Render stationary layers once; keep every movable sprite as a Tk item.

        Static segments are interleaved with the selected sprites in the original
        layer order, so overlapping sprites keep their normal foreground order.
        """
        self._clear_drag_visual()
        viewport = self.viewport(self.edit_canvas)
        self.viewports["edit"] = viewport
        vx, vy, content_w, content_h, scale = viewport
        width, height = max(1, round(content_w)), max(1, round(content_h))
        canvas = self.edit_canvas
        canvas.delete("all")
        selected = {asset.uid for asset in members}
        pending: list[Asset] = []
        first_segment = True
        try:
            def flush_static() -> None:
                nonlocal pending, first_segment
                if not pending and not first_segment:
                    return
                image = self.render_composite(width, height, scale, False,
                    assets_override=pending, transparent=not first_segment)
                photo = ImageTk.PhotoImage(image)
                self._group_drag_photos.append(photo)
                canvas.create_image(vx, vy, anchor="nw", image=photo)
                first_segment = False
                pending = []

            for asset in self.assets:
                if asset.uid not in selected:
                    pending.append(asset)
                    continue
                flush_static()
                photo = self._drag_bitmap(asset)
                self._group_drag_photos.append(photo)
                x0, y0, x1, y1 = asset.bounds
                wx, wy = transform_local(asset, (x0 + x1)/2, (y0 + y1)/2)
                cx, cy = self.world_to_screen("edit", wx, wy)
                self._group_drag_items[asset.uid] = canvas.create_image(
                    cx, cy, image=photo, anchor="center")
            flush_static()
            canvas.create_rectangle(vx, vy, vx+content_w, vy+content_h, outline="#566173")
            editor_shown = self._editor_visible_uids()
            for asset in self.assets:
                if asset.uid not in editor_shown:
                    continue
                polygon = self.polygon(asset, "edit")
                self.editor_shapes[asset.uid] = polygon
                canvas.create_polygon(*[n for xy in polygon for n in xy],
                    outline=SELECTION_COLOR if asset.uid in selected else OUTLINE_COLOR,
                    fill="", width=2 if asset.uid in selected else 1,
                    tags="active_outline" if asset.uid in selected else "")
            self._drag_visual_ready = True
        except (OSError, ValueError, MemoryError, tk.TclError) as exc:
            self._drag_visual_ready = False
            self.status_text.set(f"複数アセットの移動表示に失敗しました: {exc}")

    def _update_group_drag_visual(self, members: list[Asset]) -> None:
        if not self._drag_visual_ready:
            return
        canvas = self.edit_canvas
        for asset in members:
            item = self._group_drag_items.get(asset.uid)
            if item is None:
                continue
            x0, y0, x1, y1 = asset.bounds
            wx, wy = transform_local(asset, (x0 + x1)/2, (y0 + y1)/2)
            canvas.coords(item, *self.world_to_screen("edit", wx, wy))
        canvas.delete("active_outline")
        for asset in members:
            polygon = self.polygon(asset, "edit")
            self.editor_shapes[asset.uid] = polygon
            canvas.create_polygon(*[n for xy in polygon for n in xy],
                outline=SELECTION_COLOR, fill="", width=2, tags="active_outline")

    def _begin_marquee(self, event: tk.Event, add: bool) -> None:
        """An empty-space drag selects assets intersecting its screen rectangle."""
        base = set(self.selected_uids) if add else set()
        if not add:
            self.select(None)
        self._drag = {"mode": "marquee", "start_screen": (event.x, event.y),
                      "end_screen": (event.x, event.y), "base": base}
        self.edit_canvas.delete("marquee")
        self.edit_canvas.create_rectangle(event.x, event.y, event.x, event.y,
            outline="#5fe2fa", width=2, dash=(4, 3), tags="marquee")

    def _finish_marquee(self, drag: dict[str, Any]) -> None:
        ax, ay = drag["start_screen"]
        bx, by = drag["end_screen"]
        left, right = sorted((ax, bx))
        top, bottom = sorted((ay, by))
        chosen = set(drag["base"])
        # A click on empty space clears selection; do not select by a zero-size box.
        if abs(ax - bx) >= 3 or abs(ay - by) >= 3:
            editor_shown = self._editor_visible_uids()
            for asset in self.assets:
                if asset.uid not in editor_shown:
                    continue
                vertices = self.editor_shapes.get(asset.uid) or self.polygon(asset, "edit")
                if not vertices:
                    continue
                xs = [vertex[0] for vertex in vertices]
                ys = [vertex[1] for vertex in vertices]
                if max(xs) >= left and min(xs) <= right and max(ys) >= top and min(ys) <= bottom:
                    chosen.add(asset.uid)
        self.edit_canvas.delete("marquee")
        primary = next((a.uid for a in reversed(self.assets) if a.uid in chosen), None)
        self.select(primary, selection=chosen)

    def _begin_group_move(self, event: tk.Event, assets: list[Asset]) -> None:
        point = self.screen_to_world("edit", event.x, event.y)
        origins = {a.uid: (a.px, a.py) for a in assets}
        editor_shown = self._editor_visible_uids()
        others = [a for a in self.assets if a.uid not in origins
                  and a.uid in editor_shown]
        boxes = [asset_world_aabb(a) for a in assets]
        initial_box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                       max(b[2] for b in boxes), max(b[3] for b in boxes))
        snap_x, snap_y = snap_reference_lines(others, "")
        self._drag = {"mode": "group_move", "members": assets,
            "origins": origins, "start": point, "before": self._snapshot(),
            "initial_box": initial_box, "snap_x": snap_x, "snap_y": snap_y,
            "spacing_boxes": tuple(asset_world_aabb(a) for a in others)}
        self._begin_group_drag_visual(assets)

    def _move_group(self, event: tk.Event, drag: dict[str, Any]) -> None:
        world = self.screen_to_world("edit", event.x, event.y)
        dx = world[0] - drag["start"][0]
        dy = world[1] - drag["start"][1]
        x0, y0, x1, y1 = drag["initial_box"]
        raw = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
        threshold = SNAP_DISTANCE_PX / max(self.viewports["edit"][4], 0.001)
        sx = sy = 0.0
        gx = gy = None
        gap_guides: list[EqualGapSnap] = []
        if self.snap_enabled.get():
            sx, gx = nearest_snap((raw[0], (raw[0]+raw[2])/2, raw[2]), drag["snap_x"], threshold)
            sy, gy = nearest_snap((raw[1], (raw[1]+raw[3])/2, raw[3]), drag["snap_y"], threshold)
        if self.spacing_snap_enabled.get():
            for axis in ("x", "y"):
                candidate = equal_gap_snap(raw, drag["spacing_boxes"], axis,
                                            threshold, proximity=threshold * 2)
                if candidate is None:
                    continue
                if axis == "x" and (gx is None or abs(candidate.delta) <= abs(sx)):
                    sx, gx = candidate.delta, None
                    gap_guides.append(candidate)
                elif axis == "y" and (gy is None or abs(candidate.delta) <= abs(sy)):
                    sy, gy = candidate.delta, None
                    gap_guides.append(candidate)
        for asset in drag["members"]:
            px, py = drag["origins"][asset.uid]
            asset.px, asset.py = px + dx + sx, py + dy + sy
        if self.get_selected() is not None:
            self._sync_selected_fields(self.get_selected())
        self._update_group_drag_visual(drag["members"])
        self._paint_snap_guides(gx, gy)
        self._paint_spacing_guides(gap_guides)

    def editor_press(self, event: tk.Event) -> None:
        self.edit_canvas.focus_set()
        if self._scroll_create_mode:
            self._begin_scroll_rectangle(event)
            return
        if self.selection_mode.get() == "scroll":
            group_hit = self._scroll_group_hit(event.x, event.y, True)
            if group_hit is not None and not (event.state & (0x1 | 0x4)):
                self._begin_scroll_group_drag(event, *group_hit)
            else:
                self.selected_scroll_group_name = None
                self._draw_scroll_group_overlays()
                self._drag = None
            return
        # Asset mode: scroll frames must never intercept an asset drag.
        if self.selected_scroll_group_name is not None:
            self.selected_scroll_group_name = None
            self._draw_scroll_group_overlays()
        # Resize/rotate handles extend beyond the sprite polygon.  Treat them
        # as hits before starting a marquee so original PowerPoint-style
        # handles remain available for single selection.
        selected = self.get_selected()
        editor_shown = self._editor_visible_uids()
        on_handle = (selected is not None and len(self.selected_uids) == 1 and
                     selected.uid in editor_shown and
                     any(math.hypot(hx-event.x, hy-event.y) <= HANDLE_HIT_RADIUS
                         for hx, hy in self._handles(selected, "edit").values()))
        hit = selected if on_handle else self._hit_asset("edit", event.x, event.y)
        if hit is None:
            self._begin_marquee(event, bool(event.state & 0x1))
            return
        if event.state & (0x1 | 0x4):
            # Shift adds, Ctrl toggles an asset without dragging it.
            choice = set(self.selected_uids)
            if event.state & 0x4 and hit.uid in choice:
                choice.remove(hit.uid)
            else:
                choice.add(hit.uid)
            self.select(hit.uid if hit.uid in choice else None, selection=choice)
            self._drag = None
            return
        if hit.uid in self.selected_uids and len(self.selected_uids) > 1:
            members = [a for a in self.assets if a.uid in self.selected_uids
                       and a.uid in editor_shown]
            if len(members) > 1:
                self._begin_group_move(event, members)
                return
        asset = self.get_selected()
        handle = None
        if asset and asset.uid in editor_shown:
            for label, (x, y) in self._handles(asset, "edit").items():
                if math.hypot(x - event.x, y - event.y) <= HANDLE_HIT_RADIUS:
                    handle = label
                    break
        if handle is None:
            asset = self._hit_asset("edit", event.x, event.y)
            self.select(asset.uid if asset else None)
        if asset is None:
            self._drag = None
            return
        point_world = self.screen_to_world("edit", event.x, event.y)
        drag: dict[str, Any] = {
            "mode": "rotate" if handle == "rotate" else "resize" if handle else "move",
            "asset": asset, "start": point_world, "px": asset.px, "py": asset.py,
            "sx": asset.sx, "sy": asset.sy, "rotation": asset.rotation,
            "handle": handle,
        }
        if handle == "rotate":
            drag["start_angle"] = math.atan2(point_world[1] - asset.py, point_world[0] - asset.px)
        elif handle:
            x0, y0, x1, y1 = asset.bounds
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            lx = x0 if "w" in handle else x1 if "e" in handle else mx
            ly = y1 if "n" in handle else y0 if "s" in handle else my
            ox = x1 if "w" in handle else x0 if "e" in handle else mx
            oy = y0 if "n" in handle else y1 if "s" in handle else my
            drag["handle_local"] = (lx, ly)
            drag["opposite_local"] = (ox, oy)
            drag["opposite_world"] = transform_local(asset, ox, oy)
        drag["before"] = self._snapshot()
        visible_others = [other for other in self.assets
                          if other.uid != asset.uid and other.uid in editor_shown]
        drag["snap_x"], drag["snap_y"] = snap_reference_lines(visible_others, asset.uid)
        drag["spacing_boxes"] = tuple(asset_world_aabb(other) for other in visible_others)
        self._drag = drag
        self._begin_drag_visual(asset)

    def editor_motion(self, event: tk.Event) -> None:
        d = self._drag
        if not d:
            return
        if d["mode"] in ("scroll_move", "scroll_resize", "scroll_create"):
            self._scroll_editor_motion(event, d)
            return
        if d["mode"] == "marquee":
            d["end_screen"] = (event.x, event.y)
            self.edit_canvas.coords("marquee", *d["start_screen"], event.x, event.y)
            return
        if d["mode"] == "group_move":
            self._move_group(event, d)
            return
        asset: Asset = d["asset"]
        world = self.screen_to_world("edit", event.x, event.y)
        guide_x: Optional[float] = None
        guide_y: Optional[float] = None
        spacing_guides: list[EqualGapSnap] = []
        if d["mode"] == "move":
            asset.px = d["px"] + world[0] - d["start"][0]
            asset.py = d["py"] + world[1] - d["start"][1]
            raw_box = asset_world_aabb(asset)
            threshold = SNAP_DISTANCE_PX / max(self.viewports["edit"][4], 0.001)
            shift_x = shift_y = 0.0
            if self.snap_enabled.get():
                x0, y0, x1, y1 = raw_box
                shift_x, guide_x = nearest_snap(
                    ((x0 + x1) / 2.0, x0, x1), d["snap_x"], threshold)
                shift_y, guide_y = nearest_snap(
                    ((y0 + y1) / 2.0, y0, y1), d["snap_y"], threshold)
            if self.spacing_snap_enabled.get():
                # Evaluate independently in each direction using the unsnapped
                # bounding box.  Prefer the shorter correction on each axis.
                for axis in ("x", "y"):
                    candidate = equal_gap_snap(raw_box, d["spacing_boxes"], axis,
                                               threshold, proximity=threshold * 2)
                    if candidate is None:
                        continue
                    if axis == "x" and (guide_x is None or abs(candidate.delta) <= abs(shift_x)):
                        shift_x, guide_x = candidate.delta, None
                        spacing_guides.append(candidate)
                    elif axis == "y" and (guide_y is None or abs(candidate.delta) <= abs(shift_y)):
                        shift_y, guide_y = candidate.delta, None
                        spacing_guides.append(candidate)
            asset.px += shift_x
            asset.py += shift_y
        elif d["mode"] == "rotate":
            angle = math.atan2(world[1] - d["py"], world[0] - d["px"])
            diff = math.atan2(math.sin(angle - d["start_angle"]),
                              math.cos(angle - d["start_angle"]))
            asset.rotation = d["rotation"] + diff
            if event.state & 0x1:
                increment = math.pi / 12
                asset.rotation = round(asset.rotation / increment) * increment
        else:
            ox, oy = d["opposite_world"]
            cs, sn = math.cos(d["rotation"]), math.sin(d["rotation"])
            dx, dy = world[0] - ox, world[1] - oy
            invx, invy = cs * dx + sn * dy, -sn * dx + cs * dy
            lx, ly = d["handle_local"]
            olx, oly = d["opposite_local"]
            nsx, nsy = d["sx"], d["sy"]
            if lx != olx:
                nsx = max(MIN_SCALE, min(MAX_SCALE, invx / (lx - olx)))
            if ly != oly:
                nsy = max(MIN_SCALE, min(MAX_SCALE, invy / (ly - oly)))
            if (event.state & 0x1) and lx != olx and ly != oly:
                change_x = nsx / d["sx"]
                change_y = nsy / d["sy"]
                factor = change_x if abs(change_x - 1) >= abs(change_y - 1) else change_y
                nsx = max(MIN_SCALE, min(MAX_SCALE, d["sx"] * factor))
                nsy = max(MIN_SCALE, min(MAX_SCALE, d["sy"] * factor))
            asset.sx, asset.sy = nsx, nsy
            asset.px = ox - (cs * nsx * olx - sn * nsy * oly)
            asset.py = oy - (sn * nsx * olx + cs * nsy * oly)
            if self.snap_enabled.get():
                guide_x, guide_y = self._snap_resized_handle(asset, d, event)
            if asset.kind == "window":
                min_x, min_y = window_min_scales(asset)
                asset.sx = max(asset.sx, min_x)
                asset.sy = max(asset.sy, min_y)
                # Preserve the opposite resize handle as the fixed anchor.
                asset.px = ox - (cs * asset.sx * olx - sn * asset.sy * oly)
                asset.py = oy - (sn * asset.sx * olx + cs * asset.sy * oly)
        self._sync_selected_fields(asset)
        # No full-scene or preview recomposition while moving the mouse.
        self._update_drag_visual(asset, rebuild_bitmap=d["mode"] != "move",
                                 throttle_bitmap=d["mode"] != "move")
        self._paint_snap_guides(guide_x, guide_y)
        self._paint_spacing_guides(spacing_guides)

    def _sync_selected_fields(self, asset: Asset) -> None:
        for field_name, value in (("px", asset.px), ("py", asset.py),
                                  ("sx", asset.sx), ("sy", asset.sy),
                                  ("degrees", math.degrees(asset.rotation))):
            self.vars[field_name].set(jnum(value))

    def editor_release(self, _event: Any) -> None:
        drag = self._drag
        if drag is None:
            return
        if drag["mode"] in ("scroll_move", "scroll_resize", "scroll_create"):
            self._finish_scroll_editor_drag(drag)
            return
        if drag["mode"] == "marquee":
            self._drag = None
            self._finish_marquee(drag)
            return
        if drag["mode"] == "group_move":
            moved = any((a.px, a.py) != drag["origins"][a.uid] for a in drag["members"])
            if moved:
                self._push_undo(drag["before"])
            self._drag = None
            self._clear_drag_visual()
            self._drag_static_cache = None
            self.request_draw("both")
            return
        asset = drag["asset"]
        before = (drag["px"], drag["py"], drag["sx"], drag["sy"], drag["rotation"])
        after = (asset.px, asset.py, asset.sx, asset.sy, asset.rotation)
        if before != after:
            self._push_undo(drag["before"])
        self._drag = None
        self._clear_drag_visual()
        self.request_draw("both")

    def move_with_arrow(self, event: tk.Event) -> None:
        asset = self.get_selected()
        if not asset:
            return
        step = 10 if event.state & 0x1 else 1
        delta = {"Left": (-step, 0), "Right": (step, 0),
                 "Up": (0, step), "Down": (0, -step)}[event.keysym]
        self._push_undo()
        for item in self.assets:
            if item.uid in self.selected_uids:
                item.px += delta[0]
                item.py += delta[1]
        self._drag_static_cache = None
        self._sync_selected_fields(asset)
        self.request_draw("both")

    # ----- Mouse editing for scroll-group viewports (editor only) -----

    def _scroll_group_screen_bounds(self, group: ScrollGroup) -> tuple[float,float,float,float]:
        x0,y0,x1,y1 = group.bounds
        left,top = self.world_to_screen("edit", x0, y1)
        right,bottom = self.world_to_screen("edit", x1, y0)
        return (left,top,right,bottom)

    def _scroll_group_handles(self, group: ScrollGroup) -> dict[str, tuple[float,float]]:
        left,top,right,bottom = self._scroll_group_screen_bounds(group)
        cx,cy = (left+right)/2,(top+bottom)/2
        return {"nw":(left,top), "n":(cx,top), "ne":(right,top),
                "e":(right,cy), "se":(right,bottom), "s":(cx,bottom),
                "sw":(left,bottom), "w":(left,cy)}

    def _draw_scroll_group_overlays(self) -> None:
        """Draw inexpensive Tk shapes only; no Pillow scene recomposition on resize."""
        if "edit" not in self.viewports:
            return
        canvas=self.edit_canvas
        canvas.delete("scroll_group_overlay")
        for group in self.scroll_groups.values():
            left,top,right,bottom=self._scroll_group_screen_bounds(group)
            active=group.name==self.selected_scroll_group_name
            color="#23d4ee" if active else "#40b5c7"
            canvas.create_rectangle(left,top,right,bottom,outline=color,
                                    width=3 if active else 2,
                                    dash=() if active else (6,4),
                                    tags="scroll_group_overlay")
            title=f"⇅ {group.name}  X:{'ON' if group.scroll_x else 'OFF'} / Y:{'ON' if group.scroll_y else 'OFF'}"
            # An opaque label remains legible above sprites and doubles as a drag target.
            label_id=canvas.create_text(left+6,top+6,anchor="nw",text=title,
                                         fill="#10202b" if active else "#e2ffff",
                                         tags="scroll_group_overlay")
            box=canvas.bbox(label_id)
            if box:
                rect=canvas.create_rectangle(box[0]-3,box[1]-2,box[2]+3,box[3]+2,
                                             fill="#52cfe0" if active else "#2a6b78",
                                             outline="",tags="scroll_group_overlay")
                canvas.tag_lower(rect,label_id)
            if active:
                for _,(x,y) in self._scroll_group_handles(group).items():
                    canvas.create_rectangle(x-5,y-5,x+5,y+5,fill="#f9ffff",
                                            outline="#087b90",width=2,
                                            tags="scroll_group_overlay")
        canvas.tag_raise("scroll_group_overlay")

    def _scroll_group_hit(self, sx: float, sy: float,
                          allow_inside: bool = False) -> Optional[tuple[str,str]]:
        """Handles precede border/title; Alt+drag may grab any viewport interior."""
        if "edit" not in self.viewports:
            return None
        selected=self.scroll_groups.get(self.selected_scroll_group_name or "")
        if selected is not None:
            for handle,(x,y) in self._scroll_group_handles(selected).items():
                if abs(sx-x)<=9 and abs(sy-y)<=9:
                    return (selected.name,handle)
        for group in reversed(tuple(self.scroll_groups.values())):
            left,top,right,bottom=self._scroll_group_screen_bounds(group)
            # The label occupies the top-left area just inside the viewport.
            header = (left+2 <= sx <= min(right,left+250) and
                      top+2 <= sy <= min(bottom,top+22))
            within=(left-7<=sx<=right+7 and top-7<=sy<=bottom+7)
            edge=within and (min(abs(sx-left),abs(sx-right))<=7 or
                             min(abs(sy-top),abs(sy-bottom))<=7)
            inside=left<=sx<=right and top<=sy<=bottom
            if header or edge or (allow_inside and inside):
                return (group.name,"move")
        return None

    def enable_scroll_create_mode(self) -> None:
        self._scroll_create_mode=True
        self.scroll_create_button.configure(text="範囲作成を終了 (Esc)")
        self.edit_canvas.configure(cursor="crosshair")
        self.edit_canvas.focus_set()
        self.status_text.set("編集画面でドラッグしてスクロール範囲を指定してください。Escでキャンセルできます。")

    def toggle_scroll_create_mode(self) -> None:
        if self._scroll_create_mode:
            self._leave_scroll_create_mode()
        else:
            self.enable_scroll_create_mode()

    def _leave_scroll_create_mode(self) -> None:
        self._scroll_create_mode=False
        self.scroll_create_button.configure(text="ドラッグで範囲作成")
        self.edit_canvas.configure(cursor="arrow")
        self.edit_canvas.delete("scroll_new_range")

    def _escape_editor(self, _event: Any = None) -> str:
        if self._drag is not None and self._drag.get("mode") in (
                "scroll_move", "scroll_resize", "scroll_create"):
            d=self._drag
            if d["mode"] != "scroll_create":
                group=self.scroll_groups.get(d["group"])
                if group is not None:
                    group.px,group.py,group.width,group.height=d["original"]
            self._drag=None
            self.edit_canvas.delete("snap_guide")
            self.edit_canvas.delete("spacing_guide")
            self._leave_scroll_create_mode()
            self._draw_scroll_group_overlays()
            self.request_draw("both")
        elif self._scroll_create_mode:
            self._leave_scroll_create_mode()
        else:
            self.selected_scroll_group_name=None
            self.select(None)
            self._draw_scroll_group_overlays()
        return "break"

    def _editor_hover(self, event: tk.Event) -> None:
        if self._drag is not None or self._scroll_create_mode:
            return
        if self.selection_mode.get() != "scroll":
            if self.edit_canvas.cget("cursor") != "arrow":
                self.edit_canvas.configure(cursor="arrow")
            return
        hit=self._scroll_group_hit(event.x,event.y,True)
        if hit is None:
            cursor="arrow"
        elif hit[1] in ("nw","se"):
            cursor="sizing"
        elif hit[1] in ("ne","sw"):
            cursor="sizing"
        elif hit[1] in ("n","s"):
            cursor="sb_v_double_arrow"
        elif hit[1] in ("e","w"):
            cursor="sb_h_double_arrow"
        else:
            cursor="fleur"
        if self.edit_canvas.cget("cursor")!=cursor:
            self.edit_canvas.configure(cursor=cursor)

    def _scroll_snap_references(self, exclude_name: Optional[str] = None
                                ) -> tuple[tuple[float, ...], tuple[float, ...],
                                           tuple[tuple[float, float, float, float], ...]]:
        """Snapshot snap targets at drag start; never snap a viewport to itself.

        Shared scene/asset guides and all *other* scroll-group viewport edges and
        centers participate, respecting the current editor visibility mask.
        """
        shown = self._editor_visible_uids()
        visible = [item for item in self.assets if item.uid in shown]
        snap_x, snap_y = snap_reference_lines(visible, "")
        xs, ys = list(snap_x), list(snap_y)
        boxes = [asset_world_aabb(item) for item in visible]
        for group in self.scroll_groups.values():
            if group.name == exclude_name:
                continue
            x0, y0, x1, y1 = group.bounds
            boxes.append((x0, y0, x1, y1))
            xs.extend((x0, (x0 + x1) / 2, x1))
            ys.extend((y0, (y0 + y1) / 2, y1))
        return tuple(sorted(xs)), tuple(sorted(ys)), tuple(boxes)

    def _snap_scroll_point(self, wx: float, wy: float,
                           drag: dict[str, Any]
                           ) -> tuple[float, float, Optional[float], Optional[float]]:
        if not self.snap_enabled.get():
            return wx, wy, None, None
        threshold = SNAP_DISTANCE_PX / max(self.viewports["edit"][4], 0.001)
        dx, gx = nearest_snap((wx,), drag["snap_x"], threshold)
        dy, gy = nearest_snap((wy,), drag["snap_y"], threshold)
        return wx + dx, wy + dy, gx, gy

    def _begin_scroll_group_drag(self, event: tk.Event, name: str, handle: str) -> None:
        group=self.scroll_groups.get(name)
        if group is None:
            return
        self.selected_scroll_group_name=name
        self.select(None)
        self._draw_scroll_group_overlays()
        snap_x, snap_y, spacing_boxes = self._scroll_snap_references(name)
        self._drag={"mode":"scroll_move" if handle=="move" else "scroll_resize",
                    "group":name,"handle":handle,
                    "start_world":self.screen_to_world("edit",event.x,event.y),
                    "original":(group.px,group.py,group.width,group.height),
                    "original_bounds":group.bounds,"before":self._snapshot(),
                    "snap_x":snap_x,"snap_y":snap_y,
                    "spacing_boxes":spacing_boxes}
        self.status_text.set(f"スクロール範囲 {name}: {'移動' if handle=='move' else 'サイズ変更'}中（Escで中止）")

    def _clamp_scroll_create_point(self, sx: float, sy: float) -> tuple[float,float]:
        vx,vy,vw,vh,_=self.viewports["edit"]
        return (min(max(sx,vx),vx+vw),min(max(sy,vy),vy+vh))

    def _begin_scroll_rectangle(self, event: tk.Event) -> None:
        snap_x, snap_y, _ = self._scroll_snap_references()
        drag = {"mode": "scroll_create", "snap_x": snap_x, "snap_y": snap_y}
        point = self._clamp_scroll_create_point(event.x, event.y)
        wx, wy = self.screen_to_world("edit", *point)
        wx, wy, gx, gy = self._snap_scroll_point(wx, wy, drag)
        point = self._clamp_scroll_create_point(*self.world_to_screen("edit", wx, wy))
        drag.update({"start_screen": point, "end_screen": point})
        self._drag = drag
        self.edit_canvas.delete("scroll_new_range")
        self.edit_canvas.create_rectangle(*point, *point, fill="", outline="#23d4ee",
                                          width=2, dash=(5, 3), tags="scroll_new_range")
        self._paint_snap_guides(gx, gy)

    def _scroll_editor_motion(self, event: tk.Event, d: dict[str,Any]) -> None:
        guide_x = guide_y = None
        spacing_guides: list[EqualGapSnap] = []
        if d["mode"] == "scroll_create":
            point = self._clamp_scroll_create_point(event.x, event.y)
            wx, wy = self.screen_to_world("edit", *point)
            wx, wy, guide_x, guide_y = self._snap_scroll_point(wx, wy, d)
            point = self._clamp_scroll_create_point(*self.world_to_screen("edit", wx, wy))
            d["end_screen"] = point
            self.edit_canvas.coords("scroll_new_range", *d["start_screen"], *point)
            self._paint_snap_guides(guide_x, guide_y)
            self.edit_canvas.tag_raise("scroll_new_range")
            return
        group = self.scroll_groups.get(d["group"])
        if group is None:
            return
        wx, wy = self.screen_to_world("edit", event.x, event.y)
        start_x, start_y = d["start_world"]
        threshold = SNAP_DISTANCE_PX / max(self.viewports["edit"][4], 0.001)
        if d["mode"] == "scroll_move":
            old_x, old_y, _, _ = d["original"]
            dx, dy = wx - start_x, wy - start_y
            x0, y0, x1, y1 = d["original_bounds"]
            moving = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
            correction_x = correction_y = 0.0
            if self.snap_enabled.get():
                correction_x, guide_x = nearest_snap(
                    (moving[0], (moving[0] + moving[2]) / 2, moving[2]),
                    d["snap_x"], threshold)
                correction_y, guide_y = nearest_snap(
                    (moving[1], (moving[1] + moving[3]) / 2, moving[3]),
                    d["snap_y"], threshold)
            if self.spacing_snap_enabled.get():
                for axis in ("x", "y"):
                    candidate = equal_gap_snap(moving, d["spacing_boxes"], axis,
                                               threshold, proximity=threshold * 2)
                    if candidate is None:
                        continue
                    if axis == "x" and (guide_x is None or
                                        abs(candidate.delta) <= abs(correction_x)):
                        correction_x, guide_x = candidate.delta, None
                        spacing_guides.append(candidate)
                    elif axis == "y" and (guide_y is None or
                                          abs(candidate.delta) <= abs(correction_y)):
                        correction_y, guide_y = candidate.delta, None
                        spacing_guides.append(candidate)
            group.px = old_x + dx + correction_x
            group.py = old_y + dy + correction_y
        else:
            x0, y0, x1, y1 = d["original_bounds"]
            handle = d["handle"]
            # Only the dragged edges snap; the opposite edges stay fixed.
            dx, dy = wx - start_x, wy - start_y
            if "w" in handle:
                x0 = max(x1 - DESIGN_W * 4, min(x0 + dx, x1 - 1))
            if "e" in handle:
                x1 = min(x0 + DESIGN_W * 4, max(x1 + dx, x0 + 1))
            if "n" in handle:
                y1 = min(y0 + DESIGN_H * 4, max(y1 + dy, y0 + 1))
            if "s" in handle:
                y0 = max(y1 - DESIGN_H * 4, min(y0 + dy, y1 - 1))
            if self.snap_enabled.get():
                if "w" in handle or "e" in handle:
                    edge_x = x0 if "w" in handle else x1
                    adjustment, target = nearest_snap((edge_x,), d["snap_x"], threshold)
                    proposed = edge_x + adjustment
                    width = x1 - proposed if "w" in handle else proposed - x0
                    if 1 <= width <= DESIGN_W * 4:
                        if "w" in handle:
                            x0 = proposed
                        else:
                            x1 = proposed
                        guide_x = target
                if "n" in handle or "s" in handle:
                    edge_y = y1 if "n" in handle else y0
                    adjustment, target = nearest_snap((edge_y,), d["snap_y"], threshold)
                    proposed = edge_y + adjustment
                    height = proposed - y0 if "n" in handle else y1 - proposed
                    if 1 <= height <= DESIGN_H * 4:
                        if "n" in handle:
                            y1 = proposed
                        else:
                            y0 = proposed
                        guide_y = target
            group.px = (x0 + x1) / 2
            group.py = (y0 + y1) / 2
            group.width = x1 - x0
            group.height = y1 - y0
        self._draw_scroll_group_overlays()
        self._paint_snap_guides(guide_x, guide_y)
        self._paint_spacing_guides(spacing_guides)

    def _finish_scroll_editor_drag(self, d: dict[str,Any]) -> None:
        self.edit_canvas.delete("snap_guide")
        self.edit_canvas.delete("spacing_guide")
        self._drag=None
        if d["mode"]=="scroll_create":
            start=d["start_screen"]
            end=d["end_screen"]
            self._leave_scroll_create_mode()
            if abs(start[0]-end[0])<8 or abs(start[1]-end[1])<8:
                self.status_text.set("範囲が小さすぎます。ドラッグで幅・高さを指定してください。")
                return
            wx0,wy0=self.screen_to_world("edit",*start)
            wx1,wy1=self.screen_to_world("edit",*end)
            bounds=(min(wx0,wx1),min(wy0,wy1),max(wx0,wx1),max(wy0,wy1))
            self._pending_spawn=((bounds[0]+bounds[2])/2,(bounds[1]+bounds[3])/2)
            self.edit_canvas.after_idle(lambda b=bounds: ScrollGroupDialog(self,initial_bounds=b))
            return
        group=self.scroll_groups.get(d["group"])
        if group is None:
            return
        if (group.px,group.py,group.width,group.height)!=d["original"]:
            self._push_undo(d["before"])
            self._drag_static_cache=None
            self.scroll_offsets.pop(group.name,None)
            self.status_text.set(f"スクロール範囲 '{group.name}' を更新しました。Ctrl+Zで戻せます。")
            self.request_draw("both")
        else:
            self._draw_scroll_group_overlays()

    def _on_selection_mode_change(self) -> None:
        self._drag = None
        self.edit_canvas.delete("snap_guide")
        self.edit_canvas.delete("spacing_guide")
        if self._scroll_create_mode:
            self._leave_scroll_create_mode()
        self.selected_scroll_group_name = None
        self.select(None)
        self.edit_canvas.configure(cursor="arrow")
        self.request_draw("edit")
        self.status_text.set("スクロール範囲の選択・変形" if self.selection_mode.get() == "scroll"
                             else "通常アセットの選択・変形")

    def open_scroll_workspace(self, name: Optional[str] = None) -> None:
        if not self.scroll_groups:
            messagebox.showinfo(APP_NAME, "まずスクロールグループを作成してください。", parent=self)
            return
        if isinstance(name, str) and name in self.scroll_groups:
            chosen = name
        elif self.selected_scroll_group_name in self.scroll_groups:
            chosen = self.selected_scroll_group_name
        elif len(self.scroll_groups) == 1:
            chosen = next(iter(self.scroll_groups))
        else:
            picker = ScrollWorkspacePicker(self)
            chosen = picker.result
        if chosen not in self.scroll_groups:
            return
        old = self._scroll_workspace
        if old is not None and old.winfo_exists():
            old.destroy()
        self._scroll_workspace = ScrollWorkspaceDialog(self, chosen)

    def edit_scroll_groups(self, initial_name: Optional[str] = None) -> ScrollGroupDialog:
        return ScrollGroupDialog(self, initial_name)

    def reset_scroll_offsets(self) -> None:
        self.scroll_offsets.clear()
        self._scroll_press = None
        self.request_draw("preview")
        self.status_text.set("スクロール位置を初期値に戻しました。")

    def _scroll_group_of(self, uid: str) -> Optional[ScrollGroup]:
        return next((g for g in self.scroll_groups.values() if uid in g.members), None)

    def _scroll_group_at(self, x: float, y: float) -> Optional[ScrollGroup]:
        if "preview" not in self.viewports:
            return None
        wx,wy=self.screen_to_world("preview",x,y)
        # Hidden event-gated viewports cannot intercept preview input.
        active = self._active_scroll_groups(self._preview_visible_uids())
        # The latest defined group takes precedence for overlapping viewports.
        for group in reversed(tuple(self.scroll_groups.values())):
            if group.name not in active:
                continue
            a,b,c,d=group.bounds
            if a <= wx <= c and b <= wy <= d:
                return group
        return None

    def _scroll_limits(self, group: ScrollGroup) -> tuple[tuple[float,float],tuple[float,float]]:
        bounds=[asset_world_aabb(a) for a in self.assets if a.uid in group.members]
        if not bounds:
            return ((0.0,0.0),(0.0,0.0))
        cx0=min(b[0] for b in bounds)
        cy0=min(b[1] for b in bounds)
        cx1=max(b[2] for b in bounds)
        cy1=max(b[3] for b in bounds)
        x0,y0,x1,y1=group.bounds
        xlim=(min(0.0,x1-cx1),max(0.0,x0-cx0)) if group.scroll_x else (0.0,0.0)
        ylim=(min(0.0,y1-cy1),max(0.0,y0-cy0)) if group.scroll_y else (0.0,0.0)
        return xlim,ylim

    def _scroll_by(self, group: ScrollGroup, dx: float, dy: float) -> bool:
        ox,oy=self.scroll_offsets.get(group.name,(0.0,0.0))
        xlim,ylim=self._scroll_limits(group)
        nx=max(xlim[0],min(xlim[1],ox+dx))
        ny=max(ylim[0],min(ylim[1],oy+dy))
        if abs(nx-ox)<1e-6 and abs(ny-oy)<1e-6:
            return False
        self.scroll_offsets[group.name]=(nx,ny)
        self.request_draw("preview")
        return True

    def preview_wheel(self, event: tk.Event) -> str:
        group=self._scroll_group_at(event.x,event.y)
        if group is None:
            return "break"
        if getattr(event,"num",0)==4:
            clicks=1
        elif getattr(event,"num",0)==5:
            clicks=-1
        else:
            clicks=max(-3.0,min(3.0,float(getattr(event,"delta",0))/120.0))
        if not clicks:
            return "break"
        horizontal = bool(event.state & 0x1) or (group.scroll_x and not group.scroll_y)
        if horizontal and group.scroll_x:
            self._scroll_by(group,clicks*56,0)
        elif group.scroll_y:
            self._scroll_by(group,0,clicks*56)
        return "break"

    def preview_click(self, event: tk.Event) -> None:
        if not self._scroll_dispatch_click:
            group=self._scroll_group_at(event.x,event.y)
            if group is not None:
                self._scroll_press={"group":group.name,"x":event.x,"y":event.y,
                                    "last_x":event.x,"last_y":event.y,"moved":False}
                return
        asset = self._hit_asset("preview", event.x, event.y)
        if asset is not None and asset.kind == "toggle_uv":
            self.pressed_button_uid = asset.uid
            self._button_pointer_inside = True
            self.status_text.set(f"トグルボタン操作中: {asset.name}")
            return
        if asset is not None and asset.kind in ("button_uv", "button"):
            self.pressed_button_uid = asset.uid
            self._button_pointer_inside = True
            self.request_draw("preview")
            self.status_text.set(f"押下中: {asset.name}")
            if asset.button_se:
                se_info = self.audio["se"].get(asset.button_se)
                if se_info:
                    self.audio_player.play("se", asset.button_se, Path(se_info["file"]))
                else:
                    messagebox.showwarning(APP_NAME,
                        f"ボタン {asset.name} のSE '{asset.button_se}' が未登録です。", parent=self)
            return
        if asset and asset.kind == "animation" and asset.movement is not None:
            if asset.uid in self.playing:
                self.playing.pop(asset.uid, None)
                self.last_frame[asset.uid] = 0
                self.status_text.set(f"停止: {asset.name} / {asset.key}")
            else:
                self.playing[asset.uid] = time.monotonic()
                self.last_frame[asset.uid] = 0
                self.status_text.set(f"再生: {asset.name} / {asset.key}")
                if asset.animation_se:
                    se_info = self.audio["se"].get(asset.animation_se)
                    if se_info:
                        self.audio_player.play("se", asset.animation_se, Path(se_info["file"]))
                    else:
                        messagebox.showwarning(APP_NAME,
                            f"アニメーション {asset.name} に紐づくSE '{asset.animation_se}' が未登録です。", parent=self)
                if self._tick_job is None:
                    self._tick_job = self.after(TICK_MILLISECONDS, self._tick)
            self.request_draw("preview")

    def preview_drag_held(self, event: tk.Event) -> None:
        if self._scroll_press is not None:
            drag=self._scroll_press
            group=self.scroll_groups.get(drag["group"])
            if group is None:
                self._scroll_press=None
                return
            if math.hypot(event.x-drag["x"],event.y-drag["y"])>5:
                drag["moved"]=True
            if drag["moved"]:
                scale=self.viewports["preview"][4]
                self._scroll_by(group, (event.x-drag["last_x"])/scale if group.scroll_x else 0.0,
                                -(event.y-drag["last_y"])/scale if group.scroll_y else 0.0)
                drag["last_x"],drag["last_y"]=event.x,event.y
            return
        if self.pressed_button_uid is None:
            return
        target = self._hit_asset("preview", event.x, event.y)
        active = target is not None and target.uid == self.pressed_button_uid
        if active != getattr(self, "_button_pointer_inside", True):
            self._button_pointer_inside = active
            self.request_draw("preview")

    def preview_release(self, event: tk.Event) -> None:
        if self._scroll_press is not None:
            drag=self._scroll_press
            self._scroll_press=None
            if not drag["moved"]:
                self._scroll_dispatch_click=True
                try:
                    self.preview_click(event)
                    self.preview_release(event)
                finally:
                    self._scroll_dispatch_click=False
            return
        uid = self.pressed_button_uid
        if uid is None:
            return
        asset = next((item for item in self.assets if item.uid == uid), None)
        if asset is not None and asset.kind == "toggle_uv":
            hit = self._hit_asset("preview", event.x, event.y)
            if hit is not None and hit.uid == uid:
                current = self.toggle_states.get(uid, asset.toggle_default)
                if asset.toggle_group:
                    new_state = "on"
                    for member in self.assets:
                        if member.toggle_group == asset.toggle_group:
                            self.toggle_states[member.uid] = ("on" if member.uid == uid else "off")
                    self.status_text.set(f"排他スイッチ {asset.toggle_group}: {asset.name} をONにしました。")
                else:
                    new_state = "on" if current == "off" else "off"
                    self.toggle_states[uid] = new_state
                    self.status_text.set(f"トグルボタン {asset.name}: {new_state}")
                if asset.button_se:
                    se_info = self.audio["se"].get(asset.button_se)
                    if se_info:
                        self.audio_player.play("se", asset.button_se, Path(se_info["file"]))
                    else:
                        messagebox.showwarning(APP_NAME,
                            f"トグルボタン {asset.name} のSE '{asset.button_se}' が未登録です。", parent=self)
        if asset is not None and asset.kind in ("button", "button_uv"):
            hit = self._hit_asset("preview", event.x, event.y)
            if hit is not None and hit.uid == uid:
                self._run_button_scroll_events(asset)
        self.pressed_button_uid = None
        self._button_pointer_inside = False
        self._preview_visibility_changed()

    def _tick(self) -> None:
        self._tick_job = None
        now = time.monotonic()
        for asset in self.assets:
            if asset.uid not in self.playing or asset.movement is None:
                continue
            duration = max(1, asset.movement.duration)
            frame = int((now - self.playing[asset.uid]) * getattr(asset.movement, "fps", DEFAULT_FPS))
            if frame >= duration:
                if asset.movement.loop:
                    frame %= duration
                else:
                    frame = duration - 1
                    self.playing.pop(asset.uid, None)
            self.last_frame[asset.uid] = frame
        self.request_draw("preview")
        if self.playing:
            self._tick_job = self.after(TICK_MILLISECONDS, self._tick)

    def stop_animations(self) -> None:
        if self._tick_job is not None:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        self.playing.clear()
        self.last_frame.clear()
        self.pressed_button_uid = None

    def delete_selected(self) -> None:
        targets = {uid for uid in self.selected_uids if self._asset_by_uid(uid) is not None}
        if not targets:
            return
        count = len(targets)
        self._push_undo()
        for uid in targets:
            if self._asset_by_uid(uid) is not None:
                self._remove_asset_tree(uid)
        self._repair_toggle_groups()
        self._drag_static_cache = None
        self._refresh_event_windows()
        self.select(None)
        self.refresh_tree()
        self.request_draw("both")
        self.status_text.set(f"{count} 個のアセットを削除しました（Ctrl+Zでまとめて取り消し可能）。")

    def _editing_text_field(self) -> bool:
        """Allow Ctrl+C/Ctrl+V to retain their native semantics in text controls."""
        focused = self.focus_get()
        return isinstance(focused, (tk.Entry, tk.Text, tk.Spinbox,
                                    ttk.Entry, ttk.Combobox, ttk.Spinbox))

    def copy_selected(self, _event: Any = None, *, force: bool = False) -> Optional[str]:
        """Copy a scene asset including its UVs, animation, transform, and SE link."""
        if not force and self._editing_text_field():
            return None  # Do not hijack the native text clipboard.
        asset = self.get_selected()
        if asset is None:
            self.status_text.set("コピーするアセットを選択してください。")
            return "break"
        self._asset_clipboard = self._asset_state_copy(asset)
        group=self._scroll_group_of(asset.uid)
        self._asset_clipboard_scroll_group=group.name if group else None
        self._asset_clipboard.event_owner_uid = None
        self._asset_clipboard.toggle_group = None
        self._clipboard_descendants = {a.uid: self._asset_state_copy(a) for a in self.assets}
        self._asset_clipboard.raster_cache = {}
        self._paste_count = 0
        self.status_text.set(f"アセットをコピーしました: {asset.name}（Ctrl+Vで配置）")
        return "break"

    def paste_asset(self, _event: Any = None, *, force: bool = False) -> Optional[str]:
        """Paste with a unique asset name and one undo step per paste.

        Positions are offset per paste, keeping the original scene-layer order
        intact, and image/model data are not duplicated in memory.
        """
        if not force and self._editing_text_field():
            return None
        original = self._asset_clipboard
        if original is None:
            self.status_text.set("先にアセットをCtrl+Cでコピーしてください。")
            return "break"
        if len(self.assets) >= MAX_ASSETS:
            messagebox.showwarning(APP_NAME, "アセット数が上限に達しました。", parent=self)
            return "break"
        try:
            ensure_asset_path(original.source)
            if original.pressed_source is not None:
                ensure_asset_path(original.pressed_source)
            clone = self._asset_state_copy(original)
            clone.uid = uuid.uuid4().hex
            clone.event_owner_uid = None
            clone.toggle_group = None
            clone.raster_cache = {}
            used = {asset.name for asset in self.assets if asset.category == clone.category}
            serial = 2
            while f"{original.name}_{serial}" in used:
                serial += 1
            clone.name = f"{original.name}_{serial}"
            # Progressively offset repeated pastes, while retaining original transforms.
            next_paste = self._paste_count + 1
            clone.px = original.px + 32.0 * next_paste
            clone.py = original.py - 32.0 * next_paste
            self._push_undo()
            self.assets.append(clone)
            if self._asset_clipboard_scroll_group in self.scroll_groups:
                self.scroll_groups[self._asset_clipboard_scroll_group].members.add(clone.uid)
            if clone.kind == "toggle_uv":
                self.toggle_states[clone.uid] = clone.toggle_default
                self._clone_event_descendants(original, clone, self._clipboard_descendants,
                                              32.0 * next_paste)
            self._paste_count = next_paste
            self._drag_static_cache = None
            self.refresh_tree()
            self.select(clone.uid)
            self.request_draw("both")
            self.status_text.set(f"アセットを貼り付けました: {clone.name}")
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"アセットの貼り付けに失敗しました:\n{exc}", parent=self)
        return "break"

    def duplicate_selected(self) -> None:
        asset = self.get_selected()
        if asset is None or len(self.assets) >= MAX_ASSETS:
            return
        duplicated = self._asset_state_copy(asset)
        duplicated.uid = uuid.uuid4().hex
        duplicated.event_owner_uid = asset.event_owner_uid
        duplicated.toggle_group = None
        duplicated.raster_cache = {}
        duplicated.px += 32
        duplicated.py -= 32
        used = {a.name for a in self.assets if a.category == asset.category}
        n = 2
        while f"{asset.name}_{n}" in used:
            n += 1
        duplicated.name = f"{asset.name}_{n}"
        self._push_undo()
        if duplicated.event_owner_uid:
            owner = self._asset_by_uid(duplicated.event_owner_uid)
            if owner is not None:
                owner.events.append({"type": "show", "asset_uid": duplicated.uid})
        self.assets.append(duplicated)
        if duplicated.event_owner_uid is None:
            group=self._scroll_group_of(asset.uid)
            if group is not None:
                group.members.add(duplicated.uid)
        if duplicated.kind == "toggle_uv":
            self.toggle_states[duplicated.uid] = duplicated.toggle_default
            self._clone_event_descendants(asset, duplicated,
                                          {a.uid: a for a in self.assets}, 32)
        self._refresh_event_windows()
        self.refresh_tree()
        self.select(duplicated.uid)
        self.request_draw("both")

    def move_layer(self, front: bool) -> None:
        asset = self.get_selected()
        if asset is None:
            return
        ordered = [item.uid for item in reversed(self.assets)]
        ordered.remove(asset.uid)
        ordered.insert(0 if front else len(ordered), asset.uid)
        self._apply_asset_stack_order(ordered, moved_uid=asset.uid)
        self.tree.see(asset.uid)

    def apply_background_flags(self) -> None:
        values = (self.bg_critical_var.get(), self.bg_prefetch_var.get())
        if (self.background_critical, self.background_prefetch) != values:
            self._push_undo()
            self.background_critical, self.background_prefetch = values

    def pick_background(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self, title="背景画像を選択",
            initialdir=self._dialog_initial_dir("background"),
            filetypes=[("画像", "*.png *.jpg *.jpeg"), ("すべて", "*.*")])
        if not chosen:
            return
        try:
            path = ensure_asset_path(Path(chosen))
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        self._remember_dialog_file("background", path)
        try:
            with Image.open(path) as source:
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValueError("画像の画素数が上限を超えています。")
                image = source.convert("RGBA")
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            messagebox.showerror(APP_NAME, f"背景を読み込めませんでした:\n{exc}", parent=self)
            return
        self._push_undo()
        self.background_file, self.background_image = path, image
        self._background_cache.clear()
        self.request_draw("both")
        self.status_text.set(f"背景を設定しました: {path.name}")

    def remove_background(self) -> None:
        if self.background_file is None and self.background_image is None:
            return
        self._push_undo()
        self.background_file = None
        self.background_image = None
        self._background_cache.clear()
        self.request_draw("both")

    def _place_child_window(self, child: tk.Toplevel, parent: tk.Misc,
                            size: tuple[int, int],
                            remembered: Optional[tuple[int, int]] = None) -> None:
        """Center first opening over its owner, without snapping to a screen edge."""
        parent.update_idletasks()
        width, height = size
        if remembered is None:
            left = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            top = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        else:
            left, top = remembered
        # Clamp when the parent is on the primary display; do not forcibly move
        # dialogs to the primary screen if the editor is on another monitor.
        if 0 <= parent.winfo_rootx() < parent.winfo_screenwidth():
            left = max(0, min(left, max(0, parent.winfo_screenwidth() - width)))
        if 0 <= parent.winfo_rooty() < parent.winfo_screenheight():
            top = max(0, min(top, max(0, parent.winfo_screenheight() - height)))
        child.geometry(f"{width}x{height}+{left}+{top}")

    def rename_audio(self, kind: str, old_key: str, new_key: str) -> bool:
        """Rename an audio entry atomically, updating flags and all linked SE refs."""
        if kind not in ("bgm", "se") or old_key not in self.audio[kind]:
            raise ValueError("名前を変更する音声が見つかりません。")
        key = safe_asset_key(new_key)
        if key == old_key:
            return False
        if key in self.audio[kind]:
            raise ValueError(f"{kind}.{key} は登録済みです。")
        self._push_undo()
        self.audio[kind] = {
            (key if existing == old_key else existing): record
            for existing, record in self.audio[kind].items()
        }
        old_ref, new_ref = f"{kind}.{old_key}", f"{kind}.{key}"
        if old_ref in self.audio_flags:
            self.audio_flags[new_ref] = self.audio_flags.pop(old_ref)
        if kind == "se":
            for asset in self.assets:
                if asset.kind == "animation" and asset.animation_se == old_key:
                    asset.animation_se = key
                if asset.kind in ("button_uv", "button", "toggle_uv") and asset.button_se == old_key:
                    asset.button_se = key
        self._refresh_animation_se_choices()
        self.status_text.set(f"音声名を変更しました: {old_ref} → {new_ref}")
        return True

    def edit_audio(self) -> None:
        if self._audio_window is not None and self._audio_window.winfo_exists():
            self._audio_window.deiconify()
            self._audio_window.lift()
            self._audio_window.focus_set()
            return
        window = tk.Toplevel(self)
        self._audio_window = window
        window.title("BGM / SE の管理・試聴")
        window.minsize(580, 350)
        window.transient(self)
        self._place_child_window(window, self, AUDIO_MANAGER_SIZE, self._audio_window_xy)

        def on_close() -> None:
            if window.winfo_exists():
                self._audio_window_xy = (window.winfo_x(), window.winfo_y())
                window.destroy()
            self._audio_window = None
            self._audio_refresh = None

        window.protocol("WM_DELETE_WINDOW", on_close)
        self._audio_refresh = None
        # Also clear the reference when the parent is destroyed.
        window.bind("<Destroy>", lambda event: setattr(self, "_audio_window", None)
                    if event.widget is window else None)
        header = ttk.Frame(window, padding=10)
        header.pack(fill="x")
        ttk.Label(header, text="音声を登録して再生・停止できます。BGMは繰り返し、SEは1回再生します。",
                  wraplength=690).pack(anchor="w")
        table = ttk.Treeview(window, columns=("kind", "key", "file", "critical", "prefetch"),
                             show="headings", height=10, selectmode="browse")
        for col, title, width in (("kind", "種別", 65), ("key", "名前", 115), ("file", "ファイル", 280),
                                  ("critical", "critical", 65), ("prefetch", "prefetch", 65)):
            table.heading(col, text=title)
            table.column(col, width=width)
        table.pack(fill="both", expand=True, padx=10)
        check_critical = tk.BooleanVar(value=False)
        check_prefetch = tk.BooleanVar(value=False)

        def refresh(focus: Optional[str] = None) -> None:
            table.delete(*table.get_children())
            for kind, collection in self.audio.items():
                for key, item in collection.items():
                    cr, pf = self.audio_flags.get(f"{kind}.{key}", (False, False))
                    table.insert("", "end", iid=f"{kind}.{key}", values=(
                        kind, key, item["file"], "○" if cr else "", "○" if pf else ""))
            if focus and table.exists(focus):
                table.selection_set(focus)
                table.see(focus)
                select_audio()
            self._refresh_animation_se_choices()

        self._audio_refresh = refresh

        def select_audio(_event: Any = None) -> None:
            chosen = table.selection()
            cr, pf = self.audio_flags.get(chosen[0], (False, False)) if chosen else (False, False)
            check_critical.set(cr)
            check_prefetch.set(pf)

        def apply_audio_flags() -> None:
            chosen = table.selection()
            if chosen:
                updated = (check_critical.get(), check_prefetch.get())
                if self.audio_flags.get(chosen[0], (False, False)) != updated:
                    self._push_undo()
                    self.audio_flags[chosen[0]] = updated
                refresh(chosen[0])

        table.bind("<<TreeviewSelect>>", select_audio)

        def add() -> None:
            type_window = tk.Toplevel(window)
            type_window.title("音声の追加")
            type_window.transient(window)
            self._place_child_window(type_window, window, AUDIO_ADD_SIZE)
            type_window.grab_set()
            kind_var = tk.StringVar(value="se")
            name_var = tk.StringVar(value="confirm")
            ttk.Label(type_window, text="種別").pack(anchor="w", padx=12, pady=(10, 0))
            kind_combo = ttk.Combobox(type_window, textvariable=kind_var, values=("bgm", "se"),
                                      state="readonly")
            kind_combo.pack(fill="x", padx=12)
            kind_combo.bind("<<ComboboxSelected>>", lambda _event: name_var.set(
                "main" if kind_var.get() == "bgm" else "confirm"))
            ttk.Label(type_window, text="名前（アニメーションとの関連付けに使用）").pack(
                anchor="w", padx=12, pady=(8, 0))
            ttk.Entry(type_window, textvariable=name_var).pack(fill="x", padx=12)

            def choose() -> None:
                try:
                    name = safe_asset_key(name_var.get())
                    kind = kind_var.get()
                    if kind not in ("bgm", "se"):
                        raise ValueError("音声種別が不正です。")
                    if name in self.audio[kind]:
                        raise ValueError(f"{kind}.{name} は登録済みです。")
                except ValueError as exc:
                    messagebox.showerror(APP_NAME, str(exc), parent=type_window)
                    return
                selected = filedialog.askopenfilename(
                    parent=type_window, title="音声ファイルを選択",
                    initialdir=self._dialog_initial_dir("audio"),
                    filetypes=[("音声", "*.hca *.wav *.mp3 *.ogg"), ("すべて", "*.*")])
                if not selected:
                    return
                try:
                    path = ensure_asset_path(Path(selected))
                except ValueError as exc:
                    messagebox.showerror(APP_NAME, str(exc), parent=type_window)
                    return
                if path.suffix.casefold() not in AUDIO_EXTENSIONS:
                    messagebox.showerror(APP_NAME, "対応形式: WAV / MP3 / OGG / HCA", parent=type_window)
                    return
                self._remember_dialog_file("audio", path)
                self._push_undo()
                self.audio[kind][name] = {"file": str(path)}
                self.audio_flags[f"{kind}.{name}"] = (False, False)
                refresh(f"{kind}.{name}")
                type_window.destroy()

            ttk.Button(type_window, text="ファイルを選択して追加", command=choose).pack(padx=12, pady=13)

        def rename_selected() -> None:
            chosen = table.selection()
            if not chosen:
                messagebox.showinfo(APP_NAME, "名前を変更する音声を選択してください。", parent=window)
                return
            kind, old_key = chosen[0].split(".", 1)
            requested = simpledialog.askstring(
                APP_NAME, f"{kind}.{old_key} の新しい名前を入力してください。\nファイル名は変更されません。",
                initialvalue=old_key, parent=window)
            if requested is None:
                return
            try:
                if self.rename_audio(kind, old_key, requested):
                    refresh(f"{kind}.{requested.strip()}")
            except ValueError as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=window)

        def remove() -> None:
            chosen = table.selection()
            if not chosen:
                return
            full = chosen[0]
            category, key = full.split(".", 1)
            self._push_undo()
            self.audio[category].pop(key, None)
            self.audio_flags.pop(full, None)
            if category == "se":
                # Do not save dangling links when a sound is deleted.
                for asset in self.assets:
                    if asset.kind == "animation" and asset.animation_se == key:
                        asset.animation_se = None
                    if asset.kind in ("button_uv", "button", "toggle_uv") and asset.button_se == key:
                        asset.button_se = None
            self.audio_player.stop(category)
            refresh()

        def play_selected(_event: Any = None) -> None:
            chosen = table.selection()
            if not chosen:
                messagebox.showinfo(APP_NAME, "音声を選択してください。", parent=window)
                return
            category, key = chosen[0].split(".", 1)
            record = self.audio[category].get(key)
            if record:
                self.audio_player.play(category, key, Path(record["file"]))

        flag_row = ttk.Frame(window, padding=(10, 5))
        flag_row.pack(fill="x")
        ttk.Label(flag_row, text="選択音声:").pack(side="left")
        ttk.Checkbutton(flag_row, text="critical", variable=check_critical,
                        command=apply_audio_flags).pack(side="left", padx=12)
        ttk.Checkbutton(flag_row, text="prefetch", variable=check_prefetch,
                        command=apply_audio_flags).pack(side="left", padx=12)
        table.bind("<Double-1>", play_selected)
        table.bind("<F2>", lambda _event: rename_selected())
        buttons = ttk.Frame(window, padding=10)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="追加", command=add).pack(side="left", padx=2)
        ttk.Button(buttons, text="名前変更", command=rename_selected).pack(side="left", padx=2)
        ttk.Button(buttons, text="削除", command=remove).pack(side="left", padx=2)
        ttk.Button(buttons, text="選択音声を再生", command=play_selected).pack(side="left", padx=(16, 2))
        ttk.Button(buttons, text="BGM停止", command=lambda: self.audio_player.stop("bgm")).pack(side="left", padx=2)
        ttk.Button(buttons, text="SE停止", command=lambda: self.audio_player.stop("se")).pack(side="left", padx=2)
        ttk.Button(buttons, text="閉じる", command=on_close).pack(side="right")
        refresh()

    def _serialize_asset_entry(self, asset: Asset, lookup: Mapping[str, Asset],
                               visiting: frozenset[str] = frozenset()) -> dict[str, Any]:
        if asset.uid in visiting:
            raise ValueError("イベント追加アセットに循環参照があります。")
        transform = {"px": json_number(asset.px), "py": json_number(asset.py),
                     "sx": json_number(asset.sx), "sy": json_number(asset.sy),
                     "rotate": json_number(asset.rotation)}
        if asset.flip_x:
            transform["flip_x"] = True
        if asset.flip_y:
            transform["flip_y"] = True
        if asset.kind == "animation":
            entry: dict[str, Any] = {"file": reference_name(asset.source),
                                     "key": asset.key, **transform}
            if asset.animation_se:
                if asset.animation_se not in self.audio["se"]:
                    raise ValueError(f"{asset.name}: SE '{asset.animation_se}' が未登録です。")
                entry["se"] = asset.animation_se
        elif asset.kind in ("button", "button_uv", "toggle_uv"):
            if asset.kind == "button":
                idle = atlas_sprite_state(asset.source, asset.key)
                pressed = atlas_sprite_state(asset.source, asset.pressed_key or asset.key)
            else:
                idle = png_uv_state(asset.source, asset.uv_rect)
                pressed = png_uv_state(asset.pressed_source or asset.source,
                                       asset.pressed_uv_rect)
            if asset.kind == "toggle_uv":
                if asset.toggle_default not in ("off", "on"):
                    raise ValueError(f"{asset.name}: 初期状態が不正です。")
                entry = {**transform, "default": asset.toggle_default,
                         "state": {"off": idle, "on": pressed}}
                if asset.toggle_group:
                    entry["group"] = asset.toggle_group
                events: list[dict[str, Any]] = []
                for event in asset.events:
                    if event["type"] == "hide":
                        target = lookup.get(event["target_uid"])
                        if target is None or target.event_owner_uid is not None:
                            raise ValueError(f"{asset.name}: 非表示対象が存在しません。")
                        events.append({"type": "hide", "target": target.full_key})
                    elif event["type"] == "show_scroll_group":
                        name = event["group_name"]
                        group = self.scroll_groups.get(name)
                        if group is None:
                            raise ValueError(f"{asset.name}: 表示対象グループ '{name}' が存在しません。")
                        if asset.uid in group.members:
                            raise ValueError(f"{asset.name}: 自身の所属グループは表示制御できません。")
                        events.append({"type": "show_scroll_group", "group": name})
                    elif event["type"] == "show":
                        child = lookup.get(event["asset_uid"])
                        if child is None or child.event_owner_uid != asset.uid:
                            raise ValueError(f"{asset.name}: 追加表示アセットが存在しません。")
                        group = ("animation" if child.kind == "animation" else
                                 "toggle button" if child.kind == "toggle_uv" else
                                 "button" if child.kind in ("button", "button_uv") else
                                 "window" if child.kind == "window" else "image")
                        events.append({"type": "show", "asset": {
                            "group": group, "name": child.name,
                            "layer": next(i for i, item in enumerate(self.assets) if item.uid == child.uid),
                            "data": self._serialize_asset_entry(child, lookup, visiting | {asset.uid}),
                            "critical": child.critical, "prefetch": child.prefetch}})
                    else:
                        raise ValueError(f"{asset.name}: 未対応のイベント種別です。")
                if events:
                    entry["events"] = events
            else:
                entry = {**transform, "state": {"idle": idle, "on push": pressed}}
                if asset.events:
                    command_events = []
                    for event in asset.events:
                        kind = event["type"]
                        if kind not in ("show_scroll_group", "hide_scroll_group", "toggle_scroll_group"):
                            raise ValueError(f"{asset.name}: 通常ボタンのイベント種別が不正です。")
                        name = event["group_name"]
                        group = self.scroll_groups.get(name)
                        if group is None:
                            raise ValueError(f"{asset.name}: 表示対象グループ '{name}' がありません。")
                        if asset.uid in group.members and kind != "hide_scroll_group":
                            raise ValueError(f"{asset.name}: 自身の所属グループは表示・切替できません。")
                        command_events.append({"type": kind, "group": name})
                    entry["events"] = command_events
            if asset.button_se:
                if asset.button_se not in self.audio["se"]:
                    raise ValueError(f"{asset.name}: SE '{asset.button_se}' が未登録です。")
                entry["se"] = asset.button_se
        elif asset.kind == "window":
            l, r, t, b = asset.window_slices
            entry = {"file": reference_name(asset.source), **transform,
                     "size": {"width": asset.window_width, "height": asset.window_height},
                     "slice": {"left": l, "right": r, "top": t, "bottom": b},
                     "fill": asset.window_mode,
                     "alpha": "source"}
        elif asset.kind == "image":
            if asset.source.suffix.casefold() == ".plist":
                image = atlas_sprite_state(asset.source, asset.key)
            else:
                image = {"file": reference_name(asset.source)}
                if asset.uv_rect is not None:
                    image["uv"] = png_uv_state(asset.source, asset.uv_rect)["uv"]
            entry = {**image, **transform}
        else:
            raise ValueError(f"未対応のアセット種別: {asset.kind}")
        return entry

    def serialize(self, output_path: Path) -> dict[str, Any]:
        """Pre-release schema 1 with nested toggle events and numeric coordinates."""
        scene = safe_scene_name(self.scene_name.get())
        audio_output: dict[str, dict[str, Any]] = {"bgm": {}, "se": {}}
        for kind, collection in self.audio.items():
            for name, record in collection.items():
                audio_output[kind][name] = {"file": reference_name(Path(record["file"]))}
        background: dict[str, Any] = {}
        if self.background_file is not None:
            background["main"] = {"file": reference_name(self.background_file),
                                  "sha256": digest_file(self.background_file)}
        ui: dict[str, dict[str, Any]] = {"button": {}, "image": {}, "toggle button": {}, "window": {}}
        animations: dict[str, Any] = {}
        critical: list[str] = []
        prefetch: list[str] = []
        if background and self.background_critical:
            critical.append("background.main")
        if background and self.background_prefetch:
            prefetch.append("background.main")
        for category, items in audio_output.items():
            for key in items:
                flags = self.audio_flags.get(f"{category}.{key}", (False, False))
                if flags[0]: critical.append(f"{category}.{key}")
                if flags[1]: prefetch.append(f"{category}.{key}")
        lookup = {a.uid: a for a in self.assets}
        main_assets = [a for a in self.assets if a.event_owner_uid is None]
        validate_toggle_groups(self.assets)
        validate_scroll_toggle_events(self.assets, self.scroll_groups)
        for asset in main_assets:
            entry = self._serialize_asset_entry(asset, lookup)
            if asset.kind == "animation":
                animations[asset.name] = entry
            else:
                group = ("toggle button" if asset.kind == "toggle_uv" else
                         "button" if asset.kind in ("button_uv", "button") else
                         "window" if asset.kind == "window" else "image")
                ui[group][asset.name] = entry
            if asset.critical: critical.append(asset.full_key)
            if asset.prefetch: prefetch.append(asset.full_key)
        scroll_json: dict[str, Any]={}
        used_members: set[str]=set()
        for key,group in self.scroll_groups.items():
            safe_asset_key(key)
            if not 1 <= group.width <= DESIGN_W*4 or not 1 <= group.height <= DESIGN_H*4:
                raise ValueError(f"{key}: スクロール表示範囲が不正です。")
            member_refs=[]
            for item in self.assets:
                if item.uid not in group.members:
                    continue
                if item.uid in used_members:
                    raise ValueError(f"{item.name}: 複数スクロールグループには所属できません。")
                used_members.add(item.uid)
                member_refs.append(item.full_key)
            scroll_json[key]={"px":json_number(group.px),"py":json_number(group.py),
                              "width":json_number(group.width),"height":json_number(group.height),
                              "scroll_x":bool(group.scroll_x),"scroll_y":bool(group.scroll_y),
                              "members":member_refs}
        return {"version": 1, "scene": scene,
                **({"scroll_groups":scroll_json} if scroll_json else {}),
                "assets": {**audio_output, "background": background,
                           "ui": ui, "animation": animations},
                "asset_order": [asset.full_key for asset in main_assets],
                "critical": critical, "prefetch": prefetch}

    def _confirm_scene_overwrite(self, path: Path) -> bool:
        """Protect an existing target unless it is the current saved scene."""
        return (not path.exists() or self.saved_path == path
                or messagebox.askyesno(APP_NAME,
                    f"{path.name} は既に存在します。上書きしますか？\n{path}", parent=self))

    def save_scene(self) -> None:
        """Ctrl+S: write <scene>.json directly to ./out/scene without a dialog."""
        try:
            target = scene_output_path(self.scene_name.get())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        if not self._confirm_scene_overwrite(target):
            return
        self._write_json(target)

    def save_as_scene(self) -> None:
        """Ctrl+Shift+S: choose a new scene *name*, never another folder."""
        current_name = self.scene_name.get()
        proposed = simpledialog.askstring(
            APP_NAME, "別名保存するシーン名を入力してください。\n保存先: ./out/scene/",
            initialvalue=current_name, parent=self)
        if proposed is None:
            return
        try:
            name = safe_scene_name(proposed)
            target = scene_output_path(name)
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        if not self._confirm_scene_overwrite(target):
            return
        self.scene_name.set(name)
        if not self._write_json(target):
            self.scene_name.set(current_name)

    def _write_json(self, path: Path) -> bool:
        try:
            directory = prepare_scene_output_dir()
            expected = scene_output_path(self.scene_name.get())
            # Also enforce the fixed destination at the lowest-level writer.
            if path.resolve() != expected.resolve() or path.parent.resolve() != directory:
                raise ValueError("シーンJSONの保存先は ./out/scene/<シーン名>.json に固定されています。")
            payload = self.serialize(path)
            # Write atomically to avoid leaving a partially written scene.
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as out:
                    json.dump(payload, out, ensure_ascii=False, indent=2, allow_nan=False)
                    out.write("\n")
                    out.flush()
                    os.fsync(out.fileno())
                temporary.replace(path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            self.saved_path = path
            ui_count = sum(len(part) for part in payload["assets"]["ui"].values())
            self.status_text.set(f"保存しました: {path}　(UI {ui_count}件 / 動作 {len(payload['assets']['animation'])}件)")
            return True
        except (OSError, ValueError, OverflowError) as exc:
            messagebox.showerror(APP_NAME, f"保存に失敗しました:\n{exc}", parent=self)
            return False

    def new_scene(self) -> None:
        if (self.assets or self.scroll_groups or self.background_file or any(self.audio.values())) and not messagebox.askyesno(
                APP_NAME, "現在のシーンを破棄して新規作成しますか？", parent=self):
            return
        self.stop_animations()
        self.audio_player.stop()
        self._undo_stack.clear()
        self._close_event_windows()
        if self._scroll_workspace is not None and self._scroll_workspace.winfo_exists():
            self._scroll_workspace.close()
        self._drag_static_cache = None
        self._background_cache.clear()
        self.assets.clear()
        self.scroll_groups.clear()
        self.selected_scroll_group_name = None
        self.scroll_offsets.clear()
        self._scroll_press=None
        self.editor_hidden_uids.clear()
        self.toggle_states.clear()
        self.button_scroll_states.clear()
        self.audio = {"bgm": {}, "se": {}}
        self.audio_flags.clear()
        self.background_file = None
        self.background_image = None
        self.background_critical = True
        self.background_prefetch = False
        self.bg_critical_var.set(True)
        self.bg_prefetch_var.set(False)
        self.saved_path = None
        self.scene_name.set(DEFAULT_SCENE)
        self.select(None)
        self.refresh_tree()
        self.request_draw("both")

    def open_scene(self) -> None:
        filename = filedialog.askopenfilename(
            parent=self, title="シーン JSON を開く",
            initialdir=self._dialog_initial_dir("scene"),
            filetypes=[("シーン JSON", "*.json"), ("すべて", "*.*")])
        if not filename:
            return
        path = Path(filename).resolve()
        try:
            if path.stat().st_size > MAX_SCENE_JSON_BYTES:
                raise ValueError("シーンJSONのサイズ上限を超えています。")
            with path.open("r", encoding="utf-8-sig") as reader:
                root = json.load(reader)
            if not isinstance(root, dict) or type(root.get("version")) is not int or root["version"] not in (1, 3):
                raise ValueError("version: 1 のシーンJSONが必要です（以前のversion: 3も読み込み可能）。")
            strict_numeric = root["version"] == 1
            name = safe_scene_name(root.get("scene", ""))
            resources = root.get("assets")
            if not isinstance(resources, dict):
                raise ValueError("assets オブジェクトがありません。")
            critical = set(root.get("critical", []))
            prefetch = set(root.get("prefetch", []))
            if not isinstance(root.get("critical", []), list) or not isinstance(root.get("prefetch", []), list):
                raise ValueError("critical / prefetch は配列にしてください。")
            main_assets, warnings = read_new_scene_assets(resources, critical,
                                                          prefetch, path.parent,
                                                          strict_numeric=strict_numeric)
            new_assets, event_warnings = read_toggle_events(resources, main_assets,
                                                             path.parent, strict_numeric=strict_numeric)
            warnings.extend(event_warnings)
            layer_order = root.get("asset_order")
            if layer_order is not None:
                if (not isinstance(layer_order, list)
                        or any(not isinstance(ref, str) for ref in layer_order)
                        or len(layer_order) != len(set(layer_order))):
                    raise ValueError("asset_order は重複のない参照名の配列にしてください。")
                priority = {ref: index for index, ref in enumerate(layer_order)}
                if set(priority) != {asset.full_key for asset in main_assets}:
                    raise ValueError("asset_order と配置アセットが一致しません。")
                main_assets.sort(key=lambda asset: priority[asset.full_key])
                # Restore full stacking order: main assets via asset_order,
                # nested event assets via their absolute layer index.
                children = [asset for asset in new_assets if asset.event_owner_uid is not None]
                ordered: list[Optional[Asset]] = [None] * len(new_assets)
                for child in children:
                    if child.event_layer is None:
                        continue  # Older event JSONs can omit layer; place in remaining slots.
                    if child.event_layer >= len(ordered) or ordered[child.event_layer] is not None:
                        raise ValueError("イベントアセットのレイヤー番号が重複または範囲外です。")
                    ordered[child.event_layer] = child
                remaining = iter(main_assets + [a for a in children if a.event_layer is None])
                new_assets = [item if item is not None else next(remaining) for item in ordered]
            if layer_order is None:
                children = [a for a in new_assets if a.event_owner_uid is not None]
                if children and all(a.event_layer is not None for a in children):
                    ordered: list[Optional[Asset]] = [None] * len(new_assets)
                    for child in children:
                        if child.event_layer >= len(ordered) or ordered[child.event_layer] is not None:
                            raise ValueError("イベントアセットのレイヤー番号が重複または範囲外です。")
                        ordered[child.event_layer] = child
                    roots = iter(main_assets)
                    new_assets = [a if a is not None else next(roots) for a in ordered]
            loaded_scroll_groups = read_scroll_groups(root.get("scroll_groups"), new_assets,
                                                       strict_numeric=strict_numeric)
            validate_scroll_toggle_events(new_assets, loaded_scroll_groups)
            new_audio: dict[str, dict[str, dict[str, str]]] = {"bgm": {}, "se": {}} 
            new_flags = {}
            for category in ("bgm", "se"):
                entries = resources.get(category, {})
                if not isinstance(entries, dict):
                    raise ValueError(f"{category} はオブジェクトにしてください。")
                for key, record in entries.items():
                    safe_asset_key(key)
                    resolved = resolve_reference(record["file"], path.parent)
                    new_audio[category][key] = {"file": str(resolved)}
                    new_flags[f"{category}.{key}"] = (
                        f"{category}.{key}" in critical, f"{category}.{key}" in prefetch)
            for asset in new_assets:
                linked_se = asset.animation_se if asset.kind == "animation" else (
                    asset.button_se if asset.kind in ("button_uv", "button", "toggle_uv") else None)
                if linked_se and linked_se not in new_audio["se"]:
                    warnings.append(f"{asset.full_key}: SE '{linked_se}' が未登録です。")
            new_bg: Optional[Path] = None
            bg_image: Optional[Image.Image] = None
            background = resources.get("background", {})
            if not isinstance(background, dict):
                raise ValueError("background はオブジェクトにしてください。")
            if "main" in background:
                entry = background["main"]
                new_bg = resolve_reference(entry["file"], path.parent)
                try:
                    with Image.open(new_bg) as source:
                        if source.width * source.height > MAX_IMAGE_PIXELS:
                            raise ValueError("背景画像が画素上限を超えています。")
                        bg_image = source.convert("RGBA")
                    expected_hash = entry.get("sha256")
                    if expected_hash and digest_file(new_bg).lower() != str(expected_hash).lower():
                        warnings.append("背景画像のSHA-256がJSONと一致しません。")
                except (OSError, ValueError) as exc:
                    warnings.append(f"背景: {exc}")
            self.stop_animations()
            self.audio_player.stop()
            self._undo_stack.clear()
            self._close_event_windows()
            if self._scroll_workspace is not None and self._scroll_workspace.winfo_exists():
                self._scroll_workspace.close()
            self._drag_static_cache = None
            self._background_cache.clear()
            self.assets = new_assets
            self.scroll_groups = loaded_scroll_groups
            self.selected_scroll_group_name = None
            self._scroll_create_mode = False
            self.scroll_create_button.configure(text="ドラッグで範囲作成")
            self.scroll_offsets.clear()
            self._scroll_press=None
            self.editor_hidden_uids.clear()
            self._reset_toggle_states()
            self.audio, self.audio_flags = new_audio, new_flags
            self.background_file, self.background_image = new_bg, bg_image
            self.background_critical = "background.main" in critical
            self.background_prefetch = "background.main" in prefetch
            self.bg_critical_var.set(self.background_critical)
            self.bg_prefetch_var.set(self.background_prefetch)
            self.scene_name.set(name)
            target_path = scene_output_path(name)
            self.saved_path = path if path == target_path else None
            self._remember_dialog_file("scene", path)
            self._remember_loaded_asset_dirs(new_assets, new_bg, new_audio)
            self.select(None)
            self.refresh_tree()
            self.request_draw("both")
            self.status_text.set(f"読み込みました: {path.name}　アセット {len(new_assets)} 件")
            if warnings:
                messagebox.showwarning(APP_NAME, "読み込みに関する注意事項:\n" +
                    "\n".join(warnings[:15]) + (f"\nほか {len(warnings)-15} 件" if len(warnings) > 15 else ""), parent=self)
        except (OSError, TypeError, KeyError, ValueError, json.JSONDecodeError, MemoryError) as exc:
            messagebox.showerror(APP_NAME, f"シーンの読み込みに失敗しました:\n{exc}", parent=self)

    def quit_app(self) -> None:
        self._close_event_windows()
        if self._scroll_workspace is not None and self._scroll_workspace.winfo_exists():
            self._scroll_workspace.close()
        self.stop_animations()
        self._clear_drag_visual()
        self._drag_static_cache = None
        self.audio_player.shutdown()
        for job in (self._startup_draw_job, self._draw_job):
            if job is not None:
                self.after_cancel(job)
        self._startup_draw_job = None
        self._draw_job = None
        self.destroy()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print("シーンUIエディター: 編集画面のアセット非表示・独立した位置/等間隔スナップ / JSON version 1")
        return 0
    app = SceneEditor()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
