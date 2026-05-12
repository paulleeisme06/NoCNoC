"""
test_top.py — Cocotb testbench for top.v full pipeline
=======================================================

Tests the complete flow:
  1. BOOT    — SPI flash model loads firmware into all 9 tile SRAMs
               via boot_controller. Verifies firmware bytes in SRAM.
  2. SEED    — Host SPI bit-bang sends CMD 0x03/0xFF (hold reset),
               then CMD 0x00 writes to overwrite current_grid with
               blinker pattern in all 9 tiles.
  3. RUN     — Host SPI sends CMD 0x03/0x00 (release reset).
               Testbench waits for 0xCCCCCCCC on monitor_22_se.
  4. READBACK — Cocotb reads all 9 tile SRAMs via backdoor ports,
                assembles 30x30 frame, asserts pixel-level correctness,
                writes gol_gen1.pbm.

SPI bit-bang timing:
  Host SPI clock = sys_clk / 8 (12.5 MHz at 100 MHz sys_clk).
  All SPI transactions use Mode 0 (CPOL=0, CPHA=0).
  host_spi_slave samples MOSI on rising edge of host_sclk.
"""

import os
import struct
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, RisingEdge, FallingEdge

# ============================================================
# CONFIG
# ============================================================

TILE_SIZE  = 10
MESH_R     = 3
MESH_C     = 3
FRAME_SIZE = 30

_BASE = os.path.dirname(os.path.abspath(__file__))
_FW_CANDIDATES = [
    os.path.join(_BASE, "..", "firmware", "firmware.bin"),
    os.path.join(_BASE, "..", "src", "firmware", "firmware.bin"),
]
_ELF_CANDIDATES = [
    os.path.join(_BASE, "..", "firmware", "firmware.elf"),
    os.path.join(_BASE, "..", "src", "firmware", "firmware.elf"),
]

RESET_HOLD_MS   = 2
ITER_TIMEOUT_MS = 200
SEED_WAIT_US    = 500    # wait after releasing reset before GoL starts

# SPI bit-bang: half-period in ns (sys_clk = 10ns, so 40ns = clk/4 each edge)
SPI_HALF_NS = 40

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# ELF symbol reader
# ============================================================

