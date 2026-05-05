import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, RisingEdge

SIZE   = 10
MESH_R = 3
MESH_C = 3

FIRMWARE_BIN_NAME = "firmware.bin"
RESET_HOLD_MS     = 2
SEED_SAMPLE_US    = 2000
ITER_TIMEOUT_US   = 5000

SRAM_GRID_BASE = 0x0500
print(f"[testbench] current_grid @ 0x{SRAM_GRID_BASE:04x}")

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
DEBUG_ROW_TRACE_BASE = DEBUG_BASE + 52   # 0x0734
DEBUG_ROW8_AT_CALL   = DEBUG_BASE + 52   # 0x0734
DEBUG_ROW9_AT_CALL   = DEBUG_BASE + 56   # 0x0738
# ── new debug addresses ────────────────────────────────────────────────────
DEBUG_PRE_OR_S8      = DEBUG_BASE + 60   # 0x073C  shifted8 right before OR
DEBUG_PRE_OR_S9      = DEBUG_BASE + 64   # 0x0740  shifted9 right before OR
DEBUG_CELL9_RAW      = DEBUG_BASE + 68   # 0x0744  raw cell9 byte value
DEBUG_BIT9_VAL       = DEBUG_BASE + 72   # 0x0748  bit9 after & 1





def load_firmware_binary():
    bin_file = os.path.join(os.path.dirname(__file__), FIRMWARE_BIN_NAME)
    if os.path.exists(bin_file):
        with open(bin_file, "rb") as f:
            return list(f.read())
    return [0] * 2048


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


def read_col_bitmap_trace(tile, label=""):
    send_bm   = sram_read_word(tile, DEBUG_SEND_BM)
    row8_val  = sram_read_word(tile, DEBUG_ROW8_AT_CALL)
    row9_val  = sram_read_word(tile, DEBUG_ROW9_AT_CALL)
    pre_or_s8 = sram_read_word(tile, DEBUG_PRE_OR_S8)
    pre_or_s9 = sram_read_word(tile, DEBUG_PRE_OR_S9)
    cell9_raw = sram_read_word(tile, DEBUG_CELL9_RAW)
    bit9_val  = sram_read_word(tile, DEBUG_BIT9_VAL)

    print(f"\n{label} col_bitmap() trace:")
    print(f"  {'slot':<22} {'hex':>8}  {'binary':>12}  notes")
    print(f"  {'-'*65}")

    def row(name, val, note=""):
        print(f"  {name:<22} 0x{val:04x}  {val:012b}  {note}")

    row("cell9 (raw byte)",    cell9_raw, "expect 1")
    row("bit9  (& 1u)",        bit9_val,  "expect 1")
    row("shifted8 (after wb)", row8_val,  "expect 0x100")
    row("shifted9 (after wb)", row9_val,  "expect 0x200")
    row("pre_or shifted8",     pre_or_s8, "expect 0x100  <-- smoking gun")
    row("pre_or shifted9",     pre_or_s9, "expect 0x200  <-- smoking gun")
    row("DEBUG_SEND_BM",       send_bm,   "expect 0x300")

    print()

    # Diagnosis — walk the chain to find exactly where the value is lost
    if cell9_raw == 0:
        print("  [FAIL] cell9 was 0 at read time — grid not seeded or wrong SRAM address")
    elif bit9_val == 0:
        print("  [FAIL] bit9 lost during '& 1' — compiler/ABI issue upstream of shift")
    elif row9_val == 0:
        print("  [FAIL] shifted9 lost before write-back store — compiler dropped register")
    elif pre_or_s9 == 0:
        print("  [FAIL] shifted9 clobbered AFTER write-back, BEFORE OR — register reuse confirmed")
    elif send_bm != (pre_or_s8 | pre_or_s9):
        print("  [FAIL] OR result wrong even though both inputs were correct — check bm register")
    else:
        print("  [OK]  all intermediate values correct")

    if send_bm == 0x300:
        print("  [OK]  DEBUG_SEND_BM correct (0x300)")
    else:
        print(f"  [FAIL] DEBUG_SEND_BM = 0x{send_bm:03x}, expected 0x300")

    return send_bm, row8_val, row9_val, pre_or_s8, pre_or_s9, cell9_raw, bit9_val


