"""
test_host.py — Host-to-chip SPI seed pipeline test
====================================================

Tests the host SPI slave path:
  1. BOOT   — Flash model loads firmware into all 9 tile SRAMs via
              boot_controller (needed to bring the host bus live)
  2. SEED   — Host SPI writes input.pbm tile grid into current_grid
              in all 9 tiles via CMD 0x00 (broadcast write — one write
              hits every tile simultaneously)
  3. VERIFY — Backdoor ports (tb_tile_sel / tb_raddr / tb_rdata) read
              back each tile's SRAM and assert pixels match the PBM

Run with:
    make sim-host
"""

import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, RisingEdge, FallingEdge

# ============================================================
# CONFIG
# ============================================================

TILE_SIZE      = 10
MESH_R         = 3
MESH_C         = 3
FRAME_SIZE     = 30
SRAM_GRID_BASE = 0x0500   # current_grid base address in each tile's SRAM

RESET_HOLD_MS  = 2
SPI_HALF_NS    = 40       # host SPI half-period (Mode 0, ~12.5 MHz)

_BASE = os.path.dirname(os.path.abspath(__file__))
_FW_CANDIDATES = [
    os.path.join(_BASE, "..", "firmware", "firmware.bin"),
    os.path.join(_BASE, "..", "src", "firmware", "firmware.bin"),
]

# ============================================================
# Firmware loader
# ============================================================

def load_firmware():
    for p in _FW_CANDIDATES:
        p = os.path.normpath(p)
        if os.path.exists(p):
            print(f"[testbench] loading firmware from {p}")
            with open(p, "rb") as f:
                return list(f.read())
    print("[testbench] WARNING: firmware.bin not found — loading zeros")
    return [0] * 2048

FIRMWARE = load_firmware()

# ============================================================
# PBM loader
# ============================================================

def load_pbm(path):
    with open(path) as f:
        tokens = []
        for line in f:
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            tokens.extend(line.split())
    if tokens[0] != "P1":
        raise ValueError(f"Not a P1 PBM: {path}")
    w, h   = int(tokens[1]), int(tokens[2])
    pixels = [int(t) for t in tokens[3:]]
    if w != FRAME_SIZE or h != FRAME_SIZE:
        raise ValueError(f"PBM size {w}x{h} != expected {FRAME_SIZE}x{FRAME_SIZE}")
    return [[pixels[y * w + x] for x in range(w)] for y in range(h)]

def tile_slice(frame, tr, tc):
    """Extract the 10x10 sub-grid for tile (tr, tc) from a 30x30 frame."""
    return [[frame[tr * TILE_SIZE + y][tc * TILE_SIZE + x]
             for x in range(TILE_SIZE)]
            for y in range(TILE_SIZE)]

def print_grid(grid, label):
    print(f"\n  [{label}]")
    print(f"  +----------+")
    for row in grid:
        print(f"  |{''.join('█' if v else '·' for v in row)}|")
    print(f"  +----------+")
    live = sum(grid[y][x] for y in range(TILE_SIZE) for x in range(TILE_SIZE))
    print(f"  live cells: {live}")

INPUT_PBM = os.path.join(_BASE, "input.pbm")
if os.path.exists(INPUT_PBM):
    INPUT_FRAME = load_pbm(INPUT_PBM)
    print(f"[testbench] Loaded {INPUT_PBM}")
else:
    print(f"[testbench] WARNING: {INPUT_PBM} not found — using zero frame")
    INPUT_FRAME = [[0] * FRAME_SIZE for _ in range(FRAME_SIZE)]

# ============================================================
# SPI flash model (drives flash_miso for boot_controller)
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
# Backdoor port read helpers
# ============================================================

class TileHandle:
    def __init__(self, dut, r, c):
        self._dut = dut
        self._id  = (r << 2) | c
        self.clk  = dut.clk

async def sram_read_byte(tile, addr):
    """Read one byte from tile SRAM via top-level backdoor ports."""
    tile._dut.tb_tile_sel.value = tile._id
    tile._dut.tb_raddr.value    = addr & 0x7FF
    await RisingEdge(tile.clk)   # edge 1: SRAM captures addr
    await RisingEdge(tile.clk)   # edge 2: tb_rdata stable
    return int(tile._dut.tb_rdata.value) & 0xFF

# ============================================================
# Host SPI bit-bang helpers
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

async def host_write_sram(dut, addr, data):
    """CMD 0x00: broadcast write — same byte goes to all tiles at once."""
    await spi_transfer(dut, [
        0x00,
        (addr >> 8) & 0x07,   # 3 bits for 11-bit address space (0x000–0x7FF)
        addr & 0xFF,
        data & 0xFF,
    ])