def read_elf_symbol(elf_path, sym_name):
    if not os.path.exists(elf_path): return None
    with open(elf_path, "rb") as f: raw = f.read()
    if raw[0:4] != b'\x7fELF': return None
    e_shoff     = struct.unpack_from("<I", raw, 0x20)[0]
    e_shentsize = struct.unpack_from("<H", raw, 0x2e)[0]
    e_shnum     = struct.unpack_from("<H", raw, 0x30)[0]
    def sh(i):
        off = e_shoff + i * e_shentsize
        return struct.unpack_from("<IIIIIIIIII", raw, off)
    symtab_sh = strtab_sh = None
    for i in range(e_shnum):
        s = sh(i)
        if s[1] == 2:
            symtab_sh = s; strtab_sh = sh(s[6]); break
    if symtab_sh is None: return None
    for i in range(symtab_sh[5] // symtab_sh[9]):
        off = symtab_sh[4] + i * symtab_sh[9]
        st_name  = struct.unpack_from("<I", raw, off)[0]
        st_value = struct.unpack_from("<I", raw, off + 4)[0]
        end = raw.index(b'\x00', strtab_sh[4] + st_name)
        if raw[strtab_sh[4] + st_name:end].decode("ascii","replace") == sym_name:
            return st_value
    return None

# ============================================================
# Firmware + SRAM_GRID_BASE
# ============================================================

def load_firmware():
    for p in _FW_CANDIDATES:
        p = os.path.normpath(p)
        if os.path.exists(p):
            print(f"[testbench] loading firmware from {p}")
            with open(p, "rb") as f: return list(f.read())
    print("[testbench] WARNING: firmware.bin not found — loading zeros")
    return [0] * 2048

FIRMWARE = load_firmware()

_elf = next((os.path.normpath(p) for p in _ELF_CANDIDATES if os.path.exists(os.path.normpath(p))), None)
# current_grid is a #define (0x0500) not an ELF symbol — hardcode the fallback
SRAM_GRID_BASE = ((_elf and (read_elf_symbol(_elf, "current_grid")
                              or read_elf_symbol(_elf, "_bss_start")))
                  or 0x0500)
print(f"[testbench] SRAM_GRID_BASE = 0x{SRAM_GRID_BASE:04x}")

# ============================================================
# SPI Flash model (for boot_controller)
# ============================================================

async def spi_flash_model(dut):
    """Respond to boot_controller's SPI flash reads."""
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
# DUT helpers — backdoor port access
# ============================================================

class TileHandle:
    """Wraps the top-level backdoor ports to address a specific tile."""
    def __init__(self, dut, r, c):
        self._dut = dut
        self._id  = (r << 2) | c
        self.clk  = dut.clk

def get_tile(dut, r, c):
    return TileHandle(dut, r, c)

async def sram_read_byte(tile, addr):
    """Read one byte from tile SRAM via top-level backdoor ports."""
    tile._dut.tb_tile_sel.value = tile._id
    tile._dut.tb_raddr.value    = addr & 0x7FF
    await RisingEdge(tile.clk)   # edge 1: SRAM captures addr
    await RisingEdge(tile.clk)   # edge 2: tb_rdata stable
    return int(tile._dut.tb_rdata.value) & 0xFF

# ============================================================
# SRAM dump + ASCII visualization helpers
# ============================================================

SRAM_SIZE = 2048   # bytes per tile

async def dump_sram_hex(tile, base, length, label=""):
    """Print a hex dump of 'length' bytes from tile SRAM starting at base."""
    print(f"\n  ┌─ {label} (0x{base:03x} – 0x{base+length-1:03x}) {'─'*20}")
    for i in range(0, length, 16):
        chunk = []
        for j in range(16):
            if (i + j) < length:
                chunk.append(await sram_read_byte(tile, base + i + j))
        hex_part  = " ".join(f"{b:02x}" for b in chunk)
        asc_part  = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  │ 0x{base+i:03x}: {hex_part:<48}  {asc_part}")
    print(f"  └{'─'*60}")

async def dump_sram_full_hex(tile, label="full SRAM"):
    """Hex dump the entire SRAM for one tile."""
    await dump_sram_hex(tile, 0, SRAM_SIZE, label)

async def dump_grid_ascii(tile, label="current_grid"):
    """
    Read current_grid from SRAM and render as ASCII art.
    Shows a border, row numbers, and uses █/· for live/dead cells.
    Also shows the raw byte values in a compact table below.
    """
    grid = []
    for y in range(TILE_SIZE):
        row = []
        for x in range(TILE_SIZE):
            row.append(await sram_read_byte(tile, SRAM_GRID_BASE + y * TILE_SIZE + x))
        grid.append(row)

    live_count = sum(grid[y][x] & 1 for y in range(TILE_SIZE) for x in range(TILE_SIZE))

    print(f"\n  ┌─ {label}  (base=0x{SRAM_GRID_BASE:03x}, live={live_count}) {'─'*14}")
    print(f"  │    0123456789")
    print(f"  │   ┌──────────┐")
    for y in range(TILE_SIZE):
        row_str = "".join("█" if grid[y][x] & 1 else "·" for x in range(TILE_SIZE))
        print(f"  │ {y} │{row_str}│")
    print(f"  │   └──────────┘")
    print(f"  │  raw bytes: {[grid[y][x] for y in range(TILE_SIZE) for x in range(TILE_SIZE)]}")
    print(f"  └{'─'*50}")
    return grid

async def dump_checkpoint(dut, title, tiles="all", show_firmware_bytes=16,
                    show_grid=True, show_full_sram=False):
    """
    Master dump function — called at each checkpoint.

    title            : heading string
    tiles            : "all" or list of (row,col) tuples
    show_firmware_bytes : number of bytes from addr 0 to show as hex (0 = skip)
    show_grid        : show ASCII art of current_grid
    show_full_sram   : show full SRAM hex dump (verbose)
    """
    border = "═" * 60
    print(f"\n╔{border}╗")
    print(f"║  CHECKPOINT: {title:<45}║")
    print(f"╚{border}╝")

    tile_list = (
        [(r, c) for r in range(MESH_R) for c in range(MESH_C)]
        if tiles == "all"
        else tiles
    )

    for (tr, tc) in tile_list:
        tile = get_tile(dut, tr, tc)
        print(f"\n  ▶ tile({tr},{tc})  TILE_ID=0x{tr*4+tc:X}")

        if show_firmware_bytes > 0:
            await dump_sram_hex(tile, 0, show_firmware_bytes,
                          f"firmware header (first {show_firmware_bytes}B)")

        # Always show the region around SRAM_GRID_BASE
        context_start = max(0, SRAM_GRID_BASE - 4)
        context_len   = TILE_SIZE * TILE_SIZE + 8
        await dump_sram_hex(tile, context_start, context_len,
                      f"current_grid region (±4B context)")

        if show_grid:
            await dump_grid_ascii(tile, f"current_grid visual")

        if show_full_sram:
            await dump_sram_full_hex(tile, "full SRAM")

# ============================================================
# SPI bit-bang master
# ============================================================

async def spi_transfer(dut, tx_bytes: list) -> list:
    """
    Bit-bang a complete SPI transaction (Mode 0, MSB first).
    Asserts host_csb low, clocks all bytes, deasserts host_csb.
    Returns list of received bytes (MISO).
    """
    dut.host_csb.value  = 0
    await Timer(SPI_HALF_NS, units="ns")

    rx_bytes = []
    for byte in tx_bytes:
        rx = 0
        for bit in range(7, -1, -1):
            # Set MOSI before rising edge
            dut.host_mosi.value = (byte >> bit) & 1
            await Timer(SPI_HALF_NS, units="ns")

            # Rising edge — slave samples MOSI
            dut.host_sclk.value = 1
            await Timer(SPI_HALF_NS, units="ns")

            # Sample MISO on rising edge
            rx = (rx << 1) | int(dut.host_miso.value)

            # Falling edge
            dut.host_sclk.value = 0
            await Timer(SPI_HALF_NS, units="ns")

        rx_bytes.append(rx)

    dut.host_csb.value = 1
    await Timer(SPI_HALF_NS * 4, units="ns")  # CS deassert hold
    return rx_bytes

async def host_write_sram(dut, addr: int, data: int):
    """
    CMD 0x00: write one byte to all tile SRAMs at addr.
    Transaction: [0x00, addr[10:8], addr[7:0], data]
    """
    await spi_transfer(dut, [
        0x00,
        (addr >> 8) & 0x07,   # 3 bits for 11-bit address space (0x000–0x7FF)
        addr & 0xFF,
        data & 0xFF
    ])

async def host_set_reset(dut, hold: bool):
    """
    CMD 0x03: assert (hold=True) or release (hold=False) cpu reset.
    Transaction: [0x03, 0xFF or 0x00]
    """
    await spi_transfer(dut, [0x03, 0xFF if hold else 0x00])

async def host_read_sram(dut, tile_id: int, addr: int) -> int:
    """
    CMD 0x02: read one byte from tile tile_id at addr.
    Transaction: [0x02, tile_id<<4, addr[9:8], addr[7:0], 0x00(dummy)]
    Returns the byte read back.
    """
    rx = await spi_transfer(dut, [
        0x02,
        (tile_id & 0xF) << 4,
        (addr >> 8) & 0x03,
        addr & 0xFF,
        0x00  # dummy byte to clock out the result
    ])
    return rx[4]  # result is in the 5th byte

# ============================================================
# PBM loader
# ============================================================

def load_pbm(path: str) -> list:
    """
    Load a P1 (plain PBM) file and return a 2D list of ints (0 or 1).
    Ignores comment lines starting with #.
    Raises ValueError if dimensions don't match FRAME_SIZE x FRAME_SIZE.
    """
    with open(path) as f:
        tokens = []
        for line in f:
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            tokens.extend(line.split())

    if tokens[0] != "P1":
        raise ValueError(f"Not a P1 PBM file: {path}")

    w, h   = int(tokens[1]), int(tokens[2])
    pixels = [int(t) for t in tokens[3:]]

    if w != FRAME_SIZE or h != FRAME_SIZE:
        raise ValueError(
            f"PBM size {w}x{h} doesn't match expected {FRAME_SIZE}x{FRAME_SIZE}"
        )
    if len(pixels) != w * h:
        raise ValueError(
            f"PBM pixel count {len(pixels)} doesn't match {w*h}"
        )

    return [[pixels[y * w + x] for x in range(w)] for y in range(h)]

def frame_to_tile_grid(frame: list, tr: int, tc: int) -> list:
    """Extract the 10x10 slice for tile (tr, tc) from a 30x30 frame."""
    return [[frame[tr * TILE_SIZE + y][tc * TILE_SIZE + x]
             for x in range(TILE_SIZE)]
            for y in range(TILE_SIZE)]

def print_pbm(frame: list, label: str):
    """Print a loaded PBM as ASCII with tile grid lines."""
    print(f"\n  ┌─ {label} {'─'*40}")
    header = "   " + "".join(str(i % 10) for i in range(FRAME_SIZE))
    print(f"  │{header}")
    print(f"  │  +{'─'*FRAME_SIZE}+")
    for y, row in enumerate(frame):
        sep = "─" if y in (10, 20) else " "
        bar = "├" if y in (10, 20) else "│"
        print(f"  │{y:2d}{bar}" + "".join("█" if v else "·" for v in row) + f"{bar}")
    print(f"  │  +{'─'*FRAME_SIZE}+")
    live = sum(frame[y][x] for y in range(FRAME_SIZE) for x in range(FRAME_SIZE))
    print(f"  │  live cells: {live}")
    print(f"  └{'─'*50}")

# ============================================================
# GoL reference
# ============================================================

def gol_step_frame(frame: list) -> list:
    """One GoL step on a full 30x30 frame with fixed (dead) boundaries."""
    new_f = [[0]*FRAME_SIZE for _ in range(FRAME_SIZE)]
    for y in range(FRAME_SIZE):
        for x in range(FRAME_SIZE):
            n = sum(
                frame[y+dy][x+dx]
                for dy in (-1,0,1) for dx in (-1,0,1)
                if (dy or dx) and 0<=y+dy<FRAME_SIZE and 0<=x+dx<FRAME_SIZE
            )
            new_f[y][x] = 1 if (frame[y][x] and n in (2,3)) or \
                               (not frame[y][x] and n == 3) else 0
    return new_f

# Load input PBM — this is the single source of truth for the seed
INPUT_PBM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input.pbm")

if os.path.exists(INPUT_PBM):
    INPUT_FRAME = load_pbm(INPUT_PBM)
    print(f"[testbench] Loaded {INPUT_PBM}")
else:
    # Fallback: generate blinker programmatically if file missing
    print(f"[testbench] WARNING: {INPUT_PBM} not found — generating blinker seed")
    INPUT_FRAME = [[0]*FRAME_SIZE for _ in range(FRAME_SIZE)]
    for tr in range(MESH_R):
        for tc in range(MESH_C):
            cx, cy = TILE_SIZE//2, TILE_SIZE//2
            for dy in (-1, 0, 1):
                INPUT_FRAME[tr*TILE_SIZE + cy + dy][tc*TILE_SIZE + cx] = 1

GEN0 = INPUT_FRAME
GEN1 = gol_step_frame(GEN0)
GEN2 = gol_step_frame(GEN1)
REF  = [GEN0, GEN1, GEN2]

# ============================================================
# Frame read + assert
# ============================================================

async def read_tile_grid(tile):
    grid = []
    for y in range(TILE_SIZE):
        row = []
        for x in range(TILE_SIZE):
            val = await sram_read_byte(tile, SRAM_GRID_BASE + y*TILE_SIZE + x)
            row.append(val & 1)
        grid.append(row)
    return grid

async def read_30x30(dut):
    f = [[0]*FRAME_SIZE for _ in range(FRAME_SIZE)]
    for tr in range(MESH_R):
        for tc in range(MESH_C):
            g = await read_tile_grid(get_tile(dut, tr, tc))
            for ly in range(TILE_SIZE):
                for lx in range(TILE_SIZE):
                    f[tr*TILE_SIZE+ly][tc*TILE_SIZE+lx] = g[ly][lx]
    return f

def write_pbm(frame, filename):
    path = os.path.join(OUT_DIR, filename)
    with open(path, "w") as f:
        f.write(f"P1\n# test_top.py\n{FRAME_SIZE} {FRAME_SIZE}\n")
        for row in frame:
            f.write(" ".join(str(v) for v in row) + "\n")
    print(f"[testbench] wrote {path}")

def print_frame(frame, label):
    SEP = "+" + "+".join(["-" * TILE_SIZE] * MESH_C) + "+"
    print(f"\n{'='*38}  {label}")
    print(f"  {SEP}")
    for ridx, row in enumerate(frame):
        if ridx > 0 and ridx % TILE_SIZE == 0:
            print(f"  {SEP}")
        line = ""
        for cidx, v in enumerate(row):
            if cidx % TILE_SIZE == 0:
                line += "|"
            line += "█" if v else "·"
        print(f"  {line}|")
    print(f"  {SEP}")
    print("=" * 40)

async def assert_frame(dut, gen_idx, label):
    act = await read_30x30(dut)
    ref = REF[gen_idx]
    write_pbm(act, f"gol_gen{gen_idx}.pbm")
    print_frame(act, label)

    mismatches = [(y, x)
                  for y in range(FRAME_SIZE)
                  for x in range(FRAME_SIZE)
                  if act[y][x] != ref[y][x]]

    if mismatches:
        print(f"  ✗ {len(mismatches)} pixel mismatches:")
        for (y, x) in mismatches[:20]:
            tr, tc = y//TILE_SIZE, x//TILE_SIZE
            print(f"    frame({y},{x}) tile({tr},{tc}) "
                  f"ref={ref[y][x]} actual={act[y][x]}")
    else:
        print(f"  ✓ All {FRAME_SIZE*FRAME_SIZE} pixels correct")

    return len(mismatches)

# ============================================================
# NOC signal monitor  (watches monitor_22_se)
# ============================================================

async def wait_for_monitor(dut, target_payload, timeout_ms):
    """
    Poll monitor_22_se on every rising clock edge.
    target_payload is the lower 29 bits of the expected 32-bit signal word.
    Returns True if seen within timeout_ms, False otherwise.
    """
    clk_period_ns = 10
    max_cycles    = int(timeout_ms * 1e6 / clk_period_ns)
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        try:
            flit = int(dut.monitor_22_se.value)
            if flit & (1 << 33):                    # valid bit
                payload = flit & 0x1FFFFFFF         # lower 29 bits
                if payload == target_payload:
                    return True
        except Exception:
            pass
    return False

# ============================================================
# Boot sequence
# ============================================================

async def do_boot(dut):
    """Start clock, reset, run SPI flash model, wait for cpu_rst_n."""
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())

    # Initial pin state
    dut.rst.value         = 1
    dut.flash_miso.value  = 0
    dut.host_csb.value    = 1
    dut.host_sclk.value   = 0
    dut.host_mosi.value   = 0
    dut.tb_tile_sel.value = 0
    dut.tb_raddr.value    = 0

    await Timer(RESET_HOLD_MS, units="ms")
    dut.rst.value = 0

    cocotb.start_soon(spi_flash_model(dut))

    dut._log.info("[boot] Waiting for boot_controller to finish...")
    while int(dut.mesh_inst.cpu_rst_n.value) == 0:
        await Timer(10, units="us")
    dut._log.info("[boot] cpu_rst_n asserted — firmware loaded.")