def read_debug_info(tile, label=""):
    recv_n      = sram_read_word(tile, DEBUG_LAST_RECV_N)
    recv_s      = sram_read_word(tile, DEBUG_LAST_RECV_S)
    recv_w      = sram_read_word(tile, DEBUG_LAST_RECV_W)
    recv_e      = sram_read_word(tile, DEBUG_LAST_RECV_E)
    ghost_flags = sram_read_word(tile, DEBUG_GHOST_FLAGS)
    live_count  = sram_read_word(tile, DEBUG_LIVE_COUNT)
    col0_bm     = sram_read_word(tile, DEBUG_COL0_BM)
    my_id       = sram_read_word(tile, DEBUG_MY_ID)
    send_bm     = sram_read_word(tile, DEBUG_SEND_BM)

    sent_s = (ghost_flags >> 0) & 1
    sent_n = (ghost_flags >> 1) & 1
    sent_e = (ghost_flags >> 2) & 1
    sent_w = (ghost_flags >> 3) & 1

    print(f"\n{label}")
    print(f"  my_id=0x{my_id:x}  live_count={live_count}  col0_bm=0x{col0_bm:03x} ({col0_bm:010b})")
    print(f"  send_bm=0x{send_bm:03x} ({send_bm:010b})")
    print(f"  Ghost flags=0x{ghost_flags:02x}: sent N={sent_n} S={sent_s} E={sent_e} W={sent_w}")
    if recv_n or sent_n: print(f"  Recv N: 0x{recv_n:03x}  {recv_n:010b}")
    if recv_s or sent_s: print(f"  Recv S: 0x{recv_s:03x}  {recv_s:010b}")
    if recv_w or sent_w: print(f"  Recv W: 0x{recv_w:03x}  {recv_w:010b}")
    if recv_e or sent_e: print(f"  Recv E: 0x{recv_e:03x}  {recv_e:010b}")
    return recv_n, recv_s, recv_w, recv_e, ghost_flags


def read_debug_neighbor_histogram(tile, label=""):
    hist  = [sram_read_byte(tile, DEBUG_NEIGHBOR_HIST + i) for i in range(9)]
    total = sum(hist)
    print(f"\n{label} Neighbor Histogram (total cells={total}):")
    for n in range(9):
        bar = "#" * min(hist[n], 40)
        print(f"  n={n}: {hist[n]:4d}  {bar}")
    return hist


def read_debug_iter_count(tile):
    return sram_read_word(tile, DEBUG_ITER_COUNT)


def print_debug_state(dut, r, c, label=""):
    tile     = get_tile(dut, r, c)
    iter_num = read_debug_iter_count(tile)
    hdr      = label if label else f"TILE ({r},{c})  iter={iter_num}"
    print(f"\n{'='*60}")
    print(hdr)
    print('='*60)
    read_debug_info(tile, hdr)
    read_debug_neighbor_histogram(tile, hdr)


GLOBAL_ROWS = MESH_R * SIZE
GLOBAL_COLS = MESH_C * SIZE


def expected_blinker_seed():
    exp = [[0] * SIZE for _ in range(SIZE)]
    exp[4][5] = 1
    exp[5][5] = 1
    exp[6][5] = 1
    exp[8][0] = 1
    exp[9][0] = 1
    exp[8][9] = 1
    exp[9][9] = 1
    return exp


def build_global_seed():
    tile_seed = expected_blinker_seed()
    g = [[0] * GLOBAL_COLS for _ in range(GLOBAL_ROWS)]
    for tr in range(MESH_R):
        for tc in range(MESH_C):
            for y in range(SIZE):
                for x in range(SIZE):
                    g[tr * SIZE + y][tc * SIZE + x] = tile_seed[y][x]
    return g


