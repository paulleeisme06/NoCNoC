"""
test_mesh.py — cocotb testbench for mesh_nxn
=============================================
ONLY change MESH_R and MESH_C below.
They must match the values in mesh_config.vh (and mesh_config.h for firmware).
Everything else scales automatically.

Expected output per iteration:
  - Full side-by-side EXPECTED vs ACTUAL grid for every tile
  - Live-cell count summary
  - Debug registers for every tile
  - Col-bitmap diagnostic (when MESH_C >= 2)
  - On failure: neighbor histograms + raw SRAM dumps for failing tiles
"""
import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, RisingEdge

#Change for mesh size
MESH_R = 5   
MESH_C = 5  
SIZE   = 10  

GLOBAL_ROWS = MESH_R * SIZE
GLOBAL_COLS = MESH_C * SIZE

FIRMWARE_BIN_NAME = "firmware.bin"
RESET_HOLD_MS     = 2     # rst assertion time (ms)

SEED_WAIT_US    = 3000   # µs after cpu_rst_n to wait for seed writes
ITER_TIMEOUT_US = 8000   # µs per iteration timeout
POLL_NS         = 500    # timer poll granularity (ns)

# =============================================================================
# SRAM / debug memory map  (byte addresses inside each tile's 2 KB SRAM)
# =============================================================================
SRAM_GRID_BASE   = 0x0500
DEBUG_BASE       = 0x0700
DEBUG_LAST_RECV_N   = DEBUG_BASE + 0
DEBUG_LAST_RECV_S   = DEBUG_BASE + 4
DEBUG_LAST_RECV_W   = DEBUG_BASE + 8
DEBUG_LAST_RECV_E   = DEBUG_BASE + 12
DEBUG_NEIGHBOR_HIST = DEBUG_BASE + 16
DEBUG_ITER_COUNT    = DEBUG_BASE + 28
DEBUG_GHOST_FLAGS   = DEBUG_BASE + 32
DEBUG_LIVE_COUNT    = DEBUG_BASE + 36
DEBUG_COL0_BM       = DEBUG_BASE + 40
DEBUG_MY_ID         = DEBUG_BASE + 44
DEBUG_SEND_BM       = DEBUG_BASE + 48
DEBUG_ROW8_AT_CALL  = DEBUG_BASE + 52
DEBUG_ROW9_AT_CALL  = DEBUG_BASE + 56
DEBUG_PRE_OR_S8     = DEBUG_BASE + 60
DEBUG_PRE_OR_S9     = DEBUG_BASE + 64
DEBUG_CELL9_RAW     = DEBUG_BASE + 68
DEBUG_BIT9_VAL      = DEBUG_BASE + 72


# =============================================================================
# Helpers
# =============================================================================

def p(*args, **kwargs):
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def banner(title, width=72, char="="):
    p(char * width)
    p(f"  {title}")
    p(char * width)


# =============================================================================
# Firmware
# =============================================================================

def load_firmware_binary():
    candidates = [
        os.path.abspath(FIRMWARE_BIN_NAME),  # current working directory
        os.path.abspath(os.path.join(os.getcwd(), FIRMWARE_BIN_NAME)),
        os.path.abspath(os.path.join(os.path.dirname(__file__), FIRMWARE_BIN_NAME)),
    ]

    for bin_file in candidates:
        print(f"[firmware] checking {bin_file}")
        if os.path.exists(bin_file):
            with open(bin_file, "rb") as f:
                data = list(f.read())

            print(f"[firmware] loaded {len(data)} bytes from {bin_file}")
            print("[firmware] first 16 bytes =", " ".join(f"{b:02x}" for b in data[:16]))

            if len(data) < 2048:
                data += [0] * (2048 - len(data))

            return data[:2048]

    raise FileNotFoundError(
        "Could not find firmware.bin. Checked:\n" + "\n".join(candidates)
    )


FIRMWARE = load_firmware_binary()


# =============================================================================
# SPI flash responder
# =============================================================================

async def spi_flash_responder(dut):
    from cocotb.triggers import FallingEdge
    while True:
        await FallingEdge(dut.flash_cs_n)
        for _ in range(32):
            await RisingEdge(dut.flash_clk)
        byte_counter = 0
        while True:
            for bit in range(7, -1, -1):
                await FallingEdge(dut.flash_clk)
                b = FIRMWARE[byte_counter] if byte_counter < len(FIRMWARE) else 0x00
                dut.flash_miso.value = (b >> bit) & 1
            byte_counter += 1
            if int(dut.flash_cs_n.value) == 1:
                break


