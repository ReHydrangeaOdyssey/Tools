#!/usr/bin/env python3
"""Single-file TexturePacker / Cocos Studio viewer, v26 (Python 3.9+).

Cocos coordinate Y points upward; Pillow image Y points downward.
Companion texture atlases are resolved from ExportJson metadata automatically.
"""

from __future__ import annotations

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
from tkinter import filedialog, messagebox, ttk
from typing import Any, Mapping, Optional, Sequence

from PIL import Image, ImageTk, UnidentifiedImageError


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


class Viewer(tk.Tk):
    def __init__(self, plist_path: Optional[Path], image_path: Optional[Path], packed_rotation: str) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(WINDOW_SIZE)
        self.minsize(860, 540)
        self.atlas: Optional[Atlas] = None
        self.selected: Optional[Frame] = None
        self.list_frames: list[Frame] = []
        self.atlas_photo: Optional[ImageTk.PhotoImage] = None
        self.preview_photo: Optional[ImageTk.PhotoImage] = None
        self.zoom = 1.0
        self.overlay_var = tk.BooleanVar(value=True)
        self.label_var = tk.BooleanVar(value=False)
        self.search_var = tk.StringVar()
        self.rotation_var = tk.StringVar(value=packed_rotation)
        self.info_var = tk.StringVar(value=".plist ファイルを開いてください")
        self.status_var = tk.StringVar(value="準備完了")
        self._build_ui()
        if plist_path is not None:
            self.after_idle(lambda: self.open_plist(plist_path, image_path))

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=6)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="PLISTを開く", command=self.ask_plist).pack(side="left")
        ttk.Button(toolbar, text="ExportJsonを開く", command=self.ask_exportjson).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="画像を指定", command=self.ask_image).pack(side="left", padx=(6, 6))
        ttk.Button(toolbar, text="読み込み結果", command=self.show_load_report).pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="設定を表示・コピー", command=self.show_current_settings).pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="－", width=3, command=lambda: self.set_zoom(self.zoom / ZOOM_STEP)).pack(side="left")
        ttk.Button(toolbar, text="＋", width=3, command=lambda: self.set_zoom(self.zoom * ZOOM_STEP)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="全体表示", command=self.fit_zoom).pack(side="left")
        ttk.Checkbutton(toolbar, text="枠を表示", variable=self.overlay_var, command=self.render).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(toolbar, text="名前を表示", variable=self.label_var, command=self.render).pack(side="left", padx=(6, 0))
        ttk.Label(toolbar, text="格納時の回転:").pack(side="left", padx=(12, 3))
        rotation = ttk.Combobox(toolbar, textvariable=self.rotation_var, values=("cw", "ccw"),
                                width=5, state="readonly")
        rotation.pack(side="left")
        rotation.bind("<<ComboboxSelected>>", lambda _event: self.update_preview())

        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        left = ttk.Frame(main, padding=5)
        main.add(left, weight=1)
        ttk.Label(left, text="スプライト一覧").pack(anchor="w")
        search = ttk.Entry(left, textvariable=self.search_var)
        search.pack(fill="x", pady=5)
        self.search_var.trace_add("write", lambda *_args: self.update_list())
        list_frame = ttk.Frame(left)
        list_frame.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(list_frame, exportselection=False)
        list_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=list_scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        list_scroll.pack(side="right", fill="y")
        self.listbox.bind("<<ListboxSelect>>", self._select_from_list)

        center = ttk.Frame(main)
        main.add(center, weight=4)
        self.canvas = tk.Canvas(center, background="#262626", highlightthickness=0)
        x_scroll = ttk.Scrollbar(center, orient="horizontal", command=self.canvas.xview)
        y_scroll = ttk.Scrollbar(center, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=x_scroll.set, yscrollcommand=y_scroll.set)
        center.rowconfigure(0, weight=1)
        center.columnconfigure(0, weight=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.canvas.bind("<Button-1>", self._select_from_canvas)
        self.canvas.bind("<ButtonPress-2>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B2-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.canvas.bind("<MouseWheel>", self._mouse_zoom)
        self.canvas.bind("<Button-4>", lambda e: self._mouse_zoom_step(e, 1))
        self.canvas.bind("<Button-5>", lambda e: self._mouse_zoom_step(e, -1))

        right = ttk.Frame(main, padding=5)
        main.add(right, weight=1)
        ttk.Label(right, text="選択スプライト").pack(anchor="w")
        self.preview_label = ttk.Label(right, anchor="center")
        self.preview_label.pack(fill="x", pady=10)
        self.info_label = ttk.Label(right, textvariable=self.info_var, justify="left", wraplength=230)
        self.info_label.pack(anchor="nw", fill="x", pady=10)
        ttk.Button(right, text="選択画像を PNG 保存", command=self.save_selected).pack(fill="x", pady=10)

        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(10, 3)).pack(fill="x")

    def show_current_settings(self) -> None:
        source = self.atlas
        details = [
            'PLIST テクスチャビューアー - 現在の設定',
            '',
            'plist: ' + (str(source.plist_path) if source else '(未読込)'),
            'アトラス画像: ' + (str(source.texture_path) if source else '(未読込)'),
            '格納時の回転方向: ' + self.rotation_var.get(),
            'アトラス元画像: X右正 / Y下正',
            'スプライト格納時の回転は各フレームの rotated を使用',
            '枠表示: ' + ('ON' if self.overlay_var.get() else 'OFF'),
            '名前表示: ' + ('ON' if self.label_var.get() else 'OFF'),
            'ズーム: ' + format(self.zoom, '.5f'),
            '検索フィルター: ' + (self.search_var.get() or '(なし)'),
            '選択スプライト: ' + (self.selected.name if self.selected else '(なし)'),
            'スプライト数: ' + (str(len(source.frames)) if source else '-'),
        ]
        snapshot='\n'.join(details)+'\n'
        window=tk.Toplevel(self)
        window.title('現在の設定（テキストをコピー）')
        window.geometry('730x450')
        window.transient(self)
        box=ttk.Frame(window,padding=10)
        box.pack(fill='both',expand=True)
        text=tk.Text(box,wrap='none',font=('Consolas',10))
        text.pack(fill='both',expand=True)
        text.insert('1.0',snapshot)
        text.configure(state='disabled')
        def copy_all() -> None:
            window.clipboard_clear()
            window.clipboard_append(text.get('1.0','end-1c'))
            window.update_idletasks()
        ttk.Button(box,text='すべてコピー',command=copy_all).pack(anchor='e',pady=(8,0))

    def ask_exportjson(self) -> None:
        path = filedialog.askopenfilename(title="ExportJson を選択", filetypes=[("Cocos Studio", "*.ExportJson *.json"), ("すべて", "*.*")])
        if path:
            ArmatureViewer(self, Path(path))

    def ask_plist(self) -> None:
        filename = filedialog.askopenfilename(title="plist を選択", filetypes=[("plist", "*.plist"), ("すべて", "*.*")])
        if filename:
            self.open_plist(Path(filename), None)

    def ask_image(self) -> None:
        if self.atlas is None:
            messagebox.showinfo(APP_TITLE, "先に plist を開いてください")
            return
        filename = filedialog.askopenfilename(title="テクスチャ画像を選択", filetypes=[("画像", "*.png *.webp *.jpg *.jpeg"), ("すべて", "*.*")])
        if filename:
            self.open_plist(self.atlas.plist_path, Path(filename))

    def show_load_report(self) -> None:
        if self.atlas is None:
            messagebox.showinfo(APP_TITLE, "まだファイルを開いていません")
            return
        warnings = self.atlas.warnings
        if not warnings:
            messagebox.showinfo(APP_TITLE, "すべてのスプライト配置情報を読み取りました")
            return
        display_limit = 20
        details = "\n".join(f"・{text}" for text in warnings[:display_limit])
        if len(warnings) > display_limit:
            details += f"\nほか {len(warnings) - display_limit} 件"
        messagebox.showwarning(APP_TITLE, f"未解析・注意事項: {len(warnings)} 件\n\n{details}")

    def open_plist(self, plist_path: Path, image_path: Optional[Path]) -> None:
        try:
            new_atlas = load_atlas(plist_path, image_path)
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException, UnidentifiedImageError) as exc:
            messagebox.showerror(APP_TITLE, f"ファイルを開けませんでした:\n{exc}")
            return
        self.atlas = new_atlas
        self.atlas_photo = None
        self.selected = None
        self.preview_photo = None
        self.preview_label.configure(image="")
        self.info_var.set("スプライトを選択してください" if new_atlas.frames else
                          "この plist に解析可能な配置情報はありません。\nテクスチャ画像のみ表示しています。\n「読み込み結果」で詳細を確認できます。")
        self.title(f"{APP_TITLE} - {new_atlas.plist_path.name}")
        self.update_list()
        self.fit_zoom()
        status = (f"{new_atlas.texture_path.name} | {new_atlas.image.width}×{new_atlas.image.height} | "
                  f"スプライト {len(new_atlas.frames)} 件")
        if new_atlas.warnings:
            status += f" | 未解析・注意 {len(new_atlas.warnings)} 件（読み込み結果で詳細）"
        self.status_var.set(status)

    def update_list(self) -> None:
        if self.atlas is None:
            self.list_frames = []
            return
        query = self.search_var.get().strip().casefold()
        self.list_frames = [frame for frame in self.atlas.frames if query in frame.name.casefold()]
        self.listbox.delete(0, tk.END)
        for frame in self.list_frames:
            self.listbox.insert(tk.END, frame.name)
        self._sync_list_selection()

    def _sync_list_selection(self) -> None:
        self.listbox.selection_clear(0, tk.END)
        if self.selected in self.list_frames:
            index = self.list_frames.index(self.selected)
            self.listbox.selection_set(index)
            self.listbox.see(index)

    def _select_from_list(self, _event: tk.Event) -> None:
        indices = self.listbox.curselection()
        if indices:
            self.select_frame(self.list_frames[indices[0]])

    def _select_from_canvas(self, event: tk.Event) -> None:
        if self.atlas is None:
            return
        point_x = self.canvas.canvasx(event.x) / self.zoom
        point_y = self.canvas.canvasy(event.y) / self.zoom
        hits = [f for f in self.atlas.frames if
                f.x - HIT_TOLERANCE <= point_x < f.x + f.packed_width + HIT_TOLERANCE and
                f.y - HIT_TOLERANCE <= point_y < f.y + f.packed_height + HIT_TOLERANCE]
        if hits:
            self.select_frame(min(hits, key=lambda f: f.packed_width * f.packed_height))

    def select_frame(self, frame: Frame) -> None:
        self.selected = frame
        self._sync_list_selection()
        self.render()
        self.update_preview()

    def update_preview(self) -> None:
        if self.atlas is None or self.selected is None:
            return
        try:
            image = extract_sprite(self.atlas.image, self.selected, self.rotation_var.get())
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        preview = image.copy()
        preview.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE), RESAMPLE_NEAREST)
        self.preview_photo = ImageTk.PhotoImage(preview)
        self.preview_label.configure(image=self.preview_photo)
        frame = self.selected
        self.info_var.set(
            f"名前: {frame.name}\n"
            f"配置: ({frame.x}, {frame.y})\n"
            f"格納サイズ: {frame.packed_width}×{frame.packed_height}\n"
            f"元サイズ: {frame.source_width}×{frame.source_height}\n"
            f"トリム位置: ({frame.trim_x}, {frame.trim_y})\n"
            f"回転格納: {'はい' if frame.rotated else 'いいえ'}"
        )

    def save_selected(self) -> None:
        if self.atlas is None or self.selected is None:
            messagebox.showinfo(APP_TITLE, "保存するスプライトを選択してください")
            return
        filename = filedialog.asksaveasfilename(
            title="PNG として保存", defaultextension=".png",
            initialfile=f"{Path(self.selected.name).stem}.png",
            filetypes=[("PNG", "*.png")],
        )
        if filename:
            try:
                image = extract_sprite(self.atlas.image, self.selected, self.rotation_var.get())
                image.save(filename, format="PNG")
            except (OSError, ValueError) as exc:
                messagebox.showerror(APP_TITLE, f"保存に失敗しました:\n{exc}")
                return
            self.status_var.set(f"保存しました: {filename}")

    def _max_zoom(self) -> float:
        if self.atlas is None:
            return MAX_ZOOM
        width, height = self.atlas.image.size
        return max(MIN_ZOOM, min(MAX_ZOOM, MAX_DISPLAY_SIDE / width, MAX_DISPLAY_SIDE / height))

    def fit_zoom(self) -> None:
        if self.atlas is None:
            return
        self.update_idletasks()
        width = max(self.canvas.winfo_width() - 20, 1)
        height = max(self.canvas.winfo_height() - 20, 1)
        target = min(width / self.atlas.image.width, height / self.atlas.image.height, self._max_zoom())
        self.set_zoom(target)
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def set_zoom(self, value: float) -> None:
        if self.atlas is None:
            return
        value = max(MIN_ZOOM, min(value, self._max_zoom()))
        if abs(value - self.zoom) < 0.0001 and self.atlas_photo is not None:
            return
        self.zoom = value
        self.render()

    def _mouse_zoom_step(self, event: tk.Event, direction: int) -> None:
        del event
        self.set_zoom(self.zoom * (ZOOM_STEP if direction > 0 else 1 / ZOOM_STEP))

    def _mouse_zoom(self, event: tk.Event) -> None:
        if event.delta:
            self._mouse_zoom_step(event, 1 if event.delta > 0 else -1)

    def render(self) -> None:
        self.canvas.delete("all")
        if self.atlas is None:
            return
        src = self.atlas.image
        scaled = (max(1, round(src.width * self.zoom)), max(1, round(src.height * self.zoom)))
        resized = src.resize(scaled, RESAMPLE_NEAREST)
        self.atlas_photo = ImageTk.PhotoImage(resized)
        self.canvas.create_image(0, 0, anchor="nw", image=self.atlas_photo)
        if self.overlay_var.get():
            for frame in self.atlas.frames:
                x0, y0, x1, y1 = (coord * self.zoom for coord in frame.packed_box)
                is_selected = frame == self.selected
                color = SELECTED_COLOR if is_selected else OVERLAY_COLOR
                self.canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=3 if is_selected else 1)
                if self.label_var.get() and (self.zoom >= 0.5 or is_selected):
                    self.canvas.create_text(x0 + 2, y0 + 2, text=frame.name, fill=color, anchor="nw")
        self.canvas.configure(scrollregion=(0, 0, scaled[0], scaled[1]))


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
SELECTION_COLOR = (69, 227, 255, 255)
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
            color = SELECTION_COLOR if selected==item.bone_name else (MISSING_COLOR if item.image is None else CORNERS_COLOR)
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


