# Phase 1 Implementation Plan: Geospatial Projection & Braille Canvas Engine

Detailed technical specification, mathematical models, algorithm definitions, and test procedures for **Phase 1** of the Hellenic Railways (`railway.gov.gr`) Console TUI Map.

---

## 1. Overview & Objectives

Phase 1 establishes the mathematical and graphics foundation for the terminal user interface. It is responsible for translating geographic WGS84 coordinates (latitude and longitude) into terminal screen space, while compensating for non-square terminal font cells, and rasterizing high-definition track vectors using Unicode Braille sub-pixels.

### 1.1 Primary Deliverables
1. **`tui/geo.py`**:
   - Data structures: `GeoPoint`, `BoundingBox`, `Viewport`.
   - Projection engine: `GeoProjector` (forward and inverse Equirectangular projection with standard parallel scaling and terminal font aspect ratio correction).
   - Raster graphics engine: `BrailleCanvas` (2D bitmask buffer supporting $2 \times 4$ sub-pixel resolution per character cell, Bresenham line drawing, and character serialization).
2. **`tests/test_tui_geo.py`**:
   - Unit tests validating projection math, aspect ratio compensation, round-trip inversions, Braille bitmask encoding, line clipping, and corridor bounding boxes.

### 1.2 Upstream & Downstream Dependencies
- **Upstream:** Consumes WGS84 telemetry from [`train_tracker.py`](../../../train_tracker.py) (`TrainPosition.lat`, `TrainPosition.lng`).
- **Downstream:** Feeds rendered character buffers directly into the Phase 4 Curses viewport renderer (`tui/renderer.py`).

---

## 2. Mathematical Specifications

### 2.1 Geographic Coordinate System & Bounding Box

The Greek railway network operates within the following geographic limits:
- **South:** $37.5^\circ\text{N}$ (Peloponnese, Southern Attica)
- **North:** $41.3^\circ\text{N}$ (Promachonas, Idomeni, Dikea/Evros)
- **West:** $21.5^\circ\text{E}$ (Patras)
- **East:** $26.6^\circ\text{E}$ (Alexandroupoli, Pythio)

Standard reference latitude for Greece: $\phi_0 = 39.5^\circ\text{N} = 0.689405\text{ rad}$.

### 2.2 Terminal Font Aspect Ratio Compensation

Terminal emulators display monospaced fonts where a single character cell is roughly twice as tall as it is wide:
$$\text{Aspect Ratio } R = \frac{\text{Cell Height}}{\text{Cell Width}} \approx 2.0$$

In typical terminal fonts (e.g. 10x20px or 8x16px), 1 row of vertical movement equals 2 columns of horizontal movement. Without correction, a square geographical area renders as a vertical rectangle stretched by $200\%$.

### 2.3 Equirectangular Projection Formula

Let:
- $(\phi, \lambda)$ be the target latitude and longitude in degrees.
- $(\phi_0, \lambda_0)$ be the viewport center latitude and longitude in degrees.
- $S_x$ be the horizontal zoom scale factor (sub-pixels per degree of longitude).
- $R$ be the terminal cell aspect ratio (default: $2.0$).
- $(C_x, C_y)$ be the sub-pixel center of the canvas:
  $$C_x = \frac{\text{width\_subpixels}}{2}, \quad C_y = \frac{\text{height\_subpixels}}{2}$$

#### Forward Projection: $(\phi, \lambda) \to (P_x, P_y)$ (Sub-Pixel Space)

$$P_x = C_x + S_x \cdot (\lambda - \lambda_0) \cdot \cos\left(\frac{\pi}{180} \cdot \phi_0\right)$$

$$P_y = C_y - \frac{S_x}{R} \cdot (\phi - \phi_0)$$

*(Note: $P_y$ subtracts the latitude delta because terminal rows increase downwards from top to bottom).*

#### Inverse Projection: $(P_x, P_y) \to (\phi, \lambda)$

$$\lambda = \lambda_0 + \frac{P_x - C_x}{S_x \cdot \cos\left(\frac{\pi}{180} \cdot \phi_0\right)}$$

$$\phi = \phi_0 - \frac{R \cdot (P_y - C_y)}{S_x}$$

---

## 3. Braille Sub-Pixel Graphics Engine

### 3.1 Braille Character Encoding (`\u2800` – `\u28FF`)

Unicode standard provides an 8-dot Braille patterns block starting at code point `0x2800` (`⠀`, blank) up to `0x28FF` (`⣿`, all 8 dots). Each character cell maps to a $2 \times 4$ sub-pixel matrix:

```text
+-----------------------+
|  Cell Column 0 | Col 1|
|-----------------------|
| Row 0: Dot 1   | Dot 4|  -> Dot 1: 0x01 (bit 0), Dot 4: 0x08 (bit 3)
| Row 1: Dot 2   | Dot 5|  -> Dot 2: 0x02 (bit 1), Dot 5: 0x10 (bit 4)
| Row 2: Dot 3   | Dot 6|  -> Dot 3: 0x04 (bit 2), Dot 6: 0x20 (bit 5)
| Row 3: Dot 7   | Dot 8|  -> Dot 7: 0x40 (bit 6), Dot 8: 0x80 (bit 7)
+-----------------------+
```

### 3.2 Cell and Sub-Pixel Coordinate Mapping

For a terminal window of $W$ character columns and $H$ character rows:
- Sub-pixel canvas width: $W_{\text{sub}} = 2 \cdot W$
- Sub-pixel canvas height: $H_{\text{sub}} = 4 \cdot H$

Given a sub-pixel coordinate $(p_x, p_y)$:
1. **Cell Index:**
   $$\text{cell}_x = \lfloor p_x / 2 \rfloor, \quad \text{cell}_y = \lfloor p_y / 4 \rfloor$$
2. **Sub-Pixel Offset within Cell:**
   $$\text{sub}_x = p_x \pmod 2, \quad \text{sub}_y = p_y \pmod 4$$
3. **Bitmask Lookup Table:**
   $$\text{DOT\_MASKS}[\text{sub}_x][\text{sub}_y] = \begin{pmatrix} 0x01 & 0x02 & 0x04 & 0x40 \\ 0x08 & 0x10 & 0x20 & 0x80 \end{pmatrix}^T$$

To set sub-pixel $(p_x, p_y)$:
$$\text{buffer}[\text{cell}_y][\text{cell}_x] \;\vert=\; \text{DOT\_MASKS}[\text{sub}_x][\text{sub}_y]$$

To convert to Unicode character:
$$\text{char} = \text{chr}(0x2800 + \text{buffer}[\text{cell}_y][\text{cell}_x])$$

### 3.3 Bresenham Line Rasterization Algorithm

To connect railway station nodes and track polylines:
1. Clip line endpoints $(x_0, y_0)$ to $(x_1, y_1)$ to canvas boundaries $[0, W_{\text{sub}}-1] \times [0, H_{\text{sub}}-1]$.
2. Compute $\Delta x = |x_1 - x_0|$, $\Delta y = |y_1 - y_0|$, step directions $s_x = \text{sgn}(x_1 - x_0)$, $s_y = \text{sgn}(y_1 - y_0)$.
3. Initialize error term $err = \Delta x - \Delta y$.
4. Loop:
   - Call `set_pixel(x_0, y_0)`.
   - If $(x_0, y_0) == (x_1, y_1)$, break.
   - $e_2 = 2 \cdot err$.
   - If $e_2 > -\Delta y$, $err -= \Delta y$, $x_0 += s_x$.
   - If $e_2 < \Delta x$, $err += \Delta x$, $y_0 += s_y$.

---

## 4. Class Design & API Contract

### 4.1 Data Structures

```python
from dataclasses import dataclass
from typing import Tuple, List, Optional

@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lng: float

@dataclass(frozen=True)
class BoundingBox:
    min_lat: float
    max_lat: float
    min_lng: float
    max_lng: float

    @property
    def center(self) -> GeoPoint:
        return GeoPoint(
            lat=(self.min_lat + self.max_lat) / 2.0,
            lng=(self.min_lng + self.max_lng) / 2.0
        )

@dataclass
class Viewport:
    center: GeoPoint
    zoom: float = 1.0  # Multiplier relative to fit scale
    width_cells: int = 80
    height_cells: int = 24
    aspect_ratio: float = 2.0  # Terminal font aspect ratio (height / width)
```

### 4.2 GeoProjector Interface

```python
class GeoProjector:
    """Projects WGS84 geographic coordinates onto a 2D sub-pixel raster plane."""

    def __init__(self, viewport: Viewport):
        self.viewport = viewport

    def geo_to_subpixel(self, point: GeoPoint) -> Tuple[int, int]:
        """Convert WGS84 lat/lng to canvas sub-pixel (px, py)."""
        ...

    def geo_to_cell(self, point: GeoPoint) -> Tuple[int, int]:
        """Convert WGS84 lat/lng to character cell (col, row)."""
        ...

    def subpixel_to_geo(self, px: int, py: int) -> GeoPoint:
        """Convert canvas sub-pixel (px, py) back to WGS84 lat/lng."""
        ...

    def is_visible(self, point: GeoPoint) -> bool:
        """Check if a geographic point falls within current viewport bounds."""
        ...
```

### 4.3 BrailleCanvas Interface

```python
class BrailleCanvas:
    """A 2D terminal drawing canvas backed by Unicode Braille patterns (2x4 dots/cell)."""

    def __init__(self, width_cells: int, height_cells: int):
        self.width_cells = width_cells
        self.height_cells = height_cells
        self.sub_width = width_cells * 2
        self.sub_height = height_cells * 4
        self.buffer = bytearray(width_cells * height_cells)

    def clear(self) -> None:
        """Clear all dots in the canvas."""
        ...

    def set_pixel(self, px: int, py: int) -> None:
        """Turn on sub-pixel at (px, py). Silently ignores out-of-bounds coordinates."""
        ...

    def clear_pixel(self, px: int, py: int) -> None:
        """Turn off sub-pixel at (px, py)."""
        ...

    def draw_line(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """Draw a 1-pixel wide line between two sub-pixel points using Bresenham's algorithm."""
        ...

    def draw_polyline(self, points: List[Tuple[int, int]]) -> None:
        """Draw connected line segments across a sequence of sub-pixel points."""
        ...

    def render_rows(self) -> List[str]:
        """Render the canvas into a list of UTF-8 strings, one per terminal row."""
        ...

    def render_to_string(self) -> str:
        """Render the entire canvas to a multi-line string joined by newlines."""
        ...
```

---

## 5. Detailed Test Plan (`tests/test_tui_geo.py`)

All tests will run with standard `python3 -m unittest` in compliance with [`.agents/rules/testing.md`](../../../.agents/rules/testing.md).

| Test Case | Method | Verification Criterion |
|---|---|---|
| **Braille Bitmask Encoding** | `test_braille_dot_masks` | Verify each individual dot (1 to 8) yields the exact Unicode Braille pattern (`⠁`, `⠂`, `⠄`, `⠈`, `⠐`, `⠠`, `⡀`, `⢀`). |
| **All Dots / Empty Canvas** | `test_all_dots_and_empty` | Verify unpainted cell produces `'⠀'` (`0x2800`) and all 8 dots produce `'⣿'` (`0x28FF`). |
| **Bresenham Line Rasterization** | `test_bresenham_line_drawing` | Draw horizontal, vertical, diagonal, and single-point lines; verify expected dots are turned on. |
| **Clipping & Bounds Safety** | `test_subpixel_out_of_bounds` | Calling `set_pixel(-10, -5)` or `(1000, 1000)` does not raise exceptions or mutate valid buffer cells. |
| **Forward & Inverse Projection** | `test_projection_round_trip` | For test points (Athens, Thessaloniki, Larissa, Patras), verify $\text{subpixel\_to\_geo}(\text{geo\_to\_subpixel}(P)) \approx P$ within $\pm 0.001^\circ$. |
| **Aspect Ratio Linearity** | `test_aspect_ratio_scaling` | Verify horizontal distance scaling accurately incorporates $\cos(\phi_0)$ and vertical scale incorporates factor $R=2.0$. |
| **Corridor Bounding Boxes** | `test_corridor_presets` | Verify Greek railway presets (All Greece, Proastiakos, PATHE, Northern Greece) place key stations within visible viewport bounds. |

---

## 6. Implementation Step-by-Step Checklist

- [x] **Step 1: Module Directory Setup**
  - Create directory `tui/` and `tui/__init__.py`.
- [x] **Step 2: Geographic Domain Models**
  - Implement `GeoPoint`, `BoundingBox`, `Viewport`, and corridor preset constants in `tui/geo.py`.
- [x] **Step 3: Braille Canvas Implementation**
  - Implement `BrailleCanvas` with 2D bytearray buffer, `DOT_MASKS`, bitwise operations, Bresenham line algorithm, and string serialization.
- [x] **Step 4: GeoProjector Implementation**
  - Implement forward projection, inverse projection, aspect ratio correction, and visibility checking in `tui/geo.py`.
- [x] **Step 5: Comprehensive Unit Tests**
  - Create `tests/test_tui_geo.py` covering all mathematical and rasterization specifications.
- [x] **Step 6: Test Execution & Verification**
  - Run `python3 -m unittest discover tests` to ensure all tests pass with $100\%$ green status.
