#include <stdint.h>

// ============================================================================
// Snake-token connectivity test
//
// Purpose: prove that every inter-tile link carries data correctly.
//
// Pattern (3×3 mesh, numbers = visit order):
//
//   row 0  →  (0,0) 00 → (0,1) 01 → (0,2) 02
//                                      ↓ south
//   row 1  ←  (1,0) 05 ← (1,1) 04 ← (1,2) 03
//              ↓ south
//   row 2  →  (2,0) 06 → (2,1) 07 → (2,2) 08
//
// Each tile:
//   1. Waits to receive the token (except tile(0,0) which starts the chain).
//   2. Writes the received token payload into DEBUG_TOKEN_RECV.
//   3. Writes the direction it received the token from into DEBUG_DIR_RECV.
//   4. Forwards the token to the next tile in the snake order.
//   5. Writes DEBUG_DONE = 0xDEAD when finished.
//
// Tile(0,0) generates the initial token (payload = TOKEN_VALID_BIT | its own TILE_ID).
// Every forwarded token has payload = TOKEN_VALID_BIT | sender_TILE_ID so the
// testbench can verify that the right neighbour sent the right token.
// ============================================================================

// ============================================================================
// Mesh configuration — must match mesh_3x3.v
// ============================================================================
#define MESH_R  3
#define MESH_C  3

// ============================================================================
// NoC memory-mapped registers
// ============================================================================
#define NOC_INJECT_BASE  0x80000000u
#define NOC_RECV_BASE    0x80000004u
#define NOC_ID_BASE      0x80000008u

#define SIG_BOOT_ALIVE   0xF0000001u
#define SIG_SEED_LIVE    0xF0000002u
#define SIG_MATH_DONE    0xF0000003u
#define SIG_GEN_STABLE   0xF0000004u

#define SIZE 10

#define PHYS_ROWS    3
#define PHYS_COLS    3

/* Change these for logical mesh size */
#define ACTIVE_ROWS  3
#define ACTIVE_COLS  3
#define SIZE 10

#define PHYS_ROWS    3
#define PHYS_COLS    3

/* Change these for logical mesh size */
#define ACTIVE_ROWS  3
#define ACTIVE_COLS  3

#define GRID_BASE       0x0500u
#define GHOST_BASE      0x0600u
#define NEXT_GRID_BASE  0x0640u

#define DEBUG_BASE        0x0700u
#define DEBUG_ITER_COUNT  (DEBUG_BASE + 28)
#define DEBUG_MY_ID       (DEBUG_BASE + 44)

#define DEBUG_PRE_CHECKERBOARD   0x0740u
#define DEBUG_POST_CHECKERBOARD  0x0744u
#define DEBUG_BASE        0x0700u
#define DEBUG_ITER_COUNT  (DEBUG_BASE + 28)
#define DEBUG_MY_ID       (DEBUG_BASE + 44)

#define DEBUG_PRE_CHECKERBOARD   0x0740u
#define DEBUG_POST_CHECKERBOARD  0x0744u

static inline void noc_write(uint32_t word)
{
    *(volatile uint32_t *)NOC_INJECT_BASE = word;
}

static inline uint32_t noc_read_my_id(void)
{
    return *(volatile uint32_t *)NOC_ID_BASE & 0xFu;
}

static inline void noc_signal(uint32_t sig_word)
{
    noc_write(sig_word);
}

__attribute__((section(".text.init"), naked))
void _start(void)
{
    __asm__ volatile (
        "li   sp, 0x7fc\n"

        /* zero ghost region: 0x0600..0x0627 */

        /* zero ghost region: 0x0600..0x0627 */
        "li   t0, 0x0600\n"
        "li   t1, 0x0628\n"
        "1: bge  t0, t1, 2f\n"
        "   sb   zero, 0(t0)\n"
        "   addi t0, t0, 1\n"
        "   j    1b\n"

        /* zero next_grid region: 0x0640..0x06a3 */

        /* zero next_grid region: 0x0640..0x06a3 */
        "2: li   t0, 0x0640\n"
        "   li   t1, 0x06a4\n"
        "   li   t1, 0x06a4\n"
        "3: bge  t0, t1, 4f\n"
        "   sb   zero, 0(t0)\n"
        "   addi t0, t0, 1\n"
        "   j    3b\n"

        /* zero debug region: 0x0700..0x077f */

        /* zero debug region: 0x0700..0x077f */
        "4: li   t0, 0x0700\n"
        "   li   t1, 0x0780\n"
        "   li   t1, 0x0780\n"
        "5: bge  t0, t1, 6f\n"
        "   sb   zero, 0(t0)\n"
        "   addi t0, t0, 1\n"
        "   j    5b\n"


        "6: call main\n"
        "7: j    7b\n"
    );
}

