"""
test_blinker.py — tile(2,2) blinker GoL test
=============================================

Boot all 9 tiles via flash. Firmware main() initialises current_grid
from the built-in PATTERN array; tile(2,2) gets a 3-cell horizontal
blinker at row 5, cols 3-5.

No host-SPI seeding is needed — the firmware writes the pattern itself.

Flow:
  1. Reset + clock start
  2. Start SPI flash model (provides firmware bytes)
  3. Wait for boot_controller to release CPUs (cpu_rst_n → 1)
  4. Wait 2 ms — firmware runs: init PATTERN, ghost exchange, GoL iterates
  5. Re-hold CPUs via host SPI for a clean (non-racing) SRAM read
  6. Backdoor-read tile(2,2) current_grid (0x0500, 100 bytes)
  7. Assert blinker is either horizontal (even steps) or vertical (odd steps)

Expected blinker cycle (period 2):
  Step 0 (HORIZ):              Step 1 (VERT):
  . . . . . . . . . .          . . . . . . . . . .
  . . . . . . . . . .          . . . . . . . . . .
  . . . . . . . . . .          . . . . . . . . . .
  . . . . . . . . . .          . . . . █ . . . . .
  . . . . . . . . . .          . . . . █ . . . . .
  . . . █ █ █ . . . .  →       . . . . █ . . . . .
  . . . . . . . . . .          . . . . . . . . . .
  . . . . . . . . . .          . . . . . . . . . .
  . . . . . . . . . .          . . . . . . . . . .
  . . . . . . . . . .          . . . . . . . . . .

Run with:
    make sim-blinker
"""

import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, RisingEdge, FallingEdge

# ============================================================
# CONFIG
# ============================================================
TILE_SIZE      = 10
SRAM_GRID_BASE = 0x0500   # current_grid base address in SRAM

TARGET_ROW = 2
TARGET_COL = 2
TARGET_ID  = (TARGET_ROW << 2) | TARGET_COL  # = 10

RESET_HOLD_MS = 2
SPI_HALF_NS   = 40        # host SPI half-period (~12.5 MHz, Mode 0)

_BASE = os.path.dirname(os.path.abspath(__file__))
_FW_CANDIDATES = [
    os.path.join(_BASE, "..", "src", "firmware", "firmware.bin"),
    os.path.join(_BASE, "..", "firmware", "firmware.bin"),
]

def load_firmware():
    for p in _FW_CANDIDATES:
        p = os.path.normpath(p)
        if os.path.exists(p):
            with open(p, "rb") as f:
                return list(f.read())
    return [0] * 2048

FIRMWARE = load_firmware()

# ============================================================
# SPI flash model
# ============================================================

async def spi_flash_responder(dut):
    while True:
        await FallingEdge(dut.flash_csb)
        for _ in range(32):
            await RisingEdge(dut.flash_clk)
        byte_idx = 0
        while True:
            for bit in range(7, -1, -1):
                await FallingEdge(dut.flash_clk)
                b = FIRMWARE[byte_idx] if byte_idx < len(FIRMWARE) else 0
                dut.flash_miso.value = (b >> bit) & 1
            byte_idx += 1
            if int(dut.flash_csb.value) == 1:
                break

# ============================================================
# Backdoor SRAM read helpers
# ============================================================

class TileHandle:
    def __init__(self, dut, r, c):
        self._dut = dut
        self._id  = (r << 2) | c
        self.clk  = dut.clk

async def sram_read_byte(tile, addr):
    tile._dut.tb_tile_sel.value = tile._id
    tile._dut.tb_raddr.value    = addr & 0x7FF
    await RisingEdge(tile.clk)
    await RisingEdge(tile.clk)
    return int(tile._dut.tb_rdata.value) & 0xFF

async def dump_grid(dut, tr, tc, label=""):
    """Read current_grid from tile(tr,tc) and pretty-print it."""
    tile = TileHandle(dut, tr, tc)
    grid = []
    for y in range(TILE_SIZE):
        row = []
        for x in range(TILE_SIZE):
            b = await sram_read_byte(tile, SRAM_GRID_BASE + y * TILE_SIZE + x)
            row.append(b & 1)
        grid.append(row)

    live = sum(grid[y][x] for y in range(TILE_SIZE) for x in range(TILE_SIZE))
    print(f"\n  [{label}]  tile({tr},{tc})  live_cells={live}")
    print("  +" + "-" * TILE_SIZE + "+")
    for row in grid:
        print("  |" + "".join("█" if v else "·" for v in row) + "|")
    print("  +" + "-" * TILE_SIZE + "+")
    return grid

async def dump_sram_raw(dut, tr, tc, start=0x500, length=100, label=""):
    """Hex dump of <length> SRAM bytes starting at <start>."""
    tile = TileHandle(dut, tr, tc)
    print(f"\n  [SRAM dump {label}]  tile({tr},{tc})  addr 0x{start:03x}..0x{start+length-1:03x}")
    for i in range(length):
        if i % 16 == 0:
            print(f"\n  {start+i:03x}: ", end="")
        b = await sram_read_byte(tile, start + i)
        print(f"{b:02x} ", end="")
    print()