# ============================================================
# STAGE 1: Verify firmware loaded into all 9 SRAMs
# ============================================================

async def verify_firmware_load(dut):
    """
    Check first 16 bytes of each tile's SRAM match firmware.bin.
    A mismatch here means boot_controller or the SPI flash model is broken.
    """
    dut._log.info("[stage1] Verifying firmware load across all 9 tiles...")
    errors = 0
    for tr in range(MESH_R):
        for tc in range(MESH_C):
            tile = get_tile(dut, tr, tc)
            for addr in range(16):
                actual   = await sram_read_byte(tile, addr)
                expected = FIRMWARE[addr] if addr < len(FIRMWARE) else 0
                if actual != expected:
                    dut._log.error(
                        f"  tile({tr},{tc}) addr=0x{addr:03x} "
                        f"expected=0x{expected:02x} actual=0x{actual:02x}"
                    )
                    errors += 1
    if errors == 0:
        dut._log.info("[stage1] ✓ Firmware verified in all 9 tiles")
    return errors

# ============================================================
# STAGE 2: Host SPI overwrites current_grid with blinker seed
# ============================================================

async def host_write_seed(dut):
    """
    1. Assert reset via SPI (cores stay frozen)
    2. Write blinker seed bytes to current_grid address in all SRAMs
    3. Verify via backdoor ports
    """
    dut._log.info("[stage2] Asserting reset via host SPI...")
    await host_set_reset(dut, hold=True)
    await Timer(100, units="ns")

    # Print the input PBM so it's visible in the log
    print_pbm(INPUT_FRAME, "input.pbm — seed being written to all tiles")

    dut._log.info(f"[stage2] Writing seed to SRAM from input.pbm (base=0x{SRAM_GRID_BASE:04x})...")
    # All tiles get the same 10x10 slice (tile 0,0 slice — same for all since
    # input.pbm has identical patterns in each tile block)
    tile_seed = frame_to_tile_grid(INPUT_FRAME, 0, 0)
    for y in range(TILE_SIZE):
        for x in range(TILE_SIZE):
            addr = SRAM_GRID_BASE + y * TILE_SIZE + x
            val  = tile_seed[y][x]
            await host_write_sram(dut, addr, val)

    # Verify via backdoor ports on tile(0,0) — all tiles get same write
    dut._log.info("[stage2] Verifying seed via backdoor ports on tile(0,0)...")
    tile00  = get_tile(dut, 0, 0)
    errors  = 0
    for y in range(TILE_SIZE):
        for x in range(TILE_SIZE):
            addr     = SRAM_GRID_BASE + y * TILE_SIZE + x
            actual   = (await sram_read_byte(tile00, addr)) & 1
            expected = tile_seed[y][x]
            if actual != expected:
                dut._log.error(
                    f"  seed mismatch at ({y},{x}): "
                    f"expected={expected} actual={actual}"
                )
                errors += 1

    if errors == 0:
        dut._log.info("[stage2] ✓ Blinker seed written and verified")
    return errors