class ArmatureViewer(tk.Toplevel):
    """Standalone armature renderer; the existing plist viewer remains available."""
    def __init__(self, master: tk.Tk, json_path: Optional[Path] = None,
                 atlas_path: Optional[Path] = None, image_path: Optional[Path] = None,
                 rotation: str = 'cw', initial_fps: int = APP_FPS_DEFAULT,
                 flip_uv_y: bool = False,
                 flip_screen_y: bool = False,
                 flip_uv_x: bool = False,
                 renderer: str = 'auto',
                 hair_layer: str = 'swap') -> None:
        super().__init__(master)
        self.title('Cocos Studio ExportJson ビューアー v26')
        self.geometry('1270x820')
        self.minsize(850,560)
        self.model: Optional[ArmatureModel] = None
        self.current: Optional[MovementDef] = None
        self.asset_plist=atlas_path
        self.asset_image=image_path
        self.rotation=rotation
        self.selected: Optional[str] = None
        self.hidden: set[str] = set()
        self.bone_filter=tk.StringVar()
        self.movement_name=tk.StringVar()
        self.index_var=tk.DoubleVar(value=0)
        self.fps_var=tk.StringVar(value=str(initial_fps))
        self.bounds_var=tk.BooleanVar(value=False)
        self.flip_uv_x=tk.BooleanVar(value=flip_uv_x)
        if hair_layer not in HAIR_LAYER_LABELS.values():
            raise ValueError(f'不明な髪レイヤー設定: {hair_layer}')
        default_label = next(key for key, value in HAIR_LAYER_LABELS.items()
                             if value == hair_layer)
        self.hair_layer_var=tk.StringVar(value=default_label)
        self.gpu_var=tk.BooleanVar(value=renderer != 'cpu')
        self.gpu_renderer=None
        self.gpu_warning=''
        self.flip_uv_y=tk.BooleanVar(value=flip_uv_y)
        self.flip_screen_y=tk.BooleanVar(value=flip_screen_y)
        self.loop_var=tk.BooleanVar(value=True)
        self.status=tk.StringVar(value='ExportJson を開いてください')
        self.details=tk.StringVar(value='')
        self.frame_label=tk.StringVar(value='0 / 0')
        self.render_time_label=tk.StringVar(value='描画: -- ms')
        self.is_playing=False
        self.timer_id: Optional[str] = None
        self.rendered: Optional[ImageTk.PhotoImage] = None
        self._image_canvas_id: Optional[int] = None
        self.last_image: Optional[Image.Image] = None
        self.last_polygons: list[tuple[str,list[tuple[float,float]]]] = []
        self.scale=1.0
        self.camera=(0.0,0.0)
        self._pan_position: Optional[tuple[int,int]] = None
        self._resize_id: Optional[str] = None
        self._fit_id: Optional[str] = None
        self._build_ui()
        self.protocol('WM_DELETE_WINDOW',self._close)
        if json_path is not None:
            self.after_idle(lambda:self.open_json(json_path))

    def _build_ui(self) -> None:
        bar=ttk.Frame(self,padding=6)
        bar.pack(fill='x')
        ttk.Button(bar,text='ExportJson を開く',command=self.ask_json).pack(side='left')
        ttk.Button(bar,text='左右データを切替',command=self.switch_orientation_data).pack(side='left',padx=5)
        ttk.Button(bar,text='アトラス plist を指定',command=self.ask_plist).pack(side='left',padx=5)
        ttk.Button(bar,text='PNG を指定',command=self.ask_png).pack(side='left')
        ttk.Button(bar,text='参照テクスチャを確認',command=self.show_assets).pack(side='left',padx=5)
        ttk.Button(bar,text='読み込み結果',command=self.show_warnings).pack(side='left',padx=5)
        ttk.Button(bar,text='位置診断',command=self.show_alignment_diagnostics).pack(side='left',padx=5)
        ttk.Button(bar,text='設定を表示・コピー',command=self.show_current_settings).pack(side='left',padx=5)
        ttk.Button(bar,text='パーツ別UV補正',command=self.show_skin_overrides).pack(side='left',padx=5)
        ttk.Button(bar,text='現在の姿勢を PNG 保存',command=self.save_png).pack(side='left',padx=5)
        controls=ttk.Frame(self,padding=(7,2))
        controls.pack(fill='x')
        ttk.Label(controls,text='動作:').pack(side='left')
        self.movements=ttk.Combobox(controls,textvariable=self.movement_name,width=20,state='readonly')
        self.movements.pack(side='left',padx=4)
        self.movements.bind('<<ComboboxSelected>>',self.change_movement)
        self.play_btn=ttk.Button(controls,text='再生',command=self.toggle_play,width=6)
        self.play_btn.pack(side='left')
        ttk.Button(controls,text='先頭',command=self.goto_start).pack(side='left',padx=3)
        ttk.Checkbutton(controls,text='ループ',variable=self.loop_var).pack(side='left',padx=6)
        ttk.Label(controls,text='FPS').pack(side='left')
        ttk.Spinbox(controls,from_=APP_FPS_MIN,to=APP_FPS_MAX,
                    textvariable=self.fps_var,width=5).pack(side='left',padx=2)
        ttk.Checkbutton(controls,text='GPU描画 (ModernGL)',variable=self.gpu_var,
                        command=self.draw_scene).pack(side='left',padx=(12,2))
        ttk.Label(controls,textvariable=self.render_time_label).pack(side='left',padx=8)
        options=ttk.Frame(self,padding=(7,2))
        options.pack(fill='x')
        ttk.Label(options,text='表示:').pack(side='left',padx=(12,3))
        ttk.Checkbutton(options,text='ボーン枠',variable=self.bounds_var,
                        command=self.draw_scene).pack(side='left')
        ttk.Checkbutton(options,text='UV X反転（パーツ内）',variable=self.flip_uv_x,
                        command=self._orientation_changed).pack(side='left',padx=(7,0))
        ttk.Checkbutton(options,text='UV Y反転',variable=self.flip_uv_y,
                        command=self._orientation_changed).pack(side='left',padx=(7,0))
        ttk.Checkbutton(options,text='画面 Y反転',variable=self.flip_screen_y,
                        command=self._orientation_changed).pack(side='left',padx=(7,0))
        ttk.Button(options,text='向きを4通り比較',command=self.show_orientation_comparison).pack(side='left',padx=5)
        ttk.Button(options,text='全体表示',command=self.fit).pack(side='left',padx=4)
        ttk.Button(options,text='100%',command=self.actual_size).pack(side='left',padx=2)
        ttk.Button(options,text='－',width=3,command=lambda:self.zoom_by(0.8)).pack(side='left')
        ttk.Button(options,text='＋',width=3,command=lambda:self.zoom_by(1.25)).pack(side='left')
        hairbar=ttk.Frame(self,padding=(7,2))
        hairbar.pack(fill='x')
        ttk.Label(hairbar,text='髪のレイヤー:').pack(side='left',padx=(12,3))
        self.hair_layer_select=ttk.Combobox(
            hairbar, textvariable=self.hair_layer_var, width=19, state='readonly',
            values=tuple(HAIR_LAYER_LABELS))
        self.hair_layer_select.pack(side='left',padx=4)
        self.hair_layer_select.bind('<<ComboboxSelected>>',self._hair_layer_changed)
        framebar=ttk.Frame(self,padding=(8,3))
        framebar.pack(fill='x')
        ttk.Label(framebar,text='フレーム').pack(side='left')
        self.frame_scale=ttk.Scale(framebar,from_=0,to=1,variable=self.index_var,command=self.change_frame)
        self.frame_scale.pack(side='left',fill='x',expand=True,padx=6)
        ttk.Label(framebar,textvariable=self.frame_label,width=13).pack(side='left')
        pane=ttk.PanedWindow(self,orient='horizontal')
        pane.pack(fill='both',expand=True,padx=6,pady=4)
        left=ttk.Frame(pane,padding=5)
        pane.add(left,weight=1)
        ttk.Label(left,text='ボーン一覧（ダブルクリックで表示切替）').pack(anchor='w')
        ttk.Entry(left,textvariable=self.bone_filter).pack(fill='x',pady=4)
        self.bone_filter.trace_add('write',lambda *_args:self.list_bones())
        self.listbox=tk.Listbox(left,exportselection=False)
        self.listbox.pack(fill='both',expand=True)
        self.listbox.bind('<<ListboxSelect>>',self.select_bone)
        self.listbox.bind('<Double-Button-1>',self.toggle_bone)
        ttk.Button(left, text='選択解除 (Esc)', command=self.clear_selection).pack(fill='x',pady=(4,1))
        ttk.Label(left,textvariable=self.details,justify='left',wraplength=200).pack(fill='x',pady=5)
        center=ttk.Frame(pane)
        pane.add(center,weight=5)
        self.canvas=tk.Canvas(center,bg='#242833',highlightthickness=0)
        self.canvas.pack(fill='both',expand=True)
        self.canvas.bind('<Configure>',self._on_resize)
        self.canvas.bind('<MouseWheel>',self._wheel)
        self.canvas.bind('<Button-4>',lambda e:self.zoom_by(1.2))
        self.canvas.bind('<Button-5>',lambda e:self.zoom_by(1/1.2))
        self.canvas.bind('<ButtonPress-2>',self._start_pan)
        self.canvas.bind('<B2-Motion>',self._pan)
        self.canvas.bind('<ButtonPress-3>',self._start_pan)
        self.canvas.bind('<B3-Motion>',self._pan)
        self.canvas.bind('<Button-1>',self.click_canvas)
        self.bind('<Escape>',self.clear_selection)
        ttk.Label(self,textvariable=self.status,padding=(8,4),anchor='w').pack(fill='x')

    def _on_resize(self,_event=None) -> None:
        if self._resize_id is not None:
            self.after_cancel(self._resize_id)
        self._resize_id=self.after(120,self._draw_after_resize)

    def _draw_after_resize(self) -> None:
        self._resize_id = None
        self.draw_scene()

    def _close(self) -> None:
        self.stop()
        if self._resize_id is not None:
            self.after_cancel(self._resize_id)
            self._resize_id = None
        if self._fit_id is not None:
            self.after_cancel(self._fit_id)
            self._fit_id = None
        if self.gpu_renderer is not None:
            self.gpu_renderer.close()
            self.gpu_renderer=None
        self.destroy()
        if self.master.state() == 'withdrawn':
            self.master.destroy()

    def ask_json(self) -> None:
        path=filedialog.askopenfilename(parent=self,title='ExportJson を開く',
                                        filetypes=[('Cocos Studio ExportJson','*.ExportJson *.json'),('すべて','*.*')])
        if path:
            self.asset_plist=None
            self.asset_image=None
            self.open_json(Path(path))

    def switch_orientation_data(self) -> None:
        """Load the left/right variant's full rig and atlas; do not mirror UVs."""
        if self.model is None:
            return
        counterpart = find_other_orientation_export(self.model.json_path)
        if counterpart is None:
            messagebox.showinfo(self.title(),
                '対応する _l / _r の ExportJson が同じフォルダーに見つかりません。'
                '\n左右は異なるボーン構造を持つため、UV X反転だけでは変換できません。',
                parent=self)
            return
        old_plist, old_image = self.asset_plist, self.asset_image
        old_model = self.model
        self.asset_plist = self.asset_image = None
        self.open_json(counterpart)
        if self.model is old_model:
            self.asset_plist, self.asset_image = old_plist, old_image
        else:
            # UV X reflection is a separate diagnostic option, not rig mirroring.
            self.flip_uv_x.set(False)
            self.draw_scene()
            self._status()

    def ask_plist(self) -> None:
        if self.model is None:
            messagebox.showinfo(self.title(),'先に ExportJson を開いてください',parent=self)
            return
        path=filedialog.askopenfilename(parent=self,title='参照する plist を指定',filetypes=[('plist','*.plist')])
        if path:
            self.asset_plist=Path(path)
            self.reload_assets()

    def ask_png(self) -> None:
        if self.model is None:
            messagebox.showinfo(self.title(),'先に ExportJson を開いてください',parent=self)
            return
        path=filedialog.askopenfilename(parent=self,title='参照する PNG を指定',
                                        filetypes=[('画像','*.png *.webp *.jpg *.jpeg')])
        if path:
            self.asset_image=Path(path)
            self.reload_assets()

    def open_json(self,path:Path) -> None:
        try:
            new_model=parse_exportjson(path)
            resolve_armature_images(new_model,self.asset_plist,self.asset_image,self.rotation)
            self.status.set('全動作・全フレームの座標とパーティクルを事前計算中...')
            self.update_idletasks()
            precompute_pose_cache(new_model)
            preview_movement=next((m for m in new_model.movements if m.name=='wait'),new_model.movements[0])
            pose_bounds(new_model,preview_movement)
        except (OSError,ValueError,TypeError,MemoryError,json.JSONDecodeError) as exc:
            messagebox.showerror(self.title(),f'ExportJson を開けませんでした:\n{exc}',parent=self)
            return
        self.stop()
        # Orientation variants are distinct rigs. Do not keep a pixel-level UV
        # mirror when a different directional data set is opened from the GUI.
        previous_side = export_orientation(self.model.json_path) if self.model else None
        next_side = export_orientation(new_model.json_path)
        if previous_side is not None and next_side is not None and previous_side != next_side:
            self.flip_uv_x.set(False)
        if self.gpu_renderer is not None:
            self.gpu_renderer.clear_textures()
        self.model=new_model
        self.current=preview_movement
        self.movement_name.set(preview_movement.name)
        self.movements.configure(values=[m.name for m in new_model.movements])
        self.index_var.set(0)
        self.frame_scale.configure(to=max(0,preview_movement.duration-1))
        self.loop_var.set(preview_movement.loop)
        self.hidden.clear()
        self.selected=None
        self.list_bones()
        self.title(f'Cocos Studio ExportJson - {path.name}')
        self.update_idletasks()
        self.fit()
        # Refit after the OS finishes mapping the top-level window; otherwise
        # Tk may initially report a 1x1 canvas and start at minimum zoom.
        if self._fit_id is not None:
            self.after_cancel(self._fit_id)
        self._fit_id = self.after(180,self._fit_after_load)
        self._status()

    def _fit_after_load(self) -> None:
        self._fit_id = None
        if self.model is not None and self.current is not None:
            self.fit()

    def reload_assets(self) -> None:
        if self.gpu_renderer is not None:
            self.gpu_renderer.clear_textures()
        if self.model is None:
            return
        try:
            resolve_armature_images(self.model,self.asset_plist,self.asset_image,self.rotation)
            precompute_pose_cache(self.model)
        except (OSError,ValueError,MemoryError) as exc:
            messagebox.showerror(self.title(),str(exc),parent=self)
            return
        self.draw_scene()
        self._status()

    def _status(self) -> None:
        if self.model is None:
            return
        total=len(self.model.textures)
        count=sum(name in self.model.sprites for name in self.model.textures)
        atlas_names = ', '.join(p.name for p in self.model.loaded_plists) or '未検出'
        texture_names = ', '.join(p.name for p in self.model.loaded_images) or '未検出'
        particle_names = ', '.join(p.name for p in self.model.loaded_particle_plists) or 'なし'
        self.status.set(f'{self.model.name} | ボーン {len(self.model.bones)} | 動作 {len(self.model.movements)} | '
                        f'画像 {count}/{total} | アトラス {atlas_names} | パーティクル(簡易) {particle_names} | テクスチャ {texture_names} | '
                        f'注意 {len(self.model.warnings)} 件 | '
                        f'姿勢キャッシュ {self.model.pose_cache_frames}フレーム | '
                        f'髪レイヤー {self.hair_layer_var.get()} | '
                        f'UV X反転 {"ON" if self.flip_uv_x.get() else "OFF"} | '
                        f'UV Y反転 {"ON" if self.flip_uv_y.get() else "OFF"} | '
                        f'画面 Y反転 {"ON" if self.flip_screen_y.get() else "OFF"} | '
                        f'画像寸法 アトラス元寸法 | '
                        f'表示倍率 {self.scale:.2f}x | '
                        f'左右版 {export_orientation(self.model.json_path) or "不明"} | '
                        f'描画 {"GPU" if self.gpu_var.get() and self.gpu_renderer else "CPU"}'
                        f'{" (GPU未使用: " + self.gpu_warning[:55] + ")" if self.gpu_warning else ""}')

    def _hair_layer(self) -> str:
        return HAIR_LAYER_LABELS.get(self.hair_layer_var.get(), 'original')

    def _hair_layer_changed(self, _event: Optional[tk.Event] = None) -> None:
        self.draw_scene()
        self._status()

    def _orientation_changed(self) -> None:
        self.draw_scene()
        self._status()

    def show_orientation_comparison(self) -> None:
        """Compare all combinations using the currently loaded real atlas pixels.

        The four previews use identical pose, camera and scale. Selection changes
        only output orientation; no atlas or animation files are modified.
        """
        if self.model is None or self.current is None:
            return
        preview = tk.Toplevel(self)
        preview.title('上下方向を比較して選択')
        preview.transient(self)
        preview.geometry('960x760')
        preview.minsize(540,460)
        description = ('同じフレームを４通りで表示します。正しい向きの画像の「適用」を選択してください。'
                       ' UV X は現在の設定のまま、UV Y と画面 Y を比較します。')
        ttk.Label(preview,text=description,wraplength=880,justify='left',padding=10).pack(fill='x')
        grid = ttk.Frame(preview,padding=8)
        grid.pack(expand=True,fill='both')
        image_references = []
        # Four combinations in a fixed order, independent of the user's current settings.
        configurations = ((False,False),(True,False),(False,True),(True,True))
        names = ('A: UV通常 / 画面通常','B: UV反転 / 画面通常',
                 'C: UV通常 / 画面反転','D: UV反転 / 画面反転')
        viewport_w = max(1,self.canvas.winfo_width())
        viewport_h = max(1,self.canvas.winfo_height())
        mini_w = 400
        mini_h = 280
        mini_ratio = min(mini_w/viewport_w,mini_h/viewport_h)
        for i,((flip_uv,flip_screen),name) in enumerate(zip(configurations,names)):
            panel = ttk.Frame(grid,padding=5,relief='groove')
            panel.grid(row=i//2,column=i%2,sticky='nsew',padx=5,pady=5)
            ttk.Label(panel,text=name).pack(pady=5)
            try:
                rendered,_ = render_armature(
                    self.model,self.current,self.index_var.get(),
                    mini_w,mini_h,self.scale*mini_ratio,self.camera,
                    show_bounds=self.bounds_var.get(),selected=self.selected,
                    hidden=frozenset(self.hidden),
                    flip_uv_y=flip_uv,flip_screen_y=flip_screen,
                    flip_uv_x=self.flip_uv_x.get(),
                    hair_layer=self._hair_layer(),
                    opaque_background=True,
                    )
                background = Image.new('RGBA',rendered.size,(36,40,51,255))
                background.alpha_composite(rendered)
                photo = ImageTk.PhotoImage(background)
                image_references.append(photo)
                ttk.Label(panel,image=photo).pack(expand=True)
            except (OSError,ValueError,MemoryError) as exc:
                ttk.Label(panel,text=f'プレビュー生成エラー: {exc}',wraplength=360).pack(expand=True)
            def use_orientation(uv: bool = flip_uv, screen: bool = flip_screen) -> None:
                self.flip_uv_y.set(uv)
                self.flip_screen_y.set(screen)
                self._orientation_changed()
                preview.destroy()
            ttk.Button(panel,text='この向きを適用',command=use_orientation).pack(pady=5)
        for row in (0,1):
            grid.rowconfigure(row,weight=1)
            grid.columnconfigure(row,weight=1)
        preview.image_references=image_references

    def current_settings_text(self) -> str:
        """Copyable snapshot of authored transforms and explicit UI overrides."""
        model, movement = self.model, self.current
        def onoff(v: bool) -> str:
            return 'ON' if v else 'OFF'
        lines = [
            'Cocos Studio ExportJson ビューアー v26 - 現在の設定', '',
            '[入力]',
            'ExportJson: ' + (str(model.json_path) if model else '(未読込)'),
            '方向: ' + ((export_orientation(model.json_path) or '不明') if model else '-'),
            '動作: ' + (movement.name if movement else '(未選択)'),
            '現在フレーム: ' + format(self.index_var.get(), '.3f'),
            'アトラス: ' + (', '.join(map(str,model.loaded_plists)) if model and model.loaded_plists else '(未検出)'),
            'パーティクル: ' + (', '.join(map(str,model.loaded_particle_plists)) if model and model.loaded_particle_plists else '(未検出)'),
            'PNG: ' + (', '.join(map(str,model.loaded_images)) if model and model.loaded_images else '(未検出)'),
            'パーティクル plist: ' + (', '.join(map(str,model.loaded_particle_plists)) if model and model.loaded_particle_plists else '(なし)'),
            'パーティクル描画: 簡易プレビュー（Cocos Studio の完全な粒子挙動・色・合成ではない）',
            '選択ボーン: ' + (self.selected or '(なし)'), '',
            '[変換規則]',
            '変換エンジン: Cocos Studio 1.x (TransformHelp / Bone::applyParentTransform)',
            'nodeToMatrix: a=cX*cos(kY), b=cX*sin(kY), c=cY*sin(kX), d=cY*cos(kX)',
            '親子継承: 位置は親行列で変換 / 倍率は乗算 / kX,kYは加算',
            '静止姿勢＋キーフレーム: XY/角度を加算、倍率=rest+frame-1',
            'スキン: ボーン世界行列×スキン行列、データ別自動補正なし',
            'Cocos 座標: X右・Y上 / Tk座標: X右・Y下',
            'アトラス画像寸法: トリミング前の復元寸法',
            'UVの基準: Pillowの左上原点', '',
            '[設定]',
            '画面Y反転: ' + onoff(self.flip_screen_y.get()),
            'UV X反転: ' + onoff(self.flip_uv_x.get()),
            'UV Y反転: ' + onoff(self.flip_uv_y.get()),
            'アトラス回転復元: ' + self.rotation,
            '髪のレイヤー: ' + self.hair_layer_var.get() + ' (' + self._hair_layer() + ')',
            'ボーン枠: ' + onoff(self.bounds_var.get()),
            'FPS: ' + self.fps_var.get(),
            '姿勢キャッシュ: 全動作を初回読込時に事前計算',
            '姿勢キャッシュ済みフレーム数: ' + (str(model.pose_cache_frames) if model else '-'),
            '姿勢キャッシュ済み描画要素数: ' + (str(model.pose_cache_sprites) if model else '-'),
            '姿勢キャッシュ生成秒数: ' + (format(model.pose_cache_seconds, '.3f') if model else '-'),
            '再生中: ' + onoff(self.is_playing),
            'ExportJson ブレンド: キーフレーム bd_src/bd_dst を反映 (未指定は 770/771)',
            'ExportJson カラー: キーフレーム color.r/g/b/a を反映',
            'パーティクルブレンド: plist blendFuncSource/blendFuncDestination を反映',
            'GPU描画指定: ' + onoff(self.gpu_var.get()),
            'GPU使用可能: ' + onoff(self.gpu_renderer is not None),
            'GPU警告: ' + (self.gpu_warning or '(なし)'),
            '倍率: ' + format(self.scale, '.5f'),
            'カメラ: ' + repr(self.camera),
            '非表示ボーン: ' + (', '.join(sorted(self.hidden)) if self.hidden else '(なし)'),
            '', '[明示的なパーツ別UV補正]',
        ]
        if model:
            lines.extend([key + ': ' + mode for key,mode in sorted(model.skin_overrides.items())]
                         or ['(なし)'])
            lines += ['', '[現フレームの頭パーツ]']
            if movement:
                sprites = {part.bone_name: part for part in compose_pose(
                    model, movement, self.index_var.get(),frozenset(self.hidden),self._hair_layer())}
                for bone in model.bones:
                    if 'head' not in bone.name.casefold():
                        continue
                    part=sprites.get(bone.name)
                    if part is None:
                        continue
                    state=frame_state(movement.timelines.get(bone.name,()),self.index_var.get())
                    index=int(state.get('dI',bone.display_index)) if state else bone.display_index
                    m=part.matrix
                    lines.extend([
                        f'{bone.name}: dI={index}, image={part.image_name}',
                        '  matrix [a,b,c,d,tx,ty]: '+', '.join(format(v,'.6f') for v in m),
                        '  det: '+format(m[0]*m[3]-m[1]*m[2],'.6f'),
                    ])
                from collections import Counter
                # The summary includes every active sprite, not only head parts.
                visible = compose_pose(model, movement, self.index_var.get(),
                                       frozenset(self.hidden), self._hair_layer())
                counts = Counter((sprite.blend, sprite.tint) for sprite in visible)
                lines += ['', '[現フレームのブレンドと色]']
                for (blend, tint), count in sorted(counts.items()):
                    lines.append(f'bd_src={blend[0]}, bd_dst={blend[1]}, '
                                 f'RGBA={tint}: {count} パーツ')
            lines.extend(['', '[読み込み状況]',
                          'ボーン数: '+str(len(model.bones)),
                          '動作数: '+str(len(model.movements)),
                          '注意: '+str(len(model.warnings))])
        return '\n'.join(lines)+'\n'

    def show_skin_overrides(self) -> None:
        """Edit UV reflection per bone display without changing other sprites.

        Standard rendering is specification-based without implicit corrections;
        user-specified modes are consistently applied to CPU and GPU.
        """
        if self.model is None:
            messagebox.showinfo('パーツ別UV補正', '先に ExportJson を開いてください', parent=self)
            return
        model = self.model
        win = tk.Toplevel(self)
        win.title('パーツ別の向き（ボーン / dI ごと）')
        win.geometry('970x650')
        win.minsize(700,400)
        win.transient(self)
        outer = ttk.Frame(win,padding=10)
        outer.pack(fill='both',expand=True)
        ttk.Label(outer, text=('画像ごとに向きを指定します。\n'
            '標準では Cocos Studio の変換式だけを使用し、推定補正はありません。'
            'X/Y反転はスキン原点を軸に作用します。'),
            justify='left').pack(anchor='w',pady=(0,7))
        search_var = tk.StringVar()
        ttk.Entry(outer,textvariable=search_var).pack(fill='x',pady=(0,5))
        body = ttk.Frame(outer)
        body.pack(fill='both',expand=True)
        list_frame=ttk.Frame(body)
        list_frame.pack(side='left',fill='both',expand=True)
        items=tk.Listbox(list_frame,exportselection=False,font=('Consolas',10))
        scroll=ttk.Scrollbar(list_frame,orient='vertical',command=items.yview)
        items.configure(yscrollcommand=scroll.set)
        items.pack(side='left',fill='both',expand=True)
        scroll.pack(side='right',fill='y')
        chooser=ttk.Frame(body,padding=(12,0,0,0))
        chooser.pack(side='right',fill='y')
        ttk.Label(chooser,text='選択した画像の補正方法:').pack(anchor='w')
        mode_var=tk.StringVar(value='標準（Cocos Studio）')
        mode_select=ttk.Combobox(chooser,textvariable=mode_var,
                                 values=list(SKIN_OVERRIDE_OPTIONS),
                                 state='readonly',width=25)
        mode_select.pack(anchor='w',pady=(4,12))
        item_info=tk.StringVar(value='左の画像を選んでください')
        ttk.Label(chooser,textvariable=item_info,wraplength=270,
                  justify='left').pack(fill='x',pady=(0,8))
        records = [(bone, idx, disp) for bone in model.bones
                   for idx,disp in enumerate(bone.displays)]
        visible_records=[]
        selected_key=[None]

        def populate() -> None:
            keyword=search_var.get().strip().casefold()
            visible_records[:] = [(b,i,d) for b,i,d in records
                                  if keyword in b.name.casefold() or keyword in d.name.casefold()]
            items.delete(0,tk.END)
            for b,i,d in visible_records:
                key=skin_override_key(b.name,i)
                mode=model.skin_overrides.get(key,'auto')
                prefix='* ' if mode != 'auto' else '  '
                items.insert(tk.END,f'{prefix}{b.name}  dI={i}  {d.name}  [{mode}]')
            if selected_key[0] is not None:
                for j,(b,i,d) in enumerate(visible_records):
                    if skin_override_key(b.name,i)==selected_key[0]:
                        items.selection_set(j)
                        items.see(j)
                        break

        def selected(_event=None) -> None:
            indexes=items.curselection()
            if not indexes:return
            b,i,d=visible_records[indexes[0]]
            key=skin_override_key(b.name,i)
            selected_key[0]=key
            mode=model.skin_overrides.get(key,'auto')
            mode_var.set(next(label for label,code in SKIN_OVERRIDE_OPTIONS.items() if code==mode))
            active=''
            if self.current is not None:
                state=frame_state(self.current.timelines.get(b.name,()),self.index_var.get())
                di=int(state.get('dI',b.display_index)) if state else b.display_index
                active='使用中' if di==i else f'現在 dI={di}'
            exists='画像を読込済み' if v_normal_name(d.name) in model.sprites else '画像未読込（PNG/アトラスを確認）'
            item_info.set(f'ボーン: {b.name}\n画像: {d.name}\ndI={i}（{active}）\n{exists}')

        def v_normal_name(value: str) -> str:
            return _normal_name(value)

        def apply() -> None:
            key=selected_key[0]
            if key is None:return
            mode=SKIN_OVERRIDE_OPTIONS[mode_var.get()]
            if mode=='auto':model.skin_overrides.pop(key,None)
            else:model.skin_overrides[key]=mode
            precompute_pose_cache(model)
            populate()
            self.draw_scene()
            self._status()

        def reset() -> None:
            if selected_key[0] is None:return
            model.skin_overrides.pop(selected_key[0],None)
            precompute_pose_cache(model)
            mode_var.set('標準（Cocos Studio）')
            populate()
            self.draw_scene()
            self._status()

        def save() -> None:
            name=filedialog.asksaveasfilename(parent=win,title='パーツ別UV補正を保存',
                    initialfile=model.json_path.stem+'.uv.json',defaultextension='.json',
                    filetypes=[('JSON','*.json')])
            if not name:return
            try:
                content={'format':'cocosstudio-skin-overrides-v1',
                         'source':model.json_path.name,
                         'overrides':dict(sorted(model.skin_overrides.items()))}
                Path(name).write_text(json.dumps(content,ensure_ascii=False,indent=2)+'\n',
                                      encoding='utf-8')
            except OSError as exc:
                messagebox.showerror('保存エラー',str(exc),parent=win)

        def load() -> None:
            filename=filedialog.askopenfilename(parent=win,title='パーツ別UV補正を読み込む',
                                               filetypes=[('JSON','*.json')])
            if not filename:return
            try:
                content=json.loads(Path(filename).read_text(encoding='utf-8-sig'))
                if not isinstance(content,dict) or content.get('format')!='cocosstudio-skin-overrides-v1':
                    raise ValueError('補正設定ファイル形式が不正です')
                if content.get('source') != model.json_path.name:
                    raise ValueError('設定の元ExportJson名が異なります')
                values=content.get('overrides')
                if not isinstance(values,dict):
                    raise ValueError('overrides は辞書である必要があります')
                checked=validate_skin_overrides(model,values)
            except (OSError,UnicodeError,ValueError,TypeError,json.JSONDecodeError) as exc:
                messagebox.showerror('読込エラー',str(exc),parent=win)
                return
            model.skin_overrides.clear()
            model.skin_overrides.update(checked)
            precompute_pose_cache(model)
            populate()
            self.draw_scene()
            self._status()

        items.bind('<<ListboxSelect>>',selected)
        search_var.trace_add('write',lambda *_:populate())
        ttk.Button(chooser,text='適用',command=apply).pack(fill='x',pady=(8,3))
        ttk.Button(chooser,text='この画像を標準に戻す',command=reset).pack(fill='x',pady=3)
        ttk.Button(chooser,text='設定をJSON保存',command=save).pack(fill='x',pady=(20,3))
        ttk.Button(chooser,text='設定をJSON読込',command=load).pack(fill='x',pady=3)
        ttk.Button(chooser,text='閉じる',command=win.destroy).pack(fill='x',pady=(20,3))
        populate()
        if visible_records:
            current_selected=next((i for i,(b,di,d) in enumerate(visible_records)
                                  if b.name==self.selected),0)
            items.selection_set(current_selected)
            selected()

    def show_current_settings(self) -> None:
        """Open a selectable plain-text snapshot with an explicit copy button."""
        snapshot = self.current_settings_text()
        window = tk.Toplevel(self)
        window.title('現在の設定（テキストをコピー）')
        window.geometry('810x680')
        window.minsize(480,320)
        window.transient(self)
        panel = ttk.Frame(window, padding=10)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='表示時点の設定です。「すべてコピー」でクリップボードにコピーできます。').pack(anchor='w', pady=(0,6))
        content = ttk.Frame(panel)
        content.pack(fill='both', expand=True)
        text = tk.Text(content, wrap='none', font=('Consolas', 10), undo=False)
        yscroll = ttk.Scrollbar(content, orient='vertical', command=text.yview)
        xscroll = ttk.Scrollbar(content, orient='horizontal', command=text.xview)
        text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        text.grid(row=0,column=0,sticky='nsew')
        yscroll.grid(row=0,column=1,sticky='ns')
        xscroll.grid(row=1,column=0,sticky='ew')
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        text.insert('1.0', snapshot)
        text.configure(state='disabled')
        buttons=ttk.Frame(panel)
        buttons.pack(fill='x', pady=(8,0))
        status=ttk.Label(buttons,text='')
        status.pack(side='left')
        def copy_all() -> None:
            window.clipboard_clear()
            window.clipboard_append(text.get('1.0','end-1c'))
            window.update_idletasks()
            status.configure(text='設定をコピーしました')
        ttk.Button(buttons,text='すべてコピー',command=copy_all).pack(side='right')
        ttk.Button(buttons,text='閉じる',command=window.destroy).pack(side='right',padx=(0,8))

    def show_assets(self) -> None:
        if self.model is None:
            return
        plist_text = '\n'.join(str(p) for p in self.model.loaded_plists) or '(検出できませんでした)'
        particle_text = '\n'.join(str(p) for p in self.model.loaded_particle_plists) or '(なし)'
        image_text = '\n'.join(str(p) for p in self.model.loaded_images) or '(検出できませんでした)'
        particle_text = '\n'.join(str(p) for p in self.model.loaded_particle_plists) or '(なし)'
        messagebox.showinfo('ExportJson の参照テクスチャ',
                            f'アトラス plist:\n{plist_text}\n\n'
                            f'パーティクル plist (簡易プレビュー):\n{particle_text}\n\n'
                            f'外部画像（内蔵パーティクル画像は含まない）:\n{image_text}',
                            parent=self)

    def show_warnings(self) -> None:
        if self.model is None:
            return
        details='\n'.join(self.model.warnings[:25]) or '参照画像を含め、読み込み時の注意事項はありません。'
        if len(self.model.warnings)>25:
            details+=f'\nほか {len(self.model.warnings)-25} 件'
        messagebox.showinfo(self.title(),details,parent=self)

    def show_alignment_diagnostics(self) -> None:
        if self.model is None or self.current is None:
            return
        window = tk.Toplevel(self)
        window.title('パーツ位置診断（元画像サイズ基準）')
        window.geometry('900x580')
        window.transient(self)
        panel = ttk.Frame(window, padding=8)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='アトラス元寸法、JSON寸法、アンカー、透明領域の差を一覧表示します。',
                  wraplength=850).pack(anchor='w', pady=(0, 5))
        text = tk.Text(panel, wrap='none', font=('Consolas', 10))
        text.pack(fill='both', expand=True)
        text.insert('1.0', alignment_diagnostics(self.model, self.current, self.index_var.get()))
        text.configure(state='disabled')

    def list_bones(self) -> None:
        if self.model is None:
            return
        query=self.bone_filter.get().casefold().strip()
        self.visible_bones=[b for b in self.model.bones if query in b.name.casefold()]
        self.listbox.delete(0,tk.END)
        for bone in self.visible_bones:
            self.listbox.insert(tk.END,('× ' if bone.name in self.hidden else '○ ')+bone.name)
        if self.selected:
            for i,bone in enumerate(self.visible_bones):
                if bone.name==self.selected:
                    self.listbox.selection_set(i)
                    break

    def clear_selection(self, _event=None) -> None:
        """Clear the highlighted bone and its outline, from Esc or a button."""
        if self.selected is None:
            return
        self.selected = None
        self.details.set('')
        self.listbox.selection_clear(0, tk.END)
        self.draw_scene()

    def select_bone(self,_event=None) -> None:
        selected=self.listbox.curselection()
        if selected:
            bone=self.visible_bones[selected[0]]
            self.selected=bone.name
            detail = (f'{bone.name}\n親: {bone.parent or "(なし)"}\n描画順: {bone.z:g}\n'
                      f'画像: {", ".join(d.name for d in bone.displays)}')
            if self.model is not None and bone.displays:
                name = _normal_name(bone.displays[0].name)
                tex = self.model.textures.get(name)
                image = self.model.sprites.get(name)
                if tex is not None:
                    detail += f'\nJSON寸法: {tex.width} × {tex.height}'
                if image is not None:
                    detail += f'\nアトラス元寸法: {image.width} × {image.height}'
            self.details.set(detail)
            self.draw_scene()

    def toggle_bone(self,_event=None) -> None:
        indexes=self.listbox.curselection()
        if not indexes:
            return
        bone=self.visible_bones[indexes[0]]
        if bone.name in self.hidden:
            self.hidden.remove(bone.name)
        else:
            self.hidden.add(bone.name)
        self.list_bones()
        self.draw_scene()

    def click_canvas(self,event:tk.Event) -> None:
        x,y=event.x,event.y
        for name,poly in reversed(self.last_polygons):
            # Standard even/odd ray test supports rotated and skewed quadrilaterals.
            inside=False
            j=len(poly)-1
            for i in range(len(poly)):
                xi,yi=poly[i]; xj,yj=poly[j]
                if (yi>y)!=(yj>y) and x<(xj-xi)*(y-yi)/(yj-yi)+xi:
                    inside=not inside
                j=i
            if inside:
                if self.selected == name:
                    self.clear_selection()
                else:
                    self.selected=name
                    self.list_bones()
                    self.draw_scene()
                return
        # A click outside all bone polygons clears an existing selection.
        self.clear_selection()

    def change_movement(self,_event=None) -> None:
        if self.model is None:
            return
        self.stop()
        self.current=next((m for m in self.model.movements if m.name==self.movement_name.get()),self.model.movements[0])
        self.index_var.set(0)
        self.frame_scale.configure(to=max(0,self.current.duration-1))
        self.loop_var.set(self.current.loop)
        self.fit()

    def change_frame(self,_value=None) -> None:
        if self.current is None:
            return
        self.frame_label.set(f'{int(round(self.index_var.get()))} / {self.current.duration-1}')
        self.draw_scene()

    def goto_start(self) -> None:
        self.index_var.set(0)
        self.change_frame()

    def _fps(self) -> int:
        try:
            value=int(self.fps_var.get())
        except (TypeError,ValueError):
            value=APP_FPS_DEFAULT
        return max(APP_FPS_MIN,min(APP_FPS_MAX,value))

    def toggle_play(self) -> None:
        if self.is_playing:
            self.stop()
        elif self.current is not None:
            self.is_playing=True
            self.play_btn.configure(text='停止')
            self._tick()

    def _tick(self) -> None:
        if not self.is_playing or self.current is None:
            return
        n=int(round(self.index_var.get()))+1
        if n>=self.current.duration:
            if self.loop_var.get():
                n=0
            else:
                self.stop()
                return
        self.index_var.set(n)
        self.change_frame()
        self.timer_id=self.after(max(8,round(1000/self._fps())),self._tick)

    def stop(self) -> None:
        self.is_playing=False
        if self.timer_id is not None:
            self.after_cancel(self.timer_id)
            self.timer_id=None
        self.play_btn.configure(text='再生')

    def fit(self) -> None:
        if self.model is None or self.current is None:
            return
        try:
            x0,y0,x1,y1=pose_bounds(self.model,self.current)
        except ValueError as exc:
            messagebox.showerror(self.title(),str(exc),parent=self)
            return
        w=max(self.canvas.winfo_width(),100)
        h=max(self.canvas.winfo_height(),100)
        self.camera=((x0+x1)/2,(y0+y1)/2)
        self.scale=max(MIN_CANVAS_ZOOM,min(MAX_CANVAS_ZOOM,
             (w-10)/max(1,x1-x0),(h-10)/max(1,y1-y0)))
        self.draw_scene()

    def actual_size(self) -> None:
        """Show one logical sprite pixel as one screen pixel."""
        self.scale = 1.0
        self.draw_scene()
        self._status()

    def zoom_by(self,factor:float) -> None:
        self.scale=max(MIN_CANVAS_ZOOM,min(MAX_CANVAS_ZOOM,self.scale*factor))
        self.draw_scene()

    def _wheel(self,event:tk.Event) -> None:
        self.zoom_by(1.2 if event.delta>0 else 1/1.2)

    def _start_pan(self,event:tk.Event) -> None:
        self._pan_position=(event.x,event.y)

    def _pan(self,event:tk.Event) -> None:
        if self._pan_position is None:
            return
        px,py=self._pan_position
        dx=(event.x-px)/self.scale
        dy=(event.y-py)/self.scale
        camera_y_delta = -dy if self.flip_screen_y.get() else dy
        self.camera=(self.camera[0]-dx,self.camera[1]+camera_y_delta)
        self._pan_position=(event.x,event.y)
        self.draw_scene()

    def draw_scene(self) -> None:
        start_time = time.perf_counter()
        if self.model is None or self.current is None:
            return
        w=max(1,self.canvas.winfo_width())
        h=max(1,self.canvas.winfo_height())
        try:
            # Render at a bounded resolution in unusually large workspaces.
            ratio=min(1.0,math.sqrt(RENDER_PIXEL_LIMIT/(w*h)))
            rw=max(1,int(w*ratio));rh=max(1,int(h*ratio))
            gpu_success = False
            if self.gpu_var.get():
                try:
                    if self.gpu_renderer is None:
                        # GPUArmatureRenderer is defined in this same file.
                        self.gpu_renderer=GPUArmatureRenderer()
                    placed=compose_pose(self.model,self.current,self.index_var.get(),
                                        frozenset(self.hidden), self._hair_layer())
                    flags=(self.flip_uv_y.get(),self.flip_screen_y.get(),
                           self.flip_uv_x.get())
                    def affine(item,tw,th):
                        return sprite_pixel_to_screen(item,tw,th,self.scale*ratio,
                                                      self.camera,*flags)
                    image=self.gpu_renderer.render(placed,rw,rh,affine)
                    y_dir=1.0 if self.flip_screen_y.get() else -1.0
                    polys=[]
                    for item in placed:
                        poly=[(rw/2+(x-self.camera[0])*self.scale*ratio,
                               rh/2+y_dir*(y-self.camera[1])*self.scale*ratio)
                              for x,y in image_polygon(item)]
                        polys.append((item.bone_name,poly))
                    gpu_success=True
                    self.gpu_warning=''
                except Exception as exc:
                    # A missing OpenGL 3.3 driver or ModernGL dependency should
                    # not prevent inspection of the data. Switch to CPU once.
                    self.gpu_var.set(False)
                    self.gpu_warning=str(exc)
                    if self.gpu_renderer is not None:
                        self.gpu_renderer.close()
                        self.gpu_renderer=None
            if not gpu_success:
                image,polys=render_armature(self.model,self.current,self.index_var.get(),
                                          rw,rh,self.scale*ratio,self.camera,
                                          self.bounds_var.get(),self.selected,frozenset(self.hidden),
                                          flip_uv_y=self.flip_uv_y.get(),
                                          flip_screen_y=self.flip_screen_y.get(),
                                          flip_uv_x=self.flip_uv_x.get(),
                                          hair_layer=self._hair_layer(),
                                          opaque_background=True)
            if ratio != 1.0:
                # Upscale the preview to its actual canvas size, keeping hit-test
                # polygons mapped back from the smaller render resolution.
                image = image.resize((w,h), RESAMPLE_NEAREST)
            self.last_image=image
            self.last_polygons=[(n,[(x/ratio,y/ratio) for x,y in poly]) for n,poly in polys]
            # Reuse the same Tk photo handle and canvas image across frames.
            # v12 allocated a new PhotoImage and deleted every canvas item per
            # frame, adding significant Tk object churn during animation.
            if (self.rendered is None or self.rendered.width() != w
                    or self.rendered.height() != h or self._image_canvas_id is None):
                self.rendered=ImageTk.PhotoImage(image)
                self.canvas.delete('all')
                self._image_canvas_id=self.canvas.create_image(
                    0,0,anchor='nw',image=self.rendered,tags=('scene',))
            else:
                self.rendered.paste(image)
                self.canvas.delete('overlay')
            if gpu_success:
                for item,(name,poly) in zip(placed,self.last_polygons):
                    if not (self.bounds_var.get() or self.selected == name):
                        continue
                    color = ('#ffb000' if self.selected == name
                             else '#ff6363' if item.image is None else '#00d8ff')
                    coords = [v for p in poly for v in p]
                    self.canvas.create_line(*coords,poly[0][0],poly[0][1],
                                            fill=color,width=3 if self.selected==name else 1,
                                            tags=('overlay',))
                    if self.bounds_var.get() or self.selected==name:
                        cx=sum(p[0] for p in poly)/len(poly)
                        cy=sum(p[1] for p in poly)/len(poly)
                        self.canvas.create_text(cx+4,cy+4,text=name,fill=color,anchor='nw',tags=('overlay',))
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            backend = 'GPU' if gpu_success else 'CPU'
            self.render_time_label.set(f'{backend}: {elapsed_ms:.1f} ms/フレーム')
            if self.gpu_warning:
                self.status.set('GPU未使用・CPU描画: '+self.gpu_warning[:120])
        except (OSError,ValueError,MemoryError) as exc:
            self.status.set(f'描画エラー: {exc}')

    def save_png(self) -> None:
        if self.model is None or self.current is None:
            return
        path=filedialog.asksaveasfilename(parent=self,title='現在の姿勢を PNG 保存',
                                          defaultextension='.png',
                                          initialfile=f'{self.model.name}_{self.current.name}_{int(self.index_var.get()):03d}.png',
                                          filetypes=[('PNG','*.png')])
        if not path:
            return
        try:
            image, _=render_armature(self.model,self.current,self.index_var.get(),
                                    max(1,self.canvas.winfo_width()),
                                    max(1,self.canvas.winfo_height()),self.scale,self.camera,
                                    self.bounds_var.get(),None,frozenset(self.hidden),
                                    flip_uv_y=self.flip_uv_y.get(),
                                    flip_screen_y=self.flip_screen_y.get(),
                                    flip_uv_x=self.flip_uv_x.get(),
                                    hair_layer=self._hair_layer())
            bbox=image.getbbox()
            if bbox is not None:
                image=image.crop(bbox)
            image.save(path,format='PNG')
            self.status.set(f'PNG を保存しました: {path}')
        except (OSError,ValueError,MemoryError) as exc:
            messagebox.showerror(self.title(),f'保存に失敗しました:\n{exc}',parent=self)


def _ask_initial_source(root: Viewer) -> None:
    """Choose a .plist or .ExportJson after a double-click/argument-free launch."""
    if not root.winfo_exists():
        return
    filename = filedialog.askopenfilename(
        parent=root,
        title='開くファイルを選択（ExportJson / plist）',
        filetypes=[
            ('ExportJson / plist', '*.ExportJson *.json *.plist'),
            ('Cocos Studio ExportJson', '*.ExportJson *.json'),
            ('テクスチャアトラス plist', '*.plist'),
            ('すべてのファイル', '*.*'),
        ],
    )
    if not filename or not root.winfo_exists():
        return
    path = Path(filename)
    if path.suffix.lower() in ('.exportjson', '.json'):
        root.withdraw()
        try:
            ArmatureViewer(root, path)
        except Exception:
            root.deiconify()
            raise
    elif path.suffix.lower() == '.plist':
        root.open_plist(path, None)
    else:
        messagebox.showerror(APP_TITLE, 'ExportJson または plist ファイルを選択してください', parent=root)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='PLIST / Cocos Studio ExportJson viewer (Python 3.9+)')
    parser.add_argument('source',nargs='?',type=Path,help='*.plist / *.ExportJson')
    parser.add_argument('--plist',type=Path,help='ExportJson に使用するアトラス plist')
    parser.add_argument('--image',type=Path,help='アトラス PNG / WebP / JPEG')
    parser.add_argument('--fps',type=int,default=APP_FPS_DEFAULT,help='再生 FPS (1〜120)')
    parser.add_argument('--packed-rotation',choices=('cw','ccw'),default='cw')
    parser.add_argument('--uv-x',choices=('flip','normal'),default='normal',
                        help='ExportJson のパーツ内部の U 軸方向 (既定: normal)')
    parser.add_argument('--uv-y',choices=('flip','normal'),default='normal',
                        help='ExportJson のパーツ内部の V 軸方向 (既定: normal)')
    parser.add_argument('--screen-y',choices=('flip','normal'),default='normal',
                        help='ExportJson の画面 Y 方向 (既定: normal、Y が上方向)')
    parser.add_argument('--renderer',choices=('auto','gpu','cpu'),default='auto',
                        help='描画方式: GPU/CPU (auto=GPUを試行し利用できなければCPU)')
    parser.add_argument('--hair-layer',choices=('original','swap','front','back'),
                        default='swap',
                        help='髪の重なり: original=JSON順, swap=頭部装飾と入替 (既定), front=前面, back=背面')
    args=parser.parse_args(argv)
    if args.fps < APP_FPS_MIN or args.fps > APP_FPS_MAX:
        parser.error('--fps は 1〜120 の整数です')
    if args.source and args.source.suffix.lower() in ('.exportjson','.json'):
        root=Viewer(None,None,args.packed_rotation)
        root.withdraw()
        ArmatureViewer(root,args.source,args.plist,args.image,args.packed_rotation,args.fps,
                       flip_uv_y=(args.uv_y=='flip'),
                       flip_screen_y=(args.screen_y=='flip'),
                       flip_uv_x=(args.uv_x=='flip'),
                       renderer=args.renderer,
                       hair_layer=args.hair_layer)
    else:
        if args.plist:
            parser.error('--plist は ExportJson を指定したときに使用します')
        root=Viewer(args.source,args.image,args.packed_rotation)
        if args.source is None:
            root.after(150, lambda: _ask_initial_source(root))
    root.mainloop()
    return 0


if __name__=='__main__':
    sys.exit(main())