def gol_step_global(grid):
    new_grid = [[0] * GLOBAL_COLS for _ in range(GLOBAL_ROWS)]
    for y in range(GLOBAL_ROWS):
        for x in range(GLOBAL_COLS):
            n = 0
            for dy in [-1, 0, 1]:
                for dx in [-1, 0, 1]:
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < GLOBAL_COLS and 0 <= ny < GLOBAL_ROWS:
                        n += grid[ny][nx]
            cell = grid[y][x]
            new_grid[y][x] = 1 if (cell and n in (2, 3)) or (not cell and n == 3) else 0
    return new_grid


def get_tile_expected(iteration, tr, tc):
    g = GOL_GLOBAL[iteration]
    return [g[tr * SIZE + y][tc * SIZE: tc * SIZE + SIZE] for y in range(SIZE)]


GOL_GLOBAL = [build_global_seed()]
GOL_GLOBAL.append(gol_step_global(GOL_GLOBAL[0]))
GOL_GLOBAL.append(gol_step_global(GOL_GLOBAL[1]))


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


def print_iter_comparison(dut, r, c, iteration):
    tile = get_tile(dut, r, c)
    exp  = get_tile_expected(iteration, r, c)
    act  = read_grid_from_sram(tile)
    print(f"\n===================================================")
    print(f"TILE ({r},{c})  ITERATION {iteration}")
    print("EXPECTED (Python GoL)   ACTUAL (SRAM)")
    print("---------------------------------------------------")
    mismatches = 0
    for y in range(SIZE):
        exp_row = "".join("." if exp[y][x] else "#" for x in range(SIZE))
        act_row = "".join("." if act[y][x] else "#" for x in range(SIZE))
        marker  = "" if exp_row == act_row else "  <- MISMATCH"
        if marker:
            mismatches += sum(1 for x in range(SIZE)
                              if (exp[y][x] != 0) != (act[y][x] != 0))
        print(f"{exp_row}    {act_row}{marker}")
    print("===================================================")
    return mismatches


def diagnose_col_bitmap(dut, iteration):
    tile01 = get_tile(dut, 0, 1)
    tile00 = get_tile(dut, 0, 0)

    actual_col  = [sram_read_byte(tile01, SRAM_GRID_BASE + row * SIZE + 0)
                   for row in range(SIZE)]
    expected_bm = 0
    for row, val in enumerate(actual_col):
        if val & 1:
            expected_bm |= (1 << row)

    firmware_bm = sram_read_word(tile01, DEBUG_SEND_BM)
    received_bm = sram_read_word(tile00, DEBUG_LAST_RECV_E)

    print(f"\n{'='*60}")
    print(f"col_bitmap DIAGNOSTIC — after iter {iteration}")
    print(f"{'='*60}")
    print(f"  SRAM col 0 of tile(0,1):  {actual_col}")
    print(f"  Expected bitmap (py):     0x{expected_bm:03x}  ({expected_bm:010b})")
    print(f"  Firmware DEBUG_SEND_BM:   0x{firmware_bm:03x}  ({firmware_bm:010b})")
    print(f"  tile(0,0) DEBUG_RECV_E:   0x{received_bm:03x}  ({received_bm:010b})")

    results = read_col_bitmap_trace(tile01, f"tile(0,1) iter={iteration}")
    send_bm = results[0]

    if firmware_bm == expected_bm:
        print("  [OK]  firmware bitmap matches SRAM")
    else:
        print("  [FAIL] firmware bitmap does NOT match SRAM")
        if firmware_bm == 0:
            if results[5] == 0 and results[6] == 0:  # cell9_raw, bit9_val
                print("         -> grid wiped before col_bitmap ran")
            else:
                print("         -> bm accumulation broken OR testbench reading wrong address")
                print("            run test_magic_check first to verify address mapping!")

    if received_bm != expected_bm:
        print(f"  [FAIL] tile(0,0) received 0x{received_bm:03x} but expected 0x{expected_bm:03x}")
    else:
        print("  [OK]  tile(0,0) received correct bitmap")
    print()


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