# ============================================================
# STAGE 3: Release reset, run GoL, wait for stable signal
# ============================================================

async def run_gol(dut):
    """
    Release reset via SPI, wait for 0xCCCCCCCC.
    Watches monitor_22_se. Prints debug state every 10ms.
    """
    SIG_GEN_STABLE   = 0x0CCCCCCC   # lower 29 bits of 0xCCCCCCCC
    SIG_BOOT_ALIVE   = 0x0AAAAAAA   # lower 29 bits of 0xAAAAAAAA
    SIG_MATH_DONE    = 0x0DDDDDDD   # lower 29 bits of 0xDDDDDDDD

    dut._log.info("[stage3] Releasing reset via host SPI — cores starting...")
    await host_set_reset(dut, hold=False)

    # ── Immediate post-release diagnostics ──────────────────────────────
    await Timer(100, units="ns")
    try:
        cpu_rst_n  = int(dut.mesh_inst.cpu_rst_n.value)
        host_rst   = int(dut.host_rst.value)
        host_rst_en= int(dut.host_rst_en.value)
        dut._log.info(
            f"[stage3] POST-RELEASE: cpu_rst_n={cpu_rst_n} "
            f"host_rst_en={host_rst_en} host_rst={host_rst}"
        )
    except Exception as e:
        dut._log.warning(f"[stage3] Could not probe signals: {e}")

    # ── Poll for signal on monitor_22_se ────────────────────────────────
    dut._log.info("[stage3] Polling for NOC signals (checking every 10ms)...")

    clk_period_ns  = 10
    poll_interval  = int(10e6 / clk_period_ns)   # 10ms in cycles
    total_cycles   = int(ITER_TIMEOUT_MS * 1e6 / clk_period_ns)
    cycles_done    = 0
    seen           = False
    last_flit      = 0

    while cycles_done < total_cycles:
        for _ in range(poll_interval):
            await RisingEdge(dut.clk)
            cycles_done += 1

            # Check monitor_22_se
            try:
                flit = int(dut.monitor_22_se.value)
                if flit & (1 << 33):
                    payload = flit & 0x1FFFFFFF
                    if payload != last_flit:
                        last_flit = payload
                        if payload == SIG_GEN_STABLE:
                            dut._log.info(f"[stage3] ✓ 0xCCCCCCCC on monitor_22_se @ cycle {cycles_done}")
                            seen = True
                        elif payload == SIG_BOOT_ALIVE:
                            dut._log.info(f"[stage3] ► 0xAAAAAAAA (boot alive) on monitor_22_se")
                        elif payload == SIG_MATH_DONE:
                            dut._log.info(f"[stage3] ► 0xDDDDDDDD (math done) on monitor_22_se")
                        else:
                            dut._log.info(f"[stage3] ► flit 0x{payload:08x} on monitor_22_se")
            except Exception:
                pass

            if seen:
                break

        if seen:
            break

        # 10ms heartbeat
        elapsed_ms = cycles_done * clk_period_ns / 1e6
        try:
            cpu_rst_n = int(dut.mesh_inst.cpu_rst_n.value)
            dut._log.info(
                f"[stage3] {elapsed_ms:.0f}ms elapsed — "
                f"cpu_rst_n={cpu_rst_n} last_flit=0x{last_flit:08x} — still waiting..."
            )
        except Exception:
            dut._log.info(f"[stage3] {elapsed_ms:.0f}ms elapsed — still waiting...")

    if seen:
        await Timer(5, units="us")
        dut._log.info("[stage3] ✓ Generation 1 stable — sampling SRAM")
    else:
        dut._log.error(
            "[stage3] ✗ TIMEOUT — 0xCCCCCCCC never seen.\n"
            "  Possible causes:\n"
            "  1. tile.rst still high (host_rst not releasing)\n"
            "  2. tile.boot_mode still high (cpu_rst_n wiring issue)\n"
            "  3. firmware not executing (PC stuck)\n"
            "  4. NOC signal sent to wrong tile ID / wrong flit bits\n"
            "  Check the heartbeat lines above for cpu_rst_n state."
        )

    return seen

