import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, RisingEdge

SIZE   = 10
MESH_R = 5
MESH_C = 5

# ── Must match ACTIVE_ROWS / ACTIVE_COLS in main.c ────────────────────────
ACTIVE_R = 5
ACTIVE_C = 5

FIRMWARE_BIN_NAME = "firmware.bin"
RESET_HOLD_MS     = 2
SEED_SAMPLE_US    = 2000
ITER_TIMEOUT_US   = 5000

SRAM_GRID_BASE = 0x0500
print(f"[testbench] current_grid @ 0x{SRAM_GRID_BASE:04x}")
print(f"[testbench] active mesh = {ACTIVE_R}x{ACTIVE_C}  physical = {MESH_R}x{MESH_C}")

DEBUG_BASE           = 0x0700
DEBUG_LAST_RECV_N    = DEBUG_BASE + 0
DEBUG_LAST_RECV_S    = DEBUG_BASE + 4
DEBUG_LAST_RECV_W    = DEBUG_BASE + 8
DEBUG_LAST_RECV_E    = DEBUG_BASE + 12
DEBUG_NEIGHBOR_HIST  = DEBUG_BASE + 16
DEBUG_ITER_COUNT     = DEBUG_BASE + 28
DEBUG_GHOST_FLAGS    = DEBUG_BASE + 32
DEBUG_LIVE_COUNT     = DEBUG_BASE + 36
DEBUG_COL0_BM        = DEBUG_BASE + 40
DEBUG_MY_ID          = DEBUG_BASE + 44
DEBUG_SEND_BM        = DEBUG_BASE + 80
DEBUG_ROW_TRACE_BASE = DEBUG_BASE + 52
DEBUG_ROW8_AT_CALL   = DEBUG_BASE + 52
DEBUG_ROW9_AT_CALL   = DEBUG_BASE + 56
DEBUG_PRE_OR_S8      = DEBUG_BASE + 60
DEBUG_PRE_OR_S9      = DEBUG_BASE + 64
DEBUG_CELL9_RAW      = DEBUG_BASE + 68
DEBUG_BIT9_VAL       = DEBUG_BASE + 72


def load_firmware_binary():
    candidates = [
        os.path.abspath(FIRMWARE_BIN_NAME),
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
    raise FileNotFoundError("Could not find firmware.bin. Checked:\n" + "\n".join(candidates))


FIRMWARE = load_firmware_binary()


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
                dut.flash_miso.value = (b >> bit) & 0x1
            byte_counter += 1
            if int(dut.flash_cs_n.value) == 1:
                break


def get_tile(dut, r, c):
    return dut.rows[r].cols[c].tile_inst


def sram_read_byte(tile, cpu_addr):
    mem = tile.sram_inst.mem
    elem_bits = len(mem[0])
    if elem_bits <= 8:
        return int(mem[cpu_addr].value) & 0xFF
    bytes_per_word = elem_bits // 8
    word_index = cpu_addr // bytes_per_word
    byte_lane  = cpu_addr % bytes_per_word
    word = int(mem[word_index].value)
    return (word >> (8 * byte_lane)) & 0xFF


def sram_read_word(tile, cpu_addr):
    b0 = sram_read_byte(tile, cpu_addr)
    b1 = sram_read_byte(tile, cpu_addr + 1)
    b2 = sram_read_byte(tile, cpu_addr + 2)
    b3 = sram_read_byte(tile, cpu_addr + 3)
    return b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)


async def wait_for_iter(tile, target_iter, timeout_us=ITER_TIMEOUT_US):
    cycles = int(timeout_us * 100)
    for _ in range(cycles):
        await RisingEdge(tile.clk)
        cur = sram_read_word(tile, DEBUG_ITER_COUNT)
        if cur >= target_iter:
            return True
    return False


def read_grid_from_sram(tile):
    return [
        [sram_read_byte(tile, SRAM_GRID_BASE + y * SIZE + x) for x in range(SIZE)]
        for y in range(SIZE)
    ]


def dump_region(tile, base, count_bytes=64):
    print(f"\nDUMP @ 0x{base:04x} ({count_bytes} bytes):")
    for off in range(0, count_bytes, 16):
        chunk = [sram_read_byte(tile, base + off + i)
                 for i in range(min(16, count_bytes - off))]
        print("0x{:04x}: ".format(base + off) + " ".join(f"{b:02x}" for b in chunk))


def read_debug_info(tile, label=""):
    ghost_flags = sram_read_word(tile, DEBUG_GHOST_FLAGS)
    my_id       = sram_read_word(tile, DEBUG_MY_ID)
    iter_count  = sram_read_word(tile, DEBUG_ITER_COUNT)
    print(f"\n{label}")
    print(f"  my_id=0x{my_id:x}  iter={iter_count}")
    print(f"  Ghost flags=0x{ghost_flags:02x}")


