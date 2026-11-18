# Terminal Compatibility & Curses Rendering

`railmon` features an interactive terminal dashboard with Braille geospatial plotting and real-time status feeds. This document governs terminal dimension handling, Unicode character encoding fallbacks, and color palette degradation across diverse terminal emulators (xterm, tmux, Windows Terminal, Linux console, SSH).

## 1. Terminal Window Resizing (`KEY_RESIZE` / `SIGWINCH`)
- **Dynamic Dimension Querying:** Never cache terminal height and width once at startup. Read current dimensions on each render cycle using `stdscr.getmaxyx()`.
- **Catch `curses.KEY_RESIZE`:** When `stdscr.getch()` yields `curses.KEY_RESIZE`, trigger an immediate UI recalculation and re-render without throwing an unhandled exception or visual tearing.
- **Minimum Dimension Boundary:**
  - Minimum supported terminal dimension: **80 columns × 24 lines**.
  - If dimensions fall below the minimum, display a centered, non-crashing status banner:
    `"Terminal too small (current: WxH, minimum: 80x24). Please resize."`
  - Suppress canvas rendering until the window is expanded back to compliant dimensions.

## 2. Character Set & Unicode Braille Fallback
- **Unicode Braille Encoding:** The geospatial renderer uses Unicode Braille patterns (`\u2800` through `\u28FF`) for high-resolution 2×4 dot subpixel track and train positioning.
- **Encoding Detection:** Detect terminal encoding via `sys.stdout.encoding` or `locale.getpreferredencoding()`.
- **ASCII Fallback Matrix:** When running in an environment where UTF-8 is unavailable or unsupported:
  - Braille track dots $\to$ `.` or `-`
  - Train locomotives $\to$ `*` or `O`
  - Station hubs $\to$ `#` or `+`
  - Horizontal/vertical borders $\to$ standard ASCII `-`, `|`, `+`

## 3. Color Palette Degradation & Monochrome Consoles
- **Probe Color Support:** Always check `curses.has_colors()` before invoking `curses.start_color()`.
- **Handle Missing Palette Entries:** Never assume 256-color palette support. Wrap custom color definitions (`curses.init_color` or `curses.init_pair`) in try-except blocks:
  ```python
  try:
      curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
  except curses.error:
      pass  # Fall back to default terminal attributes
  ```
- **Monochrome & High-Contrast Fallback:** When colors are disabled or unavailable:
  - Convey operational train status (e.g., On-Time, Delayed, Stationary) using text indicators (`[OK]`, `[+12m]`, `[STOP]`) and text attributes (`curses.A_BOLD`, `curses.A_REVERSE`) rather than color alone.