# ============================================================
# FULL PIPELINE TEST
# ============================================================

@cocotb.test()
async def test_full_pipeline(dut):
    """
    Complete end-to-end test:
      Stage 1 — boot_controller loads firmware into all 9 SRAMs
      Stage 2 — host SPI overwrites current_grid with blinker seed
      Stage 3 — host releases reset, GoL runs, waits for stable signal
      Stage 4 — backdoor ports read 30x30 result, pixel assert, PBM write
    """

    # ---- BOOT ----
    await do_boot(dut)

    # ──────────────────────────────────────────────────────────
    # CHECKPOINT A: right after boot_controller finishes
    # ──────────────────────────────────────────────────────────
    await dump_checkpoint(dut,
        title="AFTER BOOT — firmware loaded, CPUs not yet released",
        tiles=[(0,0), (1,1), (2,2)],   # sample 3 tiles (diagonal)
        show_firmware_bytes=32,
        show_grid=True,
        show_full_sram=False)

    # ---- STAGE 1: firmware load ----
    fw_errors = await verify_firmware_load(dut)

    # ---- STAGE 2: host seeds current_grid ----
    seed_errors = await host_write_seed(dut)

    # ──────────────────────────────────────────────────────────
    # CHECKPOINT B: after host SPI has overwritten current_grid
    # ──────────────────────────────────────────────────────────
    await dump_checkpoint(dut,
        title="AFTER HOST SEED — current_grid overwritten with blinker",
        tiles="all",
        show_firmware_bytes=0,     # skip — firmware bytes unchanged
        show_grid=True,
        show_full_sram=False)

    # ---- STAGE 3: run GoL ----
    await run_gol(dut)

    # ──────────────────────────────────────────────────────────
    # CHECKPOINT C: after 0xCCCCCCCC — generation 1 stable
    # ──────────────────────────────────────────────────────────
    await dump_checkpoint(dut,
        title="AFTER GOL GEN 1 — current_grid should be horizontal blinker",
        tiles="all",
        show_firmware_bytes=0,
        show_grid=True,
        show_full_sram=False)

    # ---- STAGE 4: readback + assert ----
    dut._log.info("[stage4] Reading back 30x30 frame via backdoor ports...")
    mismatches = await assert_frame(dut, 1, "Generation 1 — actual result")

    # ---- Print reference for comparison ----
    print_frame(REF[1], "Generation 1 — Python reference")

    # ---- Final verdict ----
    total = fw_errors + seed_errors + mismatches
    dut._log.info(
        f"[result] fw_errors={fw_errors}  seed_errors={seed_errors}  "
        f"pixel_mismatches={mismatches}  "
        f"{'PASS' if total == 0 else 'FAIL'}"
    )

    assert fw_errors   == 0, f"Firmware load failed: {fw_errors} byte mismatches"
    assert seed_errors == 0, f"Seed write failed: {seed_errors} cell mismatches"
    assert mismatches  == 0, f"GoL result wrong: {mismatches} pixel mismatches"

    dut._log.info("[result] ✓ FULL PIPELINE PASSED")
    dut._log.info("[result] Output: gol_gen1.pbm")