# ============================================================
# Host SPI helpers (re-hold / release only — no grid seeding)
# ============================================================

async def spi_transfer(dut, tx_bytes):
    dut.host_csb.value = 0
    await Timer(SPI_HALF_NS, units="ns")
    rx_bytes = []
    for byte in tx_bytes:
        rx = 0
        for bit in range(7, -1, -1):
            dut.host_mosi.value = (byte >> bit) & 1
            await Timer(SPI_HALF_NS, units="ns")
            dut.host_sclk.value = 1
            await Timer(SPI_HALF_NS, units="ns")
            rx = (rx << 1) | int(dut.host_miso.value)
            dut.host_sclk.value = 0
            await Timer(SPI_HALF_NS, units="ns")
        rx_bytes.append(rx)
    dut.host_csb.value = 1
    await Timer(SPI_HALF_NS * 4, units="ns")
    return rx_bytes

async def host_set_reset(dut, hold):
    await spi_transfer(dut, [0x03, 0xFF if hold else 0x00])

# ============================================================
# Test
# ============================================================

@cocotb.test()
async def test_blinker_gol(dut):
    """
    Boot all 9 tiles from flash. The firmware initialises tile(2,2)
    with a 3-cell horizontal blinker (row 5, cols 3-5) from PATTERN[].
    After 2 ms of simulation time the blinker must have oscillated at
    least once and must be either horizontal or vertical.
    """
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())

    dut.rst.value         = 1
    dut.flash_miso.value  = 0
    dut.host_csb.value    = 1
    dut.host_sclk.value   = 0
    dut.host_mosi.value   = 0
    dut.tb_tile_sel.value = 0
    dut.tb_raddr.value    = 0

    await Timer(RESET_HOLD_MS, units="ms")
    dut.rst.value = 0
    cocotb.start_soon(spi_flash_responder(dut))

    # ── Stage 1: wait for flash boot ─────────────────────────────────────────
    dut._log.info("[boot] Waiting for boot_controller to finish loading firmware ...")
    while int(dut.mesh_inst.cpu_rst_n.value) == 0:
        await Timer(10, units="us")
    dut._log.info("[boot] Done — firmware loaded into all 9 tiles, CPUs released")

    # ── Stage 2: let GoL run ─────────────────────────────────────────────────
    # Firmware initialises current_grid from PATTERN (tile(2,2) gets the
    # horizontal blinker), exchanges ghost columns with E/W neighbours, then
    # iterates GoL continuously.  2 ms gives plenty of time for several steps.
    dut._log.info("[run] CPUs running — GoL iterating for 2 ms ...")
    await Timer(2, units="ms")

    # ── Stage 3: re-hold CPUs for a clean (non-racing) SRAM read ─────────────
    dut._log.info("[run] Re-holding CPUs for clean readback ...")
    await host_set_reset(dut, hold=True)
    await Timer(200, units="ns")

    # ── Stage 4: read tile(2,2) current_grid ─────────────────────────────────
    grid = await dump_grid(dut, TARGET_ROW, TARGET_COL,
                           label="tile(2,2) after GoL")
    await dump_sram_raw(dut, TARGET_ROW, TARGET_COL,
                        start=SRAM_GRID_BASE, length=TILE_SIZE * TILE_SIZE,
                        label="current_grid")

    live = sum(grid[y][x] for y in range(TILE_SIZE) for x in range(TILE_SIZE))
    dut._log.info(f"[result] live_cells = {live}")

    # ── Stage 5: assert blinker oscillated ───────────────────────────────────
    HORIZ = [[0] * TILE_SIZE for _ in range(TILE_SIZE)]
    HORIZ[5][3] = HORIZ[5][4] = HORIZ[5][5] = 1   # row 5, cols 3-5

    VERT = [[0] * TILE_SIZE for _ in range(TILE_SIZE)]
    VERT[4][4] = VERT[5][4] = VERT[6][4] = 1       # rows 4-6, col 4

    if grid == HORIZ:
        dut._log.info("[result] PASS — horizontal blinker (even iteration count)")
    elif grid == VERT:
        dut._log.info("[result] PASS — vertical blinker (odd iteration count)")
    elif live == 0:
        assert False, "All cells dead after 2 ms — firmware never ran or blinker was not seeded"
    elif live == 3:
        dut._log.error("[result] FAIL — 3 live cells but wrong positions")
        for row in grid:
            dut._log.error("  " + "".join("█" if v else "·" for v in row))
        assert False, f"3 live cells but not in a valid blinker orientation"
    else:
        dut._log.error(f"[result] FAIL — blinker diverged (live={live})")
        for row in grid:
            dut._log.error("  " + "".join("█" if v else "·" for v in row))
        assert False, f"Blinker diverged: live={live}, expected 3 in HORIZ or VERT pattern"

    assert live == 3, f"Live count wrong: {live}"
    dut._log.info("[done] Blinker GoL test PASSED")
