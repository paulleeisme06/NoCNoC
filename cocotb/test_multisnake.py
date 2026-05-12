"""
test_multisnake.py — cocotb testbench for all four snake patterns.

Each test:
  1. Boots the mesh (shared SPI flash responder).
  2. Polls until all 25 tiles write DEBUG_DONE == 0xDEAD.
  3. For every tile verifies:
       - DEBUG_TOKEN_RECV contains TOKEN_VALID_BIT (bit 10) set.
       - The low 8 bits of DEBUG_TOKEN_RECV equal the TILE_ID of the
         expected predecessor in the snake order (sender embeds its own id).
       - DEBUG_DIR_RECV matches the expected direction code.
  4. Prints an ASCII progress map while waiting.

Run each pattern with a different firmware.bin compiled with -DPATTERN=N.
The Makefile should build four targets and run four separate sim invocations.
If you want to run them from a single sim, reset and reflash between tests —
see the note at the bottom of this file.

Design notes on direction coverage
───────────────────────────────────
Pattern 0 (H-snake):   tests E, W, N  (S is traversed as N from receiver POV)
Pattern 1 (diag-SE):   tests sw_out→ne_in (intra-antidiag) + S and E transitions
Pattern 2 (diag-SW):   tests ne_out→sw_in (intra-antidiag) + S and W transitions
Pattern 3 (V-snake):   tests N, S, W  (orthogonal complement of pattern 0)

Together the four patterns exercise all 8 directional output ports of the router.
"""

import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, RisingEdge, FallingEdge

MESH_R = 5
MESH_C = 5
FIRMWARE_BIN_NAME = "firmware.bin"

DEBUG_BASE       = 0x0700
DEBUG_MY_ID      = DEBUG_BASE + 0
DEBUG_TOKEN_RECV = DEBUG_BASE + 4
DEBUG_DIR_RECV   = DEBUG_BASE + 8
DEBUG_TOKEN_SENT = DEBUG_BASE + 12
DEBUG_DEST_SENT  = DEBUG_BASE + 16
DEBUG_DONE       = DEBUG_BASE + 20
DEBUG_PATTERN    = DEBUG_BASE + 24

TOKEN_VALID_BIT  = (1 << 10)

# Direction codes — must match firmware.c
DIR_SELF  = 0
DIR_WEST  = 1
DIR_EAST  = 2
DIR_NORTH = 3
DIR_SOUTH = 4
DIR_NW    = 5
DIR_NE    = 6
DIR_SE    = 7
DIR_SW    = 8

DIR_NAME = {
    DIR_SELF: "SELF", DIR_WEST: "W", DIR_EAST: "E",
    DIR_NORTH: "N",   DIR_SOUTH: "S",
    DIR_NW: "NW",     DIR_NE: "NE", DIR_SE: "SE", DIR_SW: "SW",
}


# ─────────────────────────────────────────────────────────────────────────────
# Firmware loader
# ─────────────────────────────────────────────────────────────────────────────

def load_firmware_binary():
    candidates = [
        os.path.abspath(FIRMWARE_BIN_NAME),
        os.path.abspath(os.path.join(os.getcwd(), FIRMWARE_BIN_NAME)),
        os.path.abspath(os.path.join(os.path.dirname(__file__), FIRMWARE_BIN_NAME)),
    ]
    for p in candidates:
        if os.path.exists(p):
            with open(p, "rb") as f:
                data = list(f.read())
            if len(data) < 2048:
                data += [0] * (2048 - len(data))
            return data[:2048]
    raise FileNotFoundError("firmware.bin not found. Checked:\n" + "\n".join(candidates))

FIRMWARE = load_firmware_binary()


# ─────────────────────────────────────────────────────────────────────────────
# SPI / boot helpers
# ─────────────────────────────────────────────────────────────────────────────

async def spi_flash_responder(dut):
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
            try:
                if int(dut.flash_cs_n.value) == 1:
                    break
            except Exception:
                break

async def wait_boot(dut):
    while int(dut.cpu_rst_n.value) == 0:
        await Timer(500, unit="ns")

async def boot_mesh(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.flash_miso.value = 0
    if hasattr(dut, "inject_00_nw"):
        dut.inject_00_nw.value = 0
    await Timer(2, unit="ms")
    dut.rst.value = 0
    cocotb.start_soon(spi_flash_responder(dut))
    await wait_boot(dut)


# ─────────────────────────────────────────────────────────────────────────────
# SRAM read helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_tile(dut, r, c):
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


# ─────────────────────────────────────────────────────────────────────────────
# Snake-ordering helpers (Python mirror of the C firmware logic)
# ─────────────────────────────────────────────────────────────────────────────

def tile_id(r, c):
    return ((r & 7) << 3) | (c & 7)


# Pattern 0 ──────────────────────────────────────────────────────────────────

def p0_order():
    """Return list of (r,c) in horizontal-boustrophedon visit order."""
    result = []
    for r in range(MESH_R):
        cols = range(MESH_C) if r % 2 == 0 else range(MESH_C - 1, -1, -1)
        for c in cols:
            result.append((r, c))
    return result


# Pattern 1 ──────────────────────────────────────────────────────────────────

def p1_order():
    """Antidiagonal snake — increasing row within each antidiagonal."""
    result = []
    for d in range(MESH_R + MESH_C - 1):
        r_start = max(0, d - (MESH_C - 1))
        r_end   = min(d, MESH_R - 1)
        for r in range(r_start, r_end + 1):
            c = d - r
            result.append((r, c))
    return result


# Pattern 2 ──────────────────────────────────────────────────────────────────

def p2_order():
    """Antidiagonal snake — decreasing row (= increasing col) within each antidiagonal."""
    result = []
    for d in range(MESH_R + MESH_C - 1):
        r_start = max(0, d - (MESH_C - 1))
        r_end   = min(d, MESH_R - 1)
        for r in range(r_end, r_start - 1, -1):
            c = d - r
            result.append((r, c))
    return result


# Pattern 3 ──────────────────────────────────────────────────────────────────

def p3_order():
    """Vertical boustrophedon — even cols top-down, odd cols bottom-up."""
    result = []
    for c in range(MESH_C):
        rows = range(MESH_R) if c % 2 == 0 else range(MESH_R - 1, -1, -1)
        for r in rows:
            result.append((r, c))
    return result


PATTERN_ORDERS = [p0_order(), p1_order(), p2_order(), p3_order()]

PATTERN_NAMES = [
    "Horizontal boustrophedon (H-snake)",
    "SE antidiag diagonal snake",
    "SW antidiag diagonal snake",
    "Vertical boustrophedon (V-snake)",
]


def expected_dir(pat_order, r, c):
    """
    Return the direction code the tile at (r,c) should record in DEBUG_DIR_RECV.
    """
    idx = pat_order.index((r, c))
    if idx == 0:
        return DIR_SELF
    pr, pc = pat_order[idx - 1]
    dr, dc = r - pr, c - pc        # displacement FROM predecessor TO us
    # Cardinal
    if dr == -1 and dc ==  0: return DIR_SOUTH  # pred is below us → came from S
    if dr ==  1 and dc ==  0: return DIR_NORTH  # pred is above us → came from N
    if dr ==  0 and dc == -1: return DIR_EAST   # pred is to our left → came from W
                                                  # wait: pred is to our LEFT means
                                                  # pred.col < our col, so dc = c - pc > 0
                                                  # meaning pred sent EAST → we came from W
    if dr ==  0 and dc ==  1: return DIR_WEST   # pred is to our right → token came from E
    # Diagonal
    if dr == -1 and dc ==  1: return DIR_SW   # pred is NE of us; it used sw_out → our ne_in
    if dr ==  1 and dc == -1: return DIR_NE   # pred is SW of us; it used ne_out → our sw_in
    if dr == -1 and dc == -1: return DIR_SE   # pred is NW of us (unused in patterns 1&2)
    if dr ==  1 and dc ==  1: return DIR_NW   # pred is SE of us; sw_out? no: se_out → our nw_in
    return DIR_SELF  # fallback — should not occur


# ─────────────────────────────────────────────────────────────────────────────
# Progress-map printer
# ─────────────────────────────────────────────────────────────────────────────

def build_progress_map(dut, pattern_id):
    # Auto-detect the pattern the firmware actually ran from DEBUG_PATTERN
    # in tile (0,0) so the visit-index column matches what really executed.
    try:
        actual_id = sram_read_word(get_tile(dut, 0, 0), DEBUG_PATTERN)
        if not (0 <= actual_id < len(PATTERN_ORDERS)):
            actual_id = pattern_id
    except Exception:
        actual_id = pattern_id

    lines = []
    lines.append("=" * 72)
    mismatch = (f"  !! firmware=P{actual_id}, test expects P{pattern_id}"
                if actual_id != pattern_id else "")
    lines.append(f" Pattern {actual_id}: {PATTERN_NAMES[actual_id]}{mismatch}")
    lines.append("=" * 72)
    order = PATTERN_ORDERS[actual_id]
    visit_idx = {rc: i for i, rc in enumerate(order)}

    for r in range(MESH_R):
        row_cells = []
        for c in range(MESH_C):
            tile  = get_tile(dut, r, c)
            my_id = sram_read_word(tile, DEBUG_MY_ID)
            token = sram_read_word(tile, DEBUG_TOKEN_RECV)
            done  = sram_read_word(tile, DEBUG_DONE)
            vidx  = visit_idx.get((r, c), -1)

            sym = "WAIT"
            if done == 0xDEAD:
                sym = "DONE"
            elif token & TOKEN_VALID_BIT:
                sym = "RECV"

            rx_from = f"{token & 0xFF:02x}" if (token & TOKEN_VALID_BIT) else "--"
            row_cells.append(f"[{vidx:02d}|{my_id:02x}|RX:{rx_from}|{sym}]")

        sep = " --> " if (r % 2 == 0) else " <-- "
        if actual_id in (1, 2, 3):
            sep = "  |  "
        lines.append(sep.join(row_cells))

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Core verification logic
# ─────────────────────────────────────────────────────────────────────────────

async def run_snake_test(dut, pattern_id, timeout_us=8000):
    """
    Wait for all tiles to finish, then verify correctness.
    Returns list of failure strings (empty = pass).
    """
    order = PATTERN_ORDERS[pattern_id]
    POLL_NS  = 500
    limit    = int(timeout_us * 1000 / POLL_NS)
    last_map = ""
    all_done = False

    for _ in range(limit):
        await Timer(POLL_NS, unit="ns")
        done_count = sum(
            1 for r in range(MESH_R) for c in range(MESH_C)
            if sram_read_word(get_tile(dut, r, c), DEBUG_DONE) == 0xDEAD
        )
        if done_count == MESH_R * MESH_C:
            all_done = True
            break

        # Throttle map printing
        new_map = build_progress_map(dut, pattern_id)
        if new_map != last_map:
            dut._log.info("\n" + new_map)
            last_map = new_map

    if not all_done:
        dut._log.error("\n" + build_progress_map(dut, pattern_id))
        return [f"Pattern {pattern_id}: TIMEOUT — not all tiles finished in {timeout_us} µs"]

    dut._log.info("\n" + build_progress_map(dut, pattern_id))

    # ── Guard: check that the loaded firmware actually ran this pattern ──────
    # Read DEBUG_PATTERN from every tile and confirm they all agree and match
    # the expected pattern_id.  A mismatch means the wrong firmware.bin was
    # flashed (e.g. built with -DPATTERN=0 but this test expects PATTERN=1).
    firmware_patterns = set()
    for r in range(MESH_R):
        for c in range(MESH_C):
            fw_pat = sram_read_word(get_tile(dut, r, c), DEBUG_PATTERN)
            firmware_patterns.add(fw_pat)

    if len(firmware_patterns) != 1:
        return [
            f"Pattern {pattern_id}: inconsistent DEBUG_PATTERN values across tiles: "
            f"{sorted(firmware_patterns)} — possible boot/SRAM issue"
        ]

    actual_pattern = next(iter(firmware_patterns))
    if actual_pattern != pattern_id:
        return [
            f"Wrong firmware loaded: firmware reports PATTERN={actual_pattern} "
            f"({PATTERN_NAMES[actual_pattern]}) but this test expects PATTERN={pattern_id} "
            f"({PATTERN_NAMES[pattern_id]}). "
            f"Rebuild firmware with -DPATTERN={pattern_id} and rerun."
        ]

    # ── Verify every tile ───────────────────────────────────────────────────
    failures = []
    for i, (r, c) in enumerate(order):
        tile    = get_tile(dut, r, c)
        t_recv  = sram_read_word(tile, DEBUG_TOKEN_RECV)
        d_recv  = sram_read_word(tile, DEBUG_DIR_RECV)

        # Head tile: should have TOKEN_VALID_BIT set and dir == DIR_SELF
        if i == 0:
            if not (t_recv & TOKEN_VALID_BIT):
                failures.append(
                    f"P{pattern_id} head ({r},{c}): TOKEN_VALID_BIT not set in recv "
                    f"(got 0x{t_recv:08x})"
                )
            if d_recv != DIR_SELF:
                failures.append(
                    f"P{pattern_id} head ({r},{c}): expected DIR_SELF({DIR_SELF}), "
                    f"got {d_recv} ({DIR_NAME.get(d_recv,'?')})"
                )
            continue

        # Every other tile: token should carry sender's TILE_ID
        pr, pc    = order[i - 1]
        sender_id = tile_id(pr, pc)
        recv_id   = t_recv & 0xFF

        if not (t_recv & TOKEN_VALID_BIT):
            failures.append(
                f"P{pattern_id} ({r},{c}) idx={i}: TOKEN_VALID_BIT not set "
                f"(got 0x{t_recv:08x})"
            )
        elif recv_id != sender_id:
            failures.append(
                f"P{pattern_id} ({r},{c}) idx={i}: expected sender tile_id=0x{sender_id:02x} "
                f"but got 0x{recv_id:02x} (raw recv=0x{t_recv:08x})"
            )

        exp_dir = expected_dir(order, r, c)
        if d_recv != exp_dir:
            failures.append(
                f"P{pattern_id} ({r},{c}) idx={i}: expected dir "
                f"{DIR_NAME.get(exp_dir,'?')}({exp_dir}), "
                f"got {DIR_NAME.get(d_recv,'?')}({d_recv})"
            )

    return failures


# ─────────────────────────────────────────────────────────────────────────────
# Cocotb tests — one per pattern
# ─────────────────────────────────────────────────────────────────────────────

@cocotb.test()
async def test_pattern0_hsnake(dut):
    """Pattern 0: horizontal boustrophedon — baseline E/W/N links."""
    await boot_mesh(dut)
    dut._log.info("=== Running Pattern 0: H-snake ===")
    failures = await run_snake_test(dut, 0)
    assert not failures, "Pattern 0 failures:\n" + "\n".join(failures)
    dut._log.info("Pattern 0 PASSED")


@cocotb.test()
async def test_pattern1_diag_se(dut):
    """Pattern 1: SE antidiagonal snake — exercises ne_in / sw_out diagonal links."""
    await boot_mesh(dut)
    dut._log.info("=== Running Pattern 1: SE diagonal snake ===")
    failures = await run_snake_test(dut, 1)
    assert not failures, "Pattern 1 failures:\n" + "\n".join(failures)
    dut._log.info("Pattern 1 PASSED — all diagonal SW→NE links verified")


@cocotb.test()
async def test_pattern2_diag_sw(dut):
    """Pattern 2: SW antidiagonal snake — exercises sw_in / ne_out diagonal links."""
    await boot_mesh(dut)
    dut._log.info("=== Running Pattern 2: SW diagonal snake ===")
    failures = await run_snake_test(dut, 2)
    assert not failures, "Pattern 2 failures:\n" + "\n".join(failures)
    dut._log.info("Pattern 2 PASSED — all diagonal NE→SW links verified")


@cocotb.test()
async def test_pattern3_vsnake(dut):
    """Pattern 3: vertical boustrophedon — N/S/E link stress in column order."""
    await boot_mesh(dut)
    dut._log.info("=== Running Pattern 3: V-snake ===")
    failures = await run_snake_test(dut, 3)
    assert not failures, "Pattern 3 failures:\n" + "\n".join(failures)
    dut._log.info("Pattern 3 PASSED")


# ─────────────────────────────────────────────────────────────────────────────
# Combined test — runs all 4 patterns, collects all failures before asserting.
# Useful when the harness can reflash and soft-reset between patterns.
# ─────────────────────────────────────────────────────────────────────────────

@cocotb.test()
async def test_all_patterns(dut):
    """
    Run all four snake patterns end to end.

    This test expects four separate firmware.bin files to exist as:
        firmware_p0.bin, firmware_p1.bin, firmware_p2.bin, firmware_p3.bin
    and reflashes+resets the mesh between patterns.

    If your sim flow doesn't support dynamic reflash, disable this test and
    use the four individual tests above with four separate sim runs.
    """
    global FIRMWARE

    all_failures = []

    for pat in range(4):
        # Load per-pattern firmware if available, else fall back to current FIRMWARE
        bin_path = f"firmware_p{pat}.bin"
        if os.path.exists(bin_path):
            with open(bin_path, "rb") as f:
                data = list(f.read())
            if len(data) < 2048:
                data += [0] * (2048 - len(data))
            FIRMWARE = data[:2048]
            dut._log.info(f"Loaded {bin_path}")
        else:
            dut._log.warning(
                f"{bin_path} not found — using previously loaded firmware "
                "(pattern-specific verification may fail if it doesn't match)"
            )

        # Reset + boot
        dut.rst.value = 1
        await Timer(2, unit="ms")
        dut.rst.value = 0
        await wait_boot(dut)

        dut._log.info(f"\n{'='*60}\nPattern {pat}: {PATTERN_NAMES[pat]}\n{'='*60}")
        failures = await run_snake_test(dut, pat, timeout_us=10000)
        all_failures.extend(failures)

        if failures:
            dut._log.error(f"Pattern {pat} FAILED:\n" + "\n".join(failures))
        else:
            dut._log.info(f"Pattern {pat} PASSED")

    assert not all_failures, (
        f"{len(all_failures)} failure(s) across all patterns:\n"
        + "\n".join(all_failures)
    )
    dut._log.info("All 4 patterns PASSED — full diagonal + cardinal connectivity verified")