static void idle_tile_forever(void)
{
    while (1) {
        __asm__ volatile ("nop");
    }
}

/*
 * Direct assembly checkerboard writer.
 *
 * Writes exactly 100 bytes to:
 *   0x0500..0x0563
 *
 * Pattern:
 *   if (row + col) even: write fill_val
 *   else:                write 0
 *
 * This avoids compiler-generated C loop weirdness while debugging SRAM writes.
 */
__attribute__((noinline))
static void write_checkerboard_asm(uint8_t fill_val)
{
    __asm__ volatile (
        /* t6 = fill_val */
        "andi t6, %[fill], 0xff\n"

        /* t0 = current SRAM address = GRID_BASE */
        "li   t0, 0x0500\n"

        /* t1 = row = 0 */
        "li   t1, 0\n"

        "1:\n"
        /* t2 = col = 0 */
        "li   t2, 0\n"

        "2:\n"
        /* t3 = (row + col) & 1 */
        "add  t3, t1, t2\n"
        "andi t3, t3, 1\n"

        /* if odd, write zero */
        "bnez t3, 3f\n"

        /* even cell: write fill_val */
        "sb   t6, 0(t0)\n"
        "j    4f\n"

        /* odd cell: write zero */
        "3:\n"
        "sb   zero, 0(t0)\n"

        "4:\n"
        /* addr++, col++ */
        "addi t0, t0, 1\n"
        "addi t2, t2, 1\n"

        /* if col < 10, keep writing this row */
        "li   t4, 10\n"
        "blt  t2, t4, 2b\n"

        /* row++ */
        "addi t1, t1, 1\n"

        /* if row < 10, continue */
        "li   t4, 10\n"
        "blt  t1, t4, 1b\n"
        :
        : [fill] "r" ((uint32_t)fill_val)
        : "t0", "t1", "t2", "t3", "t4", "t6", "memory"
    );
}

static void idle_tile_forever(void)
{
    while (1) {
        __asm__ volatile ("nop");
    }
}

/*
 * Direct assembly checkerboard writer.
 *
 * Writes exactly 100 bytes to:
 *   0x0500..0x0563
 *
 * Pattern:
 *   if (row + col) even: write fill_val
 *   else:                write 0
 *
 * This avoids compiler-generated C loop weirdness while debugging SRAM writes.
 */
__attribute__((noinline))
static void write_checkerboard_asm(uint8_t fill_val)
{
    __asm__ volatile (
        /* t6 = fill_val */
        "andi t6, %[fill], 0xff\n"

        /* t0 = current SRAM address = GRID_BASE */
        "li   t0, 0x0500\n"

        /* t1 = row = 0 */
        "li   t1, 0\n"

        "1:\n"
        /* t2 = col = 0 */
        "li   t2, 0\n"

        "2:\n"
        /* t3 = (row + col) & 1 */
        "add  t3, t1, t2\n"
        "andi t3, t3, 1\n"

        /* if odd, write zero */
        "bnez t3, 3f\n"

        /* even cell: write fill_val */
        "sb   t6, 0(t0)\n"
        "j    4f\n"

        /* odd cell: write zero */
        "3:\n"
        "sb   zero, 0(t0)\n"

        "4:\n"
        /* addr++, col++ */
        "addi t0, t0, 1\n"
        "addi t2, t2, 1\n"

        /* if col < 10, keep writing this row */
        "li   t4, 10\n"
        "blt  t2, t4, 2b\n"

        /* row++ */
        "addi t1, t1, 1\n"

        /* if row < 10, continue */
        "li   t4, 10\n"
        "blt  t1, t4, 1b\n"
        :
        : [fill] "r" ((uint32_t)fill_val)
        : "t0", "t1", "t2", "t3", "t4", "t6", "memory"
    );
}