# ---------------------------------------------------------------------------
# MAGIC CHECK TEST — run this first to verify testbench address mapping
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_magic_check(dut):
    """
    Firmware writes 0xDEADBEEF to 0x0730 and 0xCAFEBABE to 0x0734
    at the very start of main(). If the testbench reads these back
    correctly, address mapping is confirmed good.
    If not, sram_read_word() is reading the wrong physical location.
    """
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
            status1 = "[OK]  " if ok1 else "[FAIL]"
            status2 = "[OK]  " if ok2 else "[FAIL]"
            print(f"\nTILE ({r},{c}):")
            print(f"  {status1} 0x0730 = 0x{magic1:08x}  (expect 0xDEADBEEF)")
            print(f"  {status2} 0x0734 = 0x{magic2:08x}  (expect 0xCAFEBABE)")

            mem       = tile.sram_inst.mem
            elem_bits = len(mem[0])
            print(f"  SRAM: elem_bits={elem_bits}, depth={len(mem)}")
            print(f"  Raw SRAM words around 0x0730:")
            base_word = 0x0730 // (elem_bits // 8)
            for wi in range(max(0, base_word - 2), base_word + 6):
                raw = int(mem[wi].value)
                print(f"    mem[{wi}] = 0x{raw:08x}")

            if not (ok1 and ok2):
                all_pass = False
                print(f"  -> ADDRESS MAPPING BROKEN for tile ({r},{c})")
                print(f"     sram_read_word is not reading what firmware wrote")

    if all_pass:
        dut._log.info("MAGIC CHECK PASSED — address mapping confirmed correct on all tiles")
    else:
        dut._log.error("MAGIC CHECK FAILED — fix sram_read_word() address mapping before debugging firmware")

    assert all_pass, "Magic check failed: testbench cannot correctly read firmware-written addresses"


@cocotb.test()
async def test_iter0_seed_only(dut):
    await boot_mesh(dut)
    await Timer(SEED_SAMPLE_US, unit="us")

    print("\n\n*** SEED SANITY CHECK — all tiles ***")
    for r in range(MESH_R):
        for c in range(MESH_C):
            tile = get_tile(dut, r, c)
            read_debug_info(tile, f"TILE ({r},{c}) seed sanity")
            dump_region(tile, SRAM_GRID_BASE, 100)

    print("\n\n******** ITERATION 0 (seed) ********")
    total = 0
    for r in range(MESH_R):
        for c in range(MESH_C):
            total += print_iter_comparison(dut, r, c, 0)

    if total == 0:
        dut._log.info("ALL TILES: seed matches")
    else:
        dut._log.error(f"{total} mismatches in seed")
    assert total == 0, f"Seed mismatch: {total} cells wrong."


@cocotb.test()
async def test_col_bitmap_diagnostic(dut):
    await boot_mesh(dut)
    await Timer(SEED_SAMPLE_US, unit="us")

    tile00 = get_tile(dut, 0, 0)
    tile01 = get_tile(dut, 0, 1)

    print("\n\n*** col_bitmap DIAGNOSTIC — ITER 0 ***")
    diagnose_col_bitmap(dut, iteration=0)

    print("\nTILE (0,1) grid dump at ITER 0:")
    dump_region(tile01, SRAM_GRID_BASE, 100)

    dut._log.info("Waiting for iter_count >= 1 on tile(0,0) ...")
    seen = await wait_for_iter(tile00, 1)
    if not seen:
        dut._log.warning("iter_count did not advance to 1 within timeout")
    await Timer(10, unit="us")

    print("\n\n*** col_bitmap DIAGNOSTIC — ITER 1 ***")
    diagnose_col_bitmap(dut, iteration=1)

    print("\nTILE (0,1) grid dump at ITER 1:")
    dump_region(tile01, SRAM_GRID_BASE, 100)

    print("\nTILE (0,1) full debug info at ITER 1:")
    read_debug_info(tile01, "TILE (0,1) iter=1")

    print("\nTILE (0,0) full debug info at ITER 1:")
    read_debug_info(tile00, "TILE (0,0) iter=1")

    print("\n\n******** ITERATION 1 — tile(0,0) only ********")
    mismatches = print_iter_comparison(dut, 0, 0, 1)
    if mismatches == 0:
        dut._log.info("tile(0,0) iter=1: PASS")
    else:
        dut._log.error(f"tile(0,0) iter=1: {mismatches} mismatches")
        dump_region(tile00, SRAM_GRID_BASE, 100)
        dump_region(tile00, 0x0600, 40)
        dump_region(tile00, DEBUG_BASE, 128)

    assert mismatches == 0, f"col_bitmap test: {mismatches} cell mismatches in tile(0,0) iter=1"


@cocotb.test()
async def test_gol_iter1_iter2(dut):
    await boot_mesh(dut)
    await Timer(SEED_SAMPLE_US, unit="us")

    print("\n\n*** SEED SANITY CHECK — all tiles ***")
    for r in range(MESH_R):
        for c in range(MESH_C):
            tile = get_tile(dut, r, c)
            read_debug_info(tile, f"TILE ({r},{c}) seed sanity")

    print("\n\n******** ITERATION 0 (seed) ********")
    iter0_mismatches = 0
    for r in range(MESH_R):
        for c in range(MESH_C):
            iter0_mismatches += print_iter_comparison(dut, r, c, 0)

    if iter0_mismatches == 0:
        dut._log.info("Iter 0: ALL tiles match seed")
    else:
        dut._log.error(f"Iter 0: {iter0_mismatches} mismatches")

    tile00 = get_tile(dut, 0, 0)
    dut._log.info("Waiting for debug_iter_count >= 1 ...")
    seen = await wait_for_iter(tile00, 1)
    if not seen:
        dut._log.warning("iter_count did not advance to 1")
    await Timer(10, unit="us")

    print("\n\n*** col_bitmap DIAGNOSTIC at ITER 1 ***")
    diagnose_col_bitmap(dut, iteration=1)

    print("\n\n******** ITERATION 1 ********")
    iter1_mismatches = 0
    for r in range(MESH_R):
        for c in range(MESH_C):
            m = print_iter_comparison(dut, r, c, 1)
            iter1_mismatches += m
            if m > 0:
                print_debug_state(dut, r, c, f"TILE ({r},{c}) iter=1 MISMATCH debug")

    if iter1_mismatches == 0:
        dut._log.info("Iter 1: ALL tiles match")
    else:
        dut._log.error(f"Iter 1: {iter1_mismatches} mismatches")
        for r in range(MESH_R):
            for c in range(MESH_C):
                dump_region(get_tile(dut, r, c), SRAM_GRID_BASE, 100)
                dump_region(get_tile(dut, r, c), 0x0600, 40)
                dump_region(get_tile(dut, r, c), DEBUG_BASE, 128)

    dut._log.info("Waiting for debug_iter_count >= 2 ...")
    seen = await wait_for_iter(tile00, 2)
    if not seen:
        dut._log.warning("iter_count did not advance to 2")
    await Timer(10, unit="us")

    print("\n\n*** col_bitmap DIAGNOSTIC at ITER 2 ***")
    diagnose_col_bitmap(dut, iteration=2)

    print("\n\n******** ITERATION 2 ********")
    iter2_mismatches = 0
    for r in range(MESH_R):
        for c in range(MESH_C):
            m = print_iter_comparison(dut, r, c, 2)
            iter2_mismatches += m
            if m > 0:
                print_debug_state(dut, r, c, f"TILE ({r},{c}) iter=2 MISMATCH debug")

    if iter2_mismatches == 0:
        dut._log.info("Iter 2: ALL tiles match")
    else:
        dut._log.error(f"Iter 2: {iter2_mismatches} mismatches")

    total = iter0_mismatches + iter1_mismatches + iter2_mismatches
    assert total == 0, (
        f"GoL test failed: iter0={iter0_mismatches}, "
        f"iter1={iter1_mismatches}, iter2={iter2_mismatches} mismatches."
    )