# ── Checkerboard reference model ───────────────────────────────────────────
#
# Mirrors exactly what firmware does:
#   fill_val = tile hardware ID = (row<<2)|col
#   iter 0 seed: (r+c) even → fill_val, odd → 0
#   iter N:      parity flips each generation
#
# Active tiles: rows 0..ACTIVE_R-1, cols 0..ACTIVE_C-1
#   hw_id = (row<<2)|col   (0,0)→0  (0,1)→1  (1,0)→4  (1,1)→5  etc.
# Idle tiles (outside active region): SRAM stays all-zero (no writes)

# tests when 0,0 is (.)
# def hw_tile_id(r, c):
#     return (r << 2) | c

def hw_tile_id(r, c):
    return ((r << 3) | c) + 1   # was r<<2; must match {row[2:0], col[2:0]}

# Build a display character from a fill_val by reverse-looking up the tile
FILL_TO_CHAR = {}
_chars = "123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_idx = 0
for _r in range(8):
    for _c in range(8):
        _fv = (((_r << 3) | _c) + 1) & 0xFF
        if _fv not in FILL_TO_CHAR:
            FILL_TO_CHAR[_fv] = _chars[_idx % len(_chars)]
            _idx += 1

def fmt(v):
    if v == 0:
        return '.'
    return FILL_TO_CHAR.get(v, '?')

def expected_checkerboard(r, c, iteration):
    """Return 10x10 expected grid for tile (r,c) at given iteration."""
    # Idle tiles never write — expect zeros
    if r >= ACTIVE_R or c >= ACTIVE_C:
        return [[0] * SIZE for _ in range(SIZE)]

    fill_val = hw_tile_id(r, c)
    # iter 0 seed parity: (row+col) % 2 == 0 → fill_val, else 0
    # each subsequent iter flips parity
    # so at iter N: (row+col+N) % 2 == 0 → fill_val, else 0
    grid = []
    for row in range(SIZE):
        grid_row = []
        for col in range(SIZE):
            if (row + col + iteration) % 2 == 0:
                grid_row.append(fill_val)
            else:
                grid_row.append(0)
        grid.append(grid_row)
    return grid


def print_iter_comparison(dut, r, c, iteration):
    """Compare expected checkerboard vs actual SRAM. Returns mismatch count."""
    tile = get_tile(dut, r, c)
    exp  = expected_checkerboard(r, c, iteration)
    act  = read_grid_from_sram(tile)

    fill_val = hw_tile_id(r, c)
    is_idle  = (r >= ACTIVE_R or c >= ACTIVE_C)
    status   = "IDLE" if is_idle else f"ACTIVE fill=0x{fill_val:02x}"

    print(f"\n===================================================")
    print(f"TILE ({r},{c})  hw_id=0x{fill_val:x}  [{status}]  ITERATION {iteration}")
    print("EXPECTED (checkerboard)   ACTUAL (SRAM)")
    print("---------------------------------------------------")

    mismatches = 0
    for y in range(SIZE):

        exp_row = "".join(fmt(exp[y][x]) for x in range(SIZE))
        act_row = "".join(fmt(act[y][x]) for x in range(SIZE))
        marker  = "" if exp_row == act_row else "  <- MISMATCH"
        if marker:
            mismatches += sum(1 for x in range(SIZE) if exp[y][x] != act[y][x])
        print(f"{exp_row}    {act_row}{marker}")
    print("===================================================")
    return mismatches


