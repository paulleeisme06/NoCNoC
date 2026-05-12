import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, RisingEdge, FallingEdge

MESH_R = 5
MESH_C = 5
FIRMWARE_BIN_NAME = "firmware.bin"

SRAM_GRID_BASE   = 0x0500
DEBUG_BASE       = 0x0700
DEBUG_MY_ID      = DEBUG_BASE + 0
DEBUG_TOKEN_RECV = DEBUG_BASE + 4
DEBUG_DIR_RECV   = DEBUG_BASE + 8
DEBUG_TOKEN_SENT = DEBUG_BASE + 12
DEBUG_DEST_SENT  = DEBUG_BASE + 16
DEBUG_DONE       = DEBUG_BASE + 20

def load_firmware_binary():
    candidates = [
        os.path.abspath(FIRMWARE_BIN_NAME),  # current working directory
        os.path.abspath(os.path.join(os.getcwd(), FIRMWARE_BIN_NAME)),
        os.path.abspath(os.path.join(os.path.dirname(__file__), FIRMWARE_BIN_NAME)),
    ]

    for bin_file in candidates:
        if os.path.exists(bin_file):
            with open(bin_file, "rb") as f:
                data = list(f.read())
            if len(data) < 2048:
                data += [0] * (2048 - len(data))
            return data[:2048]

    raise FileNotFoundError("Could not find firmware.bin. Checked:\n" + "\n".join(candidates))

FIRMWARE = load_firmware_binary()

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

@cocotb.test()
async def test_snake_pattern(dut):
    """
    Test that the snake token propagates all the way to 0xDEAD in every tile.
    """
    await boot_mesh(dut)
    
    POLL_NS = 500
    timeout_us = 5000
    limit = int(timeout_us * 1000 / POLL_NS)
    
    dut._log.info("Mesh booted, checking token propagation...")

    # Wait until all tiles have DEBUG_DONE == 0xDEAD
    all_done = False
    last_map = ""
    
    for _ in range(limit):
        await Timer(POLL_NS, unit="ns")
        
        lines = []
        lines.append("="*60)
        lines.append(" SNAKE ROUTING PROGRESS MAP ")
        lines.append("="*60)
        
        all_true = True
        
        for r in range(MESH_R):
            row_cells = []
            for c in range(MESH_C):
                
                tile = get_tile(dut, r, c)
                my_id = sram_read_word(tile, DEBUG_MY_ID)
                token = sram_read_word(tile, DEBUG_TOKEN_RECV)
                done = sram_read_word(tile, DEBUG_DONE)
                
                if done != 0xDEAD:
                    all_true = False
                
                sym = "WAIT"
                if done == 0xDEAD:
                    sym = "DONE"
                elif token != 0:
                    sym = "RECV"
                    
                rx_from = "--"
                if token != 0:
                    rx_from = f"{(token & 0xFF):02x}"
                    
                # Format: [ID|RX:xx|STAT] -> 16 chars
                row_cells.append(f"[{my_id:02x}|RX:{rx_from}|{sym}]")
                
            if r % 2 == 0:
                lines.append(" ---> ".join(row_cells))
            else:
                lines.append(" <--- ".join(row_cells))
                
            if r < MESH_R - 1:
                # Vertical line
                if r % 2 == 0:
                    # Drop down on the right
                    offset = (16 * MESH_C + 6 * (MESH_C - 1)) - 8
                    lines.append(" " * offset + "|")
                    lines.append(" " * offset + "v")
                else:
                    # Drop down on the left
                    offset = 8
                    lines.append(" " * offset + "|")
                    lines.append(" " * offset + "v")
                    
        map_str = "\n".join(lines)
        if map_str != last_map:
            dut._log.info("\n" + map_str)
            last_map = map_str
            
        if all_true:
            all_done = True
            break

    assert all_done, "Timeout waiting for all tiles to finish snake pattern."
    dut._log.info("Token cleanly arrived at all nodes!")
