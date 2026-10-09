"""Platform-appropriate UI fonts (Tk on macOS renders points ~25% smaller)."""
import sys

if sys.platform == "darwin":
    _FAMILY, _SCALE = "Helvetica Neue", 1.4
else:
    _FAMILY, _SCALE = "Segoe UI", 1.0


def ui_font(size: int, *style: str) -> tuple:
    return (_FAMILY, round(size * _SCALE), *style)


def ui_px(n: int) -> int:
    """Scale a pixel dimension to match the font scale."""
    return round(n * _SCALE)


def scale_named_fonts(root) -> None:
    """Swap Tk's and the sv_ttk theme's built-in fonts to the macOS UI font.

    Their sizes are already right on macOS (sv_ttk uses pixel sizes), so only
    the family changes — scaling them too makes dialogs oversized.
    """
    if _SCALE == 1.0:
        return
    import tkinter.font as tkfont
    for name in tkfont.names(root):
        tkfont.nametofont(name, root=root).configure(family=_FAMILY)