async def host_write_sram_unicast(dut, tile_id, addr, data):
    """CMD 0x01: unicast write — targets one specific tile by tile_id."""
    await spi_transfer(dut, [
        0x01,
        (tile_id & 0x0F) << 4,  # tile_id in upper nibble, matches rd_tile encoding
        (addr >> 8) & 0x07,
        addr & 0xFF,
        data & 0xFF,
    ])

async def host_set_reset(dut, hold):
    """CMD 0x03: assert (hold=True) or release (hold=False) cpu reset."""
    await spi_transfer(dut, [0x03, 0xFF if hold else 0x00])

# ============================================================
# Test
# ============================================================

@cocotb.test()
async def test_host_seed_all_tiles(dut):
    """
    Boot all 9 tiles via flash, then use host SPI to write input.pbm
    into current_grid. Verify every tile matches the expected pattern
    using the top-level backdoor ports.
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

    # ── Stage 1: wait for flash boot to finish ───────────────────────────
    dut._log.info("[boot] Waiting for boot_controller to finish loading SRAMs ...")
    while int(dut.mesh_inst.cpu_rst_n.value) == 0:
        await Timer(10, units="us")
    dut._log.info("[boot] cpu_rst_n asserted — firmware in all tiles")

    # ── Stage 2: hold CPUs in reset so SRAM is free for host writes ──────
    dut._log.info("[seed] Asserting host reset via SPI ...")
    await host_set_reset(dut, hold=True)
    await Timer(100, units="ns")

    # Each tile gets its own unique 10x10 slice from input.pbm via CMD 0x01 unicast.
    expected_grids = {}
    for tr in range(MESH_R):
        for tc in range(MESH_C):
            tile_id = (tr << 2) | tc
            seed    = tile_slice(INPUT_FRAME, tr, tc)
            expected_grids[(tr, tc)] = seed
            print_grid(seed, f"seed for tile({tr},{tc})")
            dut._log.info(f"[seed] Writing tile({tr},{tc}) id={tile_id:#x} unicast ...")
            for y in range(TILE_SIZE):
                for x in range(TILE_SIZE):
                    addr = SRAM_GRID_BASE + y * TILE_SIZE + x
                    await host_write_sram_unicast(dut, tile_id, addr, seed[y][x])

    dut._log.info("[seed] All 9 tiles written")
    await Timer(1, units="us")

    # ── Stage 3: verify all 9 tiles via backdoor ports ───────────────────
    dut._log.info("[verify] Reading back all 9 tiles via backdoor ports ...")
    errors     = 0
    all_grids  = {}

    for tr in range(MESH_R):
        for tc in range(MESH_C):
            tile      = TileHandle(dut, tr, tc)
            seed      = expected_grids[(tr, tc)]
            tile_errs = 0
            actual_grid = []

            for y in range(TILE_SIZE):
                row = []
                for x in range(TILE_SIZE):
                    addr   = SRAM_GRID_BASE + y * TILE_SIZE + x
                    actual = (await sram_read_byte(tile, addr)) & 1
                    row.append(actual)
                    if actual != seed[y][x]:
                        dut._log.error(
                            f"  tile({tr},{tc}) [{y},{x}] "
                            f"expected={seed[y][x]} got={actual}"
                        )
                        tile_errs += 1
                actual_grid.append(row)

            all_grids[(tr, tc)] = actual_grid

            if tile_errs == 0:
                dut._log.info(f"  tile({tr},{tc}): PASS")
            else:
                print_grid(actual_grid, f"tile({tr},{tc}) ACTUAL (has {tile_errs} errors)")
                errors += tile_errs

    # ── Assembled 30×30 display ──────────────────────────────────────────
    SEP = "+" + "+".join(["-" * TILE_SIZE] * MESH_C) + "+"
    print(f"\n  [ASSEMBLED 30x30 — actual SRAM (all tiles same broadcast seed)]")
    print(f"  {SEP}")
    for tr in range(MESH_R):
        for y in range(TILE_SIZE):
            row_str = "  |"
            for tc in range(MESH_C):
                g = all_grids.get((tr, tc), [[0]*TILE_SIZE]*TILE_SIZE)
                row_str += "".join("█" if g[y][x] else "·" for x in range(TILE_SIZE))
                row_str += "|"
            print(row_str)
        print(f"  {SEP}")

    if errors == 0:
        dut._log.info("ALL 9 TILES PASS — host SPI unicast seed pipeline verified")
    assert errors == 0, f"{errors} cell(s) failed host SPI unicast seed check"