# =============================================================================
# Tile / SRAM accessors
# =============================================================================

def get_tile(dut, r, c):
    """Return the mesh_tile instance at (r, c)."""
    return dut.rows[r].cols[c].tile_inst


def sram_read_byte(tile, cpu_addr):
    mem = tile.sram_inst.mem
    elem_bits = len(mem[0])
    if elem_bits <= 8:
        return int(mem[cpu_addr].value) & 0xFF
    bpw   = elem_bits // 8
    word  = int(mem[cpu_addr // bpw].value)
    shift = (cpu_addr % bpw) * 8
    return (word >> shift) & 0xFF


def sram_read_word(tile, cpu_addr):
    return (sram_read_byte(tile, cpu_addr)
            | (sram_read_byte(tile, cpu_addr + 1) << 8)
            | (sram_read_byte(tile, cpu_addr + 2) << 16)
            | (sram_read_byte(tile, cpu_addr + 3) << 24))


def read_grid_from_sram(tile):
    return [
        [sram_read_byte(tile, SRAM_GRID_BASE + y * SIZE + x) for x in range(SIZE)]
        for y in range(SIZE)
    ]


# =============================================================================
# Wait helpers
# =============================================================================

async def wait_boot(dut):
    while int(dut.cpu_rst_n.value) == 0:
        await Timer(POLL_NS, unit="ns")


async def wait_for_iter(tile, target, timeout_us=ITER_TIMEOUT_US):
    limit = int(timeout_us * 1000 / POLL_NS)
    for _ in range(limit):
        await Timer(POLL_NS, unit="ns")
        if sram_read_word(tile, DEBUG_ITER_COUNT) >= target:
            return True
    return False


async def wait_for_seed(dut, timeout_us=SEED_WAIT_US):
    """
    Poll until grid[9][9] is non-zero on every tile.
    grid[9][9] is the last cell written by the seed sequence in main.c.
    """
    last_seed_addr = SRAM_GRID_BASE + 9 * SIZE + 9
    limit = int(timeout_us * 1000 / POLL_NS)
    for _ in range(limit):
        await Timer(POLL_NS, unit="ns")
        all_done = all(
            sram_read_byte(get_tile(dut, r, c), last_seed_addr) != 0
            for r in range(MESH_R) for c in range(MESH_C)
        )
        if all_done:
            return True
    return False


# =============================================================================
# GoL reference model
# =============================================================================

def _tile_seed():
    g = [[0] * SIZE for _ in range(SIZE)]
    g[4][5] = g[5][5] = g[6][5] = 1    # vertical blinker
    g[8][0] = g[9][0] = 1              # bottom-left corner pair
    g[8][9] = g[9][9] = 1              # bottom-right corner pair
    return g


def build_global_seed():
    seed = _tile_seed()
    g = [[0] * GLOBAL_COLS for _ in range(GLOBAL_ROWS)]
    for tr in range(MESH_R):
        for tc in range(MESH_C):
            for y in range(SIZE):
                for x in range(SIZE):
                    g[tr * SIZE + y][tc * SIZE + x] = seed[y][x]
    return g


def gol_step(grid):
    rows, cols = len(grid), len(grid[0])
    ng = [[0] * cols for _ in range(rows)]
    for y in range(rows):
        for x in range(cols):
            n = sum(
                grid[y + dy][x + dx]
                for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                if (dy or dx) and 0 <= y + dy < rows and 0 <= x + dx < cols
            )
            ng[y][x] = 1 if (grid[y][x] and n in (2, 3)) or (not grid[y][x] and n == 3) else 0
    return ng


def get_tile_expected(iteration, tr, tc):
    g = GOL_GLOBAL[iteration]
    return [g[tr * SIZE + y][tc * SIZE: tc * SIZE + SIZE] for y in range(SIZE)]


# Pre-compute 3 reference generations
GOL_GLOBAL = [build_global_seed()]
GOL_GLOBAL.append(gol_step(GOL_GLOBAL[0]))
GOL_GLOBAL.append(gol_step(GOL_GLOBAL[1]))


# =============================================================================
# Grid display
# =============================================================================

def print_all_tiles_for_iteration(dut, iteration):
    banner(f"ITERATION {iteration}  —  {MESH_R}×{MESH_C} mesh  (# = live, . = dead)")

    total_wrong = 0
    summary = []

    for r in range(MESH_R):
        for c in range(MESH_C):
            tile = get_tile(dut, r, c)
            exp  = get_tile_expected(iteration, r, c)
            act  = read_grid_from_sram(tile)

            wrong    = sum((exp[y][x] != 0) != (act[y][x] != 0)
                           for y in range(SIZE) for x in range(SIZE))
            live_exp = sum(exp[y][x] for y in range(SIZE) for x in range(SIZE))
            live_act = sum(1 for y in range(SIZE) for x in range(SIZE) if act[y][x])
            total_wrong += wrong

            status = "[OK]" if wrong == 0 else f"[FAIL: {wrong} cells wrong]"
            summary.append(f"  ({r},{c})  live_exp={live_exp:3d}  live_act={live_act:3d}  {status}")

            p(f"\nTILE ({r},{c})  iter={iteration}  {status}")
            p(f"  EXPECTED (Python GoL)      ACTUAL (SRAM)")
            p(f"  {'-'*SIZE}      {'-'*SIZE}")
            for y in range(SIZE):
                er = "".join("#" if exp[y][x] else "." for x in range(SIZE))
                ar = "".join("#" if act[y][x] else "." for x in range(SIZE))
                mk = "  <-- MISMATCH" if er != ar else ""
                p(f"  {er}      {ar}{mk}")

    p()
    p(f"  Live-cell summary  iter={iteration}:")
    for ln in summary:
        p(ln)
    p()
    return total_wrong


def dump_region(tile, base, count_bytes=64):
    p(f"\n  DUMP @ 0x{base:04x} ({count_bytes} bytes):")
    for off in range(0, count_bytes, 16):
        chunk = [sram_read_byte(tile, base + off + i)
                 for i in range(min(16, count_bytes - off))]
        p("  0x{:04x}: ".format(base + off) + " ".join(f"{b:02x}" for b in chunk))


# =============================================================================
# Debug helpers
# =============================================================================

def read_debug_info(tile, label=""):
    recv_n      = sram_read_word(tile, DEBUG_LAST_RECV_N)
    recv_s      = sram_read_word(tile, DEBUG_LAST_RECV_S)
    recv_w      = sram_read_word(tile, DEBUG_LAST_RECV_W)
    recv_e      = sram_read_word(tile, DEBUG_LAST_RECV_E)
    ghost_flags = sram_read_word(tile, DEBUG_GHOST_FLAGS)
    my_id       = sram_read_word(tile, DEBUG_MY_ID)
    send_bm     = sram_read_word(tile, DEBUG_SEND_BM)
    live_count  = sram_read_word(tile, DEBUG_LIVE_COUNT)

    sn = (ghost_flags >> 0) & 1
    ss = (ghost_flags >> 1) & 1
    se = (ghost_flags >> 2) & 1
    sw = (ghost_flags >> 3) & 1

    if label:
        p(f"\n  [{label}]")
    p(f"  my_id=0x{my_id:x}  live_count={live_count}  send_bm=0x{send_bm:03x} ({send_bm:010b})")
    p(f"  Ghost flags=0x{ghost_flags:02x}: sent N={sn} S={ss} E={se} W={sw}")
    if recv_n or sn: p(f"  Recv N: 0x{recv_n:03x}  {recv_n:010b}")
    if recv_s or ss: p(f"  Recv S: 0x{recv_s:03x}  {recv_s:010b}")
    if recv_w or sw: p(f"  Recv W: 0x{recv_w:03x}  {recv_w:010b}")
    if recv_e or se: p(f"  Recv E: 0x{recv_e:03x}  {recv_e:010b}")
    return recv_n, recv_s, recv_w, recv_e, ghost_flags


def print_all_debug_info(dut, label_prefix=""):
    for r in range(MESH_R):
        for c in range(MESH_C):
            read_debug_info(get_tile(dut, r, c), f"{label_prefix}TILE ({r},{c})")


def read_neighbor_histogram(tile, label=""):
    hist  = [sram_read_byte(tile, DEBUG_NEIGHBOR_HIST + i) for i in range(9)]
    total = sum(hist)
    p(f"\n  {label} Neighbor histogram (total={total}):")
    for n, cnt in enumerate(hist):
        p(f"    n={n}: {cnt:4d}  {'#' * min(cnt // 2, 30)}")
    return hist


def diagnose_col_bitmap(dut, iteration):
    if MESH_C < 2:
        p("  [col_bitmap diag skipped — only 1 column]")
        return

    tile01 = get_tile(dut, 0, 1)
    tile00 = get_tile(dut, 0, 0)

    col0    = [sram_read_byte(tile01, SRAM_GRID_BASE + row * SIZE + 0) for row in range(SIZE)]
    exp_bm  = sum((1 << row) for row, v in enumerate(col0) if v & 1)
    fw_bm   = sram_read_word(tile01, DEBUG_SEND_BM)
    recv_bm = sram_read_word(tile00, DEBUG_LAST_RECV_E)

    p(f"\n  col_bitmap diagnostic — iter {iteration}")
    p(f"  tile(0,1) col-0 cells:   {col0}")
    p(f"  Expected bitmap (py):    0x{exp_bm:03x}  {exp_bm:010b}")
    p(f"  Firmware DEBUG_SEND_BM:  0x{fw_bm:03x}  {fw_bm:010b}  {'[OK]' if fw_bm == exp_bm else '[FAIL]'}")
    p(f"  tile(0,0) DEBUG_RECV_E:  0x{recv_bm:03x}  {recv_bm:010b}  {'[OK]' if recv_bm == exp_bm else '[FAIL]'}")
    p()


# =============================================================================
# Boot helper
# =============================================================================

async def boot_mesh(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.flash_miso.value = 0
    if hasattr(dut, "inject_00_nw"):
        dut.inject_00_nw.value = 0
    await Timer(RESET_HOLD_MS, unit="ms")
    dut.rst.value = 0
    cocotb.start_soon(spi_flash_responder(dut))
    p("[boot] waiting for cpu_rst_n...")
    await wait_boot(dut)
    dut._log.info(f"[boot] cpu_rst_n asserted — {MESH_R}×{MESH_C} mesh running")


# =============================================================================
# TEST: address mapping sanity
# =============================================================================

@cocotb.test()
async def test_magic_check(dut):
    """
    Firmware writes 0xDEADBEEF → 0x0730 and 0xCAFEBABE → 0x0734 as the
    very first thing in main().  Confirms sram_read_word() addressing is correct.
    """
    await boot_mesh(dut)
    await Timer(SEED_WAIT_US, unit="us")

    banner(f"MAGIC CHECK — {MESH_R}×{MESH_C} mesh")
    all_pass = True

    for r in range(MESH_R):
        for c in range(MESH_C):
            tile = get_tile(dut, r, c)
            m1   = sram_read_word(tile, 0x0730)
            m2   = sram_read_word(tile, 0x0734)
            ok1, ok2 = m1 == 0xDEADBEEF, m2 == 0xCAFEBABE
            p(f"\n  TILE ({r},{c}):")
            p(f"    {'[OK]  ' if ok1 else '[FAIL]'} 0x0730 = 0x{m1:08x}  (expect 0xDEADBEEF)")
            p(f"    {'[OK]  ' if ok2 else '[FAIL]'} 0x0734 = 0x{m2:08x}  (expect 0xCAFEBABE)")
            if not (ok1 and ok2):
                all_pass = False
                mem = tile.sram_inst.mem
                bpw = len(mem[0]) // 8
                base = 0x0730 // bpw
                p(f"    Raw SRAM words near 0x0730:")
                for wi in range(max(0, base - 2), base + 6):
                    p(f"      mem[{wi}] = 0x{int(mem[wi].value):08x}")

    p()
    if all_pass:
        dut._log.info("MAGIC CHECK PASSED on all tiles")
    else:
        dut._log.error("MAGIC CHECK FAILED — address mapping broken")
    assert all_pass, "Magic check failed"


# =============================================================================
# TEST: GoL iterations 0, 1, 2
# =============================================================================

@cocotb.test()
async def test_gol_iterations(dut):
    """
    Main correctness test — checks 3 GoL iterations across all tiles.
    Iter 0: waits for seed, then prints grids.
    Iter 1 & 2: waits for DEBUG_ITER_COUNT to advance, then prints grids.
    Failing tiles get neighbor histograms and raw SRAM dumps.
    """
    await boot_mesh(dut)

    # ── Iteration 0 — seed ──────────────────────────────────────────────────
    p(f"\n[iter 0] waiting up to {SEED_WAIT_US} µs for firmware to seed all tiles...")
    ok = await wait_for_seed(dut)
    p("[iter 0] seed confirmed on all tiles" if ok else
      "[iter 0] WARNING: seed timeout — grids may still be zero")
    if not ok:
        await Timer(500, unit="us")

    f0 = print_all_tiles_for_iteration(dut, 0)
    p(f"[iter 0] {'PASS' if f0 == 0 else f'FAIL ({f0} mismatches)'}")

    banner("DEBUG REGISTERS — iter 0", char="-")
    print_all_debug_info(dut, "iter0 ")

    if MESH_C >= 2:
        banner("COL-BITMAP DIAGNOSTIC — iter 0", char="-")
        diagnose_col_bitmap(dut, 0)

    if f0 > 0:
        for r in range(MESH_R):
            for c in range(MESH_C):
                tile = get_tile(dut, r, c)
                exp  = get_tile_expected(0, r, c)
                act  = read_grid_from_sram(tile)
                if any((exp[y][x] != 0) != (act[y][x] != 0)
                       for y in range(SIZE) for x in range(SIZE)):
                    p(f"\n  TILE ({r},{c}) raw SRAM dump:")
                    dump_region(tile, SRAM_GRID_BASE, 100)

    # ── Iteration 1 ─────────────────────────────────────────────────────────
    tile00 = get_tile(dut, 0, 0)
    p(f"\n[iter 1] waiting up to {ITER_TIMEOUT_US} µs for iter_count >= 1...")
    ok = await wait_for_iter(tile00, 1)
    p("[iter 1] confirmed" if ok else "[iter 1] WARNING: timeout")
    await Timer(200, unit="us")

    f1 = print_all_tiles_for_iteration(dut, 1)
    p(f"[iter 1] {'PASS' if f1 == 0 else f'FAIL ({f1} mismatches)'}")

    banner("DEBUG REGISTERS — iter 1", char="-")
    print_all_debug_info(dut, "iter1 ")

    if MESH_C >= 2:
        banner("COL-BITMAP DIAGNOSTIC — iter 1", char="-")
        diagnose_col_bitmap(dut, 1)

    if f1 > 0:
        for r in range(MESH_R):
            for c in range(MESH_C):
                tile = get_tile(dut, r, c)
                exp  = get_tile_expected(1, r, c)
                act  = read_grid_from_sram(tile)
                if any((exp[y][x] != 0) != (act[y][x] != 0)
                       for y in range(SIZE) for x in range(SIZE)):
                    read_neighbor_histogram(tile, f"TILE ({r},{c})")
                    dump_region(tile, SRAM_GRID_BASE, 100)
                    dump_region(tile, 0x0600, 40)
                    dump_region(tile, DEBUG_BASE, 128)

    # ── Iteration 2 ─────────────────────────────────────────────────────────
    p(f"\n[iter 2] waiting up to {ITER_TIMEOUT_US} µs for iter_count >= 2...")
    ok = await wait_for_iter(tile00, 2)
    p("[iter 2] confirmed" if ok else "[iter 2] WARNING: timeout")
    await Timer(200, unit="us")

    f2 = print_all_tiles_for_iteration(dut, 2)
    p(f"[iter 2] {'PASS' if f2 == 0 else f'FAIL ({f2} mismatches)'}")

    banner("DEBUG REGISTERS — iter 2", char="-")
    print_all_debug_info(dut, "iter2 ")

    if MESH_C >= 2:
        banner("COL-BITMAP DIAGNOSTIC — iter 2", char="-")
        diagnose_col_bitmap(dut, 2)

    if f2 > 0:
        for r in range(MESH_R):
            for c in range(MESH_C):
                tile = get_tile(dut, r, c)
                exp  = get_tile_expected(2, r, c)
                act  = read_grid_from_sram(tile)
                if any((exp[y][x] != 0) != (act[y][x] != 0)
                       for y in range(SIZE) for x in range(SIZE)):
                    read_neighbor_histogram(tile, f"TILE ({r},{c})")
                    dump_region(tile, SRAM_GRID_BASE, 100)
                    dump_region(tile, 0x0600, 40)
                    dump_region(tile, DEBUG_BASE, 128)

    # ── Final verdict ────────────────────────────────────────────────────────
    total = f0 + f1 + f2
    banner(
        f"FINAL  iter0={'PASS' if f0==0 else 'FAIL'}  "
        f"iter1={'PASS' if f1==0 else 'FAIL'}  "
        f"iter2={'PASS' if f2==0 else 'FAIL'}"
    )
    assert total == 0, f"GoL FAILED: iter0={f0} iter1={f1} iter2={f2} mismatches"