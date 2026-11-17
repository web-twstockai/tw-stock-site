"""選股追蹤範圍（DECISIONS 2026-11-10 04:36：範圍由設定決定，範圍外的股票不算「排除」）。

設定：環境變數 TWSITE_UNIVERSE
- 未設定或 "sample30"：第二階段 30 檔驗證樣本，清單唯讀引用 pipeline/config.py 的 SAMPLE30（不在網站寫死代號）
- "all"：全市場（securities 全部普通股；第三階段）
- 其他值：視為清單檔路徑（每行一個代號，# 起為註解；相對路徑以專案根目錄為準）
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_CONFIG = PROJECT_ROOT / "pipeline" / "config.py"
DEFAULT_UNIVERSE = "sample30"
NAMED_LABELS = {"sample30": "{n} 檔驗證樣本", "all": "全市場普通股"}


@dataclass(frozen=True)
class Universe:
    name: str
    symbols: Optional[tuple]     # None＝全市場
    label: str                   # 例：「30 檔驗證樣本」
    source: str                  # 清單出處

    @property
    def text(self) -> str:
        return f"追蹤範圍：{self.label}"


class UniverseError(ValueError):
    pass


def _pipeline_list(var: str) -> list[str]:
    """唯讀解析 pipeline/config.py 的清單常數（不 import，避免執行 pipeline 程式）。"""
    tree = ast.parse(PIPELINE_CONFIG.read_text(encoding="utf-8"))
    env: dict[str, list] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            try:
                env[name] = ast.literal_eval(node.value)
            except ValueError:
                if isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.Add):
                    l, r = node.value.left, node.value.right
                    if isinstance(l, ast.Name) and isinstance(r, ast.Name) and l.id in env and r.id in env:
                        env[name] = list(env[l.id]) + list(env[r.id])
    if var not in env:
        raise UniverseError(f"{PIPELINE_CONFIG} 沒有 {var}")
    return [str(s) for s in env[var]]


def resolve(value: Optional[str] = None) -> Universe:
    v = (value if value is not None else os.environ.get("TWSITE_UNIVERSE", "")).strip() or DEFAULT_UNIVERSE
    if v == "all":
        return Universe("all", None, NAMED_LABELS["all"], "analysis/io_sqlite.py：securities 全部普通股")
    if v == "sample30":
        syms = _pipeline_list("SAMPLE30")
        return Universe("sample30", tuple(syms), NAMED_LABELS["sample30"].format(n=len(syms)),
                        "pipeline/config.py SAMPLE30")
    p = Path(v)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.is_file():
        raise UniverseError(f"TWSITE_UNIVERSE={v}：不是 sample30／all，也找不到清單檔")
    syms = [ln.split("#", 1)[0].strip() for ln in p.read_text(encoding="utf-8").splitlines()]
    syms = list(dict.fromkeys(s for s in syms if s))
    return Universe(p.stem, tuple(syms), f"{len(syms)} 檔（清單 {p.name}）", str(p))