# ============================================================
# SRAM DUMP TEST — boot only, then dump all 9 tile SRAMs
# ============================================================

@cocotb.test()
async def test_sram_dump_after_boot(dut):
    """
    1. Boot via flash (wait for cpu_rst_n)
    2. Assert host reset to keep tiles frozen
    3. Write blinker seed into current_grid via host SPI
    4. Hex-dump each tile's SRAM — shows host-written data at current_grid
    5. Release reset
    """
    # ---- Step 1: boot ----
    await do_boot(dut)
    await Timer(1, units="us")

    # ---- Step 2: assert host reset (tiles stay frozen, boot bus active) ----
    dut._log.info("[sram-dump] Asserting host reset via SPI...")
    await host_set_reset(dut, hold=True)
    await Timer(100, units="ns")

    # ---- Step 3: write seed from input.pbm into current_grid on all tiles ----
    tile_seed = frame_to_tile_grid(INPUT_FRAME, 0, 0)
    dut._log.info(f"[sram-dump] Writing blinker seed to SRAM_GRID_BASE=0x{SRAM_GRID_BASE:03x}...")
    for y in range(TILE_SIZE):
        for x in range(TILE_SIZE):
            addr = SRAM_GRID_BASE + y * TILE_SIZE + x
            await host_write_sram(dut, addr, tile_seed[y][x])

    dut._log.info("[sram-dump] Host seed write complete — dumping all 9 tiles")

    # ---- Step 4: dump ----
    DUMP_BYTES = 64   # firmware header bytes to show (set to SRAM_SIZE for full)

    border = "═" * 64
    print(f"\n╔{border}╗")
    print(f"║  SRAM DUMP — after boot + host seed (tiles still in reset){' '*5}║")
    print(f"╚{border}╝")

    for tr in range(MESH_R):
        for tc in range(MESH_C):
            tile = get_tile(dut, tr, tc)
            print(f"\n  ▶ tile({tr},{tc})")

            await dump_sram_hex(tile, 0, DUMP_BYTES,
                          f"firmware header (first {DUMP_BYTES}B)")

            grid_len = TILE_SIZE * TILE_SIZE
            await dump_sram_hex(tile, SRAM_GRID_BASE, grid_len,
                          f"current_grid @ 0x{SRAM_GRID_BASE:03x}  ← host-written")

            await dump_grid_ascii(tile, f"tile({tr},{tc}) current_grid visual")

            # Spot-check: current_grid should match tile_seed
            mismatches = []
            for y in range(TILE_SIZE):
                for x in range(TILE_SIZE):
                    addr = SRAM_GRID_BASE + y * TILE_SIZE + x
                    got  = (await sram_read_byte(tile, addr)) & 1
                    exp  = tile_seed[y][x]
                    if got != exp:
                        mismatches.append((y, x, exp, got))
            if mismatches:
                for (y, x, exp, got) in mismatches[:5]:
                    print(f"  ✗ tile({tr},{tc}) grid({y},{x}) exp={exp} got={got}")
            else:
                print(f"  ✓ tile({tr},{tc}): current_grid matches host-written seed")

    # ---- Assembled 30×30 view ----
    assembled = await read_30x30(dut)
    print_frame(assembled, "ASSEMBLED 30×30 — all 9 tiles (host-written seed)")
    print_pbm(INPUT_FRAME, "input.pbm — reference (tile borders shown)")

    # ---- Step 5: release reset ----
    await host_set_reset(dut, hold=False)
    dut._log.info("[sram-dump] Reset released — test complete")
