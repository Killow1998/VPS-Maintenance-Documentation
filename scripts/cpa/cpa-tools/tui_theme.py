"""One black-background palette for every TUI screen."""
import curses


def apply_theme(window):
    colors = {"base": 0}
    if curses.has_colors():
        for number, (name, foreground) in enumerate((("base", curses.COLOR_WHITE),
                ("good", curses.COLOR_GREEN), ("warning", curses.COLOR_YELLOW),
                ("low", curses.COLOR_RED),
                ("muted", 250 if curses.COLORS >= 256 else curses.COLOR_WHITE)), 1):
            try:
                curses.init_pair(number, foreground, curses.COLOR_BLACK)
                colors[name] = curses.color_pair(number)
            except curses.error:
                pass
    window.bkgd(" ", colors["base"])
    window.attrset(colors["base"])
    return colors


def style(colors, attributes=0):
    if attributes & curses.A_DIM:
        attributes &= ~curses.A_DIM
        if not attributes & curses.A_COLOR:
            return colors.get("muted", colors["base"]) | attributes
    return attributes if attributes & curses.A_COLOR else colors["base"] | attributes