async def boot_mesh(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.flash_miso.value = 0
    if hasattr(dut, "inject_00_nw"):
        dut.inject_00_nw.value = 0
    await Timer(RESET_HOLD_MS, unit="ms")
    dut.rst.value = 0
    cocotb.start_soon(spi_flash_responder(dut))
    while int(dut.cpu_rst_n.value) == 0:
        await Timer(10, unit="us")
    dut._log.info("[boot] cpu_rst_n asserted — CPU running.")


# ── Test 1: magic check — confirms testbench can read SRAM at all ─────────
@cocotb.test()
async def test_magic_check(dut):
    """Firmware writes 0xDEADBEEF/0xCAFEBABE; confirms address mapping."""
    await boot_mesh(dut)
    await Timer(SEED_SAMPLE_US, unit="us")

    print("\n\n" + "="*60)
    print("MAGIC CHECK — verifying testbench address mapping")
    print("="*60)

    all_pass = True
    for r in range(MESH_R):
        for c in range(MESH_C):
            tile   = get_tile(dut, r, c)
            magic1 = sram_read_word(tile, 0x0730)
            magic2 = sram_read_word(tile, 0x0734)
            ok1    = magic1 == 0xDEADBEEF
            ok2    = magic2 == 0xCAFEBABE
            label1 = "[OK]  " if ok1 else "[FAIL]"
            label2 = "[OK]  " if ok2 else "[FAIL]"
            is_idle = (r >= ACTIVE_R or c >= ACTIVE_C)
            role   = "IDLE" if is_idle else "ACTIVE"
            print(f"\nTILE ({r},{c}) [{role}]:")
            print(f"  {label1} 0x0730 = 0x{magic1:08x}  (expect 0xDEADBEEF)")
            print(f"  {label2} 0x0734 = 0x{magic2:08x}  (expect 0xCAFEBABE)")
            if not (ok1 and ok2):
                all_pass = False

    if all_pass:
        dut._log.info("MAGIC CHECK PASSED on all tiles")
    else:
        dut._log.error("MAGIC CHECK FAILED — fix address mapping before debugging firmware")
    assert all_pass, "Magic check failed"


# ── Test 2: seed check ────────────────────────────────────────────────────
@cocotb.test()
async def test_iter0_seed_only(dut):
    """Each tile should have checkerboard[iter=0] in SRAM at seed time."""
    await boot_mesh(dut)
    await Timer(SEED_SAMPLE_US, unit="us")

    print("\n\n******** ITERATION 0 (seed checkerboard) ********")
    print(f"Active tiles: rows 0..{ACTIVE_R-1}, cols 0..{ACTIVE_C-1}")
    print(f"Idle  tiles: SRAM should be all-zero\n")

    total = 0
    for r in range(MESH_R):
        for c in range(MESH_C):
            total += print_iter_comparison(dut, r, c, 0)

    if total == 0:
        dut._log.info("ALL TILES: seed matches")
    else:
        dut._log.error(f"{total} mismatches in seed")
        for r in range(MESH_R):
            for c in range(MESH_C):
                dump_region(get_tile(dut, r, c), SRAM_GRID_BASE, 100)
    assert total == 0, f"Seed mismatch: {total} cells wrong."


# ── Test 3: iter 1 and iter 2 ─────────────────────────────────────────────
@cocotb.test()
async def test_checkerboard_iter1_iter2(dut):
    """
    Each generation the checkerboard parity flips.
    No ghost exchange — every tile runs independently.
    If a tile fails here, that tile's firmware loop is broken or
    it's stuck in idle_tile_forever() when it shouldn't be.
    """
    await boot_mesh(dut)
    await Timer(SEED_SAMPLE_US, unit="us")

    tile00 = get_tile(dut, 0, 0)

    # ── iter 0 ────────────────────────────────────────────────────────────
    print("\n\n******** ITERATION 0 (seed) ********")
    iter0_mismatches = 0
    for r in range(MESH_R):
        for c in range(MESH_C):
            iter0_mismatches += print_iter_comparison(dut, r, c, 0)
    if iter0_mismatches == 0:
        dut._log.info("Iter 0: ALL tiles match")
    else:
        dut._log.error(f"Iter 0: {iter0_mismatches} mismatches")

    # ── iter 1 ────────────────────────────────────────────────────────────
    dut._log.info("Waiting for iter_count >= 1 on tile(0,0)...")
    seen = await wait_for_iter(tile00, 1)
    if not seen:
        dut._log.warning("iter_count did not advance to 1 — tile(0,0) may be hung")
    await Timer(10, unit="us")

    print("\n\n******** ITERATION 1 ********")
    iter1_mismatches = 0
    for r in range(MESH_R):
        for c in range(MESH_C):
            m = print_iter_comparison(dut, r, c, 1)
            iter1_mismatches += m
            if m > 0:
                tile = get_tile(dut, r, c)
                my_id = sram_read_word(tile, DEBUG_MY_ID)
                itr   = sram_read_word(tile, DEBUG_ITER_COUNT)
                print(f"  -> tile({r},{c}) hw_id=0x{my_id:x} iter_count={itr}")
                dump_region(tile, SRAM_GRID_BASE, 100)

    if iter1_mismatches == 0:
        dut._log.info("Iter 1: ALL tiles match")
    else:
        dut._log.error(f"Iter 1: {iter1_mismatches} mismatches")

    # ── iter 2 ────────────────────────────────────────────────────────────
    dut._log.info("Waiting for iter_count >= 2 on tile(0,0)...")
    seen = await wait_for_iter(tile00, 2)
    if not seen:
        dut._log.warning("iter_count did not advance to 2")
    await Timer(10, unit="us")

    print("\n\n******** ITERATION 2 ********")
    iter2_mismatches = 0
    for r in range(MESH_R):
        for c in range(MESH_C):
            m = print_iter_comparison(dut, r, c, 2)
            iter2_mismatches += m
            if m > 0:
                tile = get_tile(dut, r, c)
                my_id = sram_read_word(tile, DEBUG_MY_ID)
                itr   = sram_read_word(tile, DEBUG_ITER_COUNT)
                print(f"  -> tile({r},{c}) hw_id=0x{my_id:x} iter_count={itr}")

    if iter2_mismatches == 0:
        dut._log.info("Iter 2: ALL tiles match")
    else:
        dut._log.error(f"Iter 2: {iter2_mismatches} mismatches")

    total = iter0_mismatches + iter1_mismatches + iter2_mismatches
    assert total == 0, (
        f"Checkerboard test failed: "
        f"iter0={iter0_mismatches}, iter1={iter1_mismatches}, iter2={iter2_mismatches}"
    )