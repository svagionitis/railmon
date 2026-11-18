---
name: tui-compatibility-tester
description: >-
  Validates TUI rendering across varying terminal dimensions (sub-minimum 40x10, standard 80x24,
  and ultra-wide 200x60), tests color degradation on monochrome consoles, and verifies
  Unicode Braille to ASCII character fallback.
---

# TUI Compatibility & Visual Resilience Tester

Validates the robustness of `railmon`'s curses-based terminal UI across different terminal geometries, character encodings, and color capability levels.

## When to Apply
- Modifying canvas rendering in `tui/renderer.py` or event loop handling in `tui/app.py`.
- Altering Braille drawing algorithms or corridor track projection in `tui/geo.py`.
- Validating rendering on constrained terminal environments (serial consoles, SSH clients, minimal containers).

## Test Procedures

### 1. Run Automated TUI Unit & Mock Tests
Run the comprehensive suite of TUI component tests:
```bash
python -m unittest tests/test_tui_controls.py tests/test_tui_geo.py tests/test_tui_renderer.py tests/test_tui_state.py tests/test_tui_integration.py -v
```
**Pass Criteria:** 100% test pass rate with zero curses rendering exceptions.

### 2. Verify Extreme Terminal Dimension Handling

Test rendering behavior across three key boundary sizes:

1. **Sub-Minimum Dimensions ($< 80\times24$, e.g., $60\times15$):**
   - Assert that the renderer suppresses normal canvas layout and displays the resize advisory banner without crashing or raising `curses.error: addstr() returned ERR`.
2. **Standard Dimensions ($80\times24$):**
   - Assert that all core panels (Header, Canvas Map, Active Trains Table, Status Bar) fit without overlapping or overflowing window bounds.
3. **Ultra-Wide Dimensions ($200\times60$):**
   - Assert that the Braille canvas scales proportionally and tracks render cleanly without coordinate wrapping.

### 3. Verify Encoding & ASCII Fallback
Run the renderer under forced non-UTF-8 conditions:
```bash
python -c "import locale; print('Locale preferred encoding:', locale.getpreferredencoding())"
```
- Verify that when Braille characters (`\u2800`–`\u28ff`) cannot be encoded, the renderer automatically uses ASCII substitutes (`-`, `|`, `*`, `#`) rather than raising `UnicodeEncodeError`.

### 4. Verify Monochrome / 8-Color Palette Graceful Degradation
- Test execution with `TERM=xterm` vs `TERM=vt100` vs `TERM=dumb`.
- Confirm that `curses.has_colors() == False` degrades to bold and reverse video text attributes without raising exceptions on color pair initializations.