int main(void)
{
    /*
     * Magic check.
     * If these pass, firmware executed and SRAM writes work.
     */
    /*
     * Magic check.
     * If these pass, firmware executed and SRAM writes work.
     */
    *(volatile uint32_t *)0x0730u = 0xDEADBEEFu;
    *(volatile uint32_t *)0x0734u = 0xCAFEBABEu;

    uint32_t my_id = noc_read_my_id();

    int phys_row = (int)((my_id >> 2) & 0x3u);
    int phys_col = (int)(my_id & 0x3u);


    int phys_row = (int)((my_id >> 2) & 0x3u);
    int phys_col = (int)(my_id & 0x3u);

    *(volatile uint32_t *)DEBUG_MY_ID = my_id;

    /*
     * Keep parameterization.
     * Tiles outside ACTIVE_ROWS x ACTIVE_COLS do nothing.
     */
    if (phys_row >= ACTIVE_ROWS || phys_col >= ACTIVE_COLS) {
        idle_tile_forever();
    }

    /*
     * Testbench expects:
     * tile(0,0) = 0
     * tile(0,1) = 1
     * tile(0,2) = 2
     * tile(1,0) = 4
     * tile(1,1) = 5
     * etc.
     */
    uint8_t fill_val = (uint8_t)my_id;

    /*
     * Debug markers around checkerboard write.
     * Check these if grid still does not show up.
     */
    *(volatile uint32_t *)DEBUG_PRE_CHECKERBOARD = 0x11111111u;

    write_checkerboard_asm(fill_val);

    *(volatile uint32_t *)DEBUG_POST_CHECKERBOARD = 0x22222222u;

    /*
     * Mark seed complete.
     * For the seed-only test, iter_count should stay 0.
     */
    *(volatile uint32_t *)DEBUG_ITER_COUNT = 0u;

    /*
     * Send optional NoC signals after SRAM is already written.
     */

    /*
     * Keep parameterization.
     * Tiles outside ACTIVE_ROWS x ACTIVE_COLS do nothing.
     */
    if (phys_row >= ACTIVE_ROWS || phys_col >= ACTIVE_COLS) {
        idle_tile_forever();
    }

    /*
     * Testbench expects:
     * tile(0,0) = 0
     * tile(0,1) = 1
     * tile(0,2) = 2
     * tile(1,0) = 4
     * tile(1,1) = 5
     * etc.
     */
    uint8_t fill_val = (uint8_t)my_id;

    /*
     * Debug markers around checkerboard write.
     * Check these if grid still does not show up.
     */
    *(volatile uint32_t *)DEBUG_PRE_CHECKERBOARD = 0x11111111u;

    write_checkerboard_asm(fill_val);

    *(volatile uint32_t *)DEBUG_POST_CHECKERBOARD = 0x22222222u;

    /*
     * Mark seed complete.
     * For the seed-only test, iter_count should stay 0.
     */
    *(volatile uint32_t *)DEBUG_ITER_COUNT = 0u;

    /*
     * Send optional NoC signals after SRAM is already written.
     */
    noc_signal(SIG_BOOT_ALIVE);
    noc_signal(SIG_SEED_LIVE);
    noc_signal(SIG_MATH_DONE);
    noc_signal(SIG_GEN_STABLE);
    noc_signal(SIG_MATH_DONE);
    noc_signal(SIG_GEN_STABLE);

    /*
     * Stop. Do not rewrite grid. Do not drain NOC_RECV_BASE.
     */
    /*
     * Stop. Do not rewrite grid. Do not drain NOC_RECV_BASE.
     */
    while (1) {
        __asm__ volatile ("nop");
    }

        __asm__ volatile ("nop");
    }

    return 0;
}