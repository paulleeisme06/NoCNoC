import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer

FIRMWARE_BIN = "firmware.bin"

def load_firmware():
    base = os.path.dirname(__file__)
    candidates = [
        os.path.join(base, FIRMWARE_BIN),
        os.path.join(base, "..", "src", "firmware", FIRMWARE_BIN),
        os.path.join(base, "..", "firmware", FIRMWARE_BIN),
    ]
    for p in candidates:
        p = os.path.normpath(p)
        if os.path.exists(p):
            with open(p, "rb") as f:
                return list(f.read())
    return [0x13, 0x01, 0xc0, 0x7f] + [0x00] * 2044 # andrea changed 1024 to 2044 



FIRMWARE = load_firmware()


async def spi_flash_stream(dut):
    while True:
        await FallingEdge(dut.flash_csb)
                # Skip 32 bits command+address
        for _ in range(32):
            await RisingEdge(dut.flash_clk)
        byte_idx = 0
        while True:
            for bit in range(7, -1, -1):
                await FallingEdge(dut.flash_clk)
                b = FIRMWARE[byte_idx] if byte_idx < len(FIRMWARE) else 0xFF
                dut.flash_miso.value = (b >> bit) & 1
                await RisingEdge(dut.flash_clk)  # wait for sample
            byte_idx += 1
            try:
                if int(dut.flash_csb.value) == 1:
                    break
            except Exception:
                break
        dut.flash_miso.value = 0

async def sram_read_byte(dut, tile_id, addr):b# andrea added this for testing purposes 
    """Read one byte from tile SRAM via top-level backdoor ports."""
    dut.tb_tile_sel.value = tile_id
    dut.tb_raddr.value    = addr & 0x7FF
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    return int(dut.tb_rdata.value) & 0xFF


async def sram_dump(dut, r, c, start=0x000, length=32):
    """Hex dump <length> bytes of tile(r,c) SRAM starting at <start>."""
    tile_id = (r << 2) | c
    print(f"\n  [SRAM dump] tile({r},{c})  TILE_ID={tile_id}"
          f"  addr 0x{start:03x}..0x{start+length-1:03x}")
    for off in range(0, length, 16):
        chunk = []
        for i in range(min(16, length - off)):
            chunk.append(await sram_read_byte(dut, tile_id, start + off + i))
        hex_str  = " ".join(f"{b:02x}" for b in chunk)
        ascii_str = "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in chunk)
        print(f"    0x{start+off:03x}:  {hex_str:<48}  {ascii_str}")


@cocotb.test()
async def flash_populates_all_tile_srams(dut):
    dut.flash_miso.value  = 0
    dut.host_csb.value    = 1
    dut.host_sclk.value   = 0
    dut.host_mosi.value   = 0
    dut.tb_tile_sel.value = 0
    dut.tb_raddr.value    = 0

    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    cocotb.start_soon(spi_flash_stream(dut))

    dut.rst.value = 1
    for _ in range(8):
        await RisingEdge(dut.clk)
    dut.rst.value = 0

    dut._log.info("Waiting for boot_controller to finish loading firmware ...")
    for cycle in range(600_000):
        await RisingEdge(dut.clk)
        try:
            v = dut.mesh_inst.cpu_rst_n.value
            if v.is_resolvable and int(v) == 1:
                dut._log.info(f"cpu_rst_n asserted at cycle {cycle}")
                break
        except Exception:
            pass

    # settle
    for _ in range(10):
        await RisingEdge(dut.clk)

    N_CHECK = 4
    exp = FIRMWARE[:N_CHECK]
    dut._log.info(f"Expected SRAM[0:{N_CHECK}] = {[hex(b) for b in exp]}")

    errors = 0
    tiles = [
        (0,0), (0,1), (0,2),
        (1,0), (1,1), (1,2),
        (2,0), (2,1), (2,2),
    ]

    # andrea added this, to read srams using hte backdoor approch 
    for (r, c) in tiles:
        tile_id = (r << 2) | c
        actual  = [await sram_read_byte(dut, tile_id, addr) for addr in range(N_CHECK)]
        if actual == exp:
            dut._log.info(f"tile({r},{c}): PASS  {[hex(b) for b in actual]}")
        else:
            dut._log.error(
                f"tile({r},{c}): MISMATCH  "
                f"got={[hex(b) for b in actual]}  "
                f"exp={[hex(b) for b in exp]}"
            )
            errors += 1
# andrea added this, to read srams using hte backdoor approch 
    if errors == 0:
        dut._log.info("ALL 9 TILES PASS — flash boot verified")

    # ── SRAM dump — all 9 tiles ──────────────────────────────────────────────
    print("\n\n=== SRAM DUMP — all 9 tiles (bytes 0x000..0x01f) ===")
    for r in range(3):
        for c in range(3):
            await sram_dump(dut, r, c, start=0x000, length=32)

    if errors:
        raise AssertionError(f"{errors} tile(s) failed")