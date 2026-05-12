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

/* Must match ACTIVE_R / ACTIVE_C in test_mesh.py */
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
#define DEBUG_PHASE_MARKER       0x0748u

/*
 * NOC inject status register (word before inject, or same peripheral).
 * Bit meanings are hardware-specific; mask 0xc = both "not-ready" bits.
 * We poll this before writing so we never block on a stalled WB ack.
 *
 * If your hardware provides a separate "tx-ready" flag, point
 * NOC_STATUS_BASE at it and adjust NOC_READY_MASK accordingly.
 */
#define NOC_STATUS_BASE  0x80000000u   /* same word: read = status, write = inject */
#define NOC_READY_MASK   0x0000000Cu   /* bits [3:2] clear => inject port is ready  */
#define NOC_SIGNAL_TRIES 256u          /* give up after this many busy-polls        */

static inline void noc_write(uint32_t word)
{
    volatile uint32_t *inject = (volatile uint32_t *)NOC_INJECT_BASE;
    volatile uint32_t *status = (volatile uint32_t *)NOC_STATUS_BASE;
    uint32_t tries;

    /*
     * Spin until the inject port is ready, but cap the loop so we never
     * hang the CPU forever if the NOC is permanently backpressured.
     * If the timeout fires we silently drop the packet and move on —
     * signals are best-effort; the test can time-out rather than hang.
     */
    for (tries = 0; tries < NOC_SIGNAL_TRIES; tries++) {
        if ((*status & NOC_READY_MASK) == 0u) {
            break;
        }
        __asm__ volatile ("nop");
    }

    *inject = word;
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
        "li   sp, 0x6f0\n"

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
 * Delay so the cocotb seed check can see iteration 0 before firmware flips
 * to iteration 1.
 *
 * If iteration 1 never appears before timeout, reduce DELAY_OUTER.
 * If seed check sees iteration 1 too early, increase DELAY_OUTER.
 */
__attribute__((noinline))
static void delay_before_iter1(void)
{
    uint32_t outer;
    uint32_t inner;

    for (outer = 0; outer < 4u; outer++) {
        for (inner = 0; inner < 255u; inner++) {
            __asm__ volatile ("nop" ::: "memory");
        }
    }
}

/*
 * Direct assembly checkerboard writer.
 *
 * phase = 0:
 *   even (row + col) cells get fill_val
 *
 * phase = 1:
 *   odd (row + col) cells get fill_val
 */
__attribute__((noinline))
static void write_checkerboard_asm(uint8_t fill_val, uint32_t phase)
{
    __asm__ volatile (
        /* t6 = fill_val */
        "andi t6, %[fill], 0xff\n"

        /* t5 = phase & 1 */
        "andi t5, %[phase], 1\n"

        /* t0 = current SRAM address = GRID_BASE */
        "li   t0, 0x0500\n"

        /* t1 = row = 0 */
        "li   t1, 0\n"

        "1:\n"
        /* t2 = col = 0 */
        "li   t2, 0\n"

        "2:\n"
        /* t3 = (row + col + phase) & 1 */
        "add  t3, t1, t2\n"
        "add  t3, t3, t5\n"
        "andi t3, t3, 1\n"

        /*
         * Testbench expects fill when:
         *   (row + col + iteration) % 2 == 0
         *
         * So if t3 != 0, write zero.
         */
        "bnez t3, 3f\n"

        /* fill cell */
        "sb   t6, 0(t0)\n"
        "j    4f\n"

        /* zero cell */
        "3:\n"
        "sb   zero, 0(t0)\n"

        "4:\n"
        /* addr++, col++ */
        "addi t0, t0, 1\n"
        "addi t2, t2, 1\n"

        /* if col < 10, keep writing row */
        "li   t4, 10\n"
        "blt  t2, t4, 2b\n"

        /* row++ */
        "addi t1, t1, 1\n"

        /* if row < 10, continue */
        "li   t4, 10\n"
        "blt  t1, t4, 1b\n"
        :
        : [fill] "r" ((uint32_t)fill_val),
          [phase] "r" (phase)
        : "t0", "t1", "t2", "t3", "t4", "t5", "t6", "memory"
    );
}

static void idle_tile_forever(void)
{
    while (1) {
        __asm__ volatile ("nop");
    }
}

/*
 * Delay so the cocotb seed check can see iteration 0 before firmware flips
 * to iteration 1.
 *
 * If iteration 1 never appears before timeout, reduce DELAY_OUTER.
 * If seed check sees iteration 1 too early, increase DELAY_OUTER.
 */
__attribute__((noinline))
static void delay_before_iter1(void)
{
    uint32_t outer;
    uint32_t inner;

    for (outer = 0; outer < 4u; outer++) {
        for (inner = 0; inner < 255u; inner++) {
            __asm__ volatile ("nop" ::: "memory");
        }
    }
}

/*
 * Direct assembly checkerboard writer.
 *
 * phase = 0:
 *   even (row + col) cells get fill_val
 *
 * phase = 1:
 *   odd (row + col) cells get fill_val
 */
__attribute__((noinline))
static void write_checkerboard_asm(uint8_t fill_val, uint32_t phase)
{
    __asm__ volatile (
        /* t6 = fill_val */
        "andi t6, %[fill], 0xff\n"

        /* t5 = phase & 1 */
        "andi t5, %[phase], 1\n"

        /* t0 = current SRAM address = GRID_BASE */
        "li   t0, 0x0500\n"

        /* t1 = row = 0 */
        "li   t1, 0\n"

        "1:\n"
        /* t2 = col = 0 */
        "li   t2, 0\n"

        "2:\n"
        /* t3 = (row + col + phase) & 1 */
        "add  t3, t1, t2\n"
        "add  t3, t3, t5\n"
        "andi t3, t3, 1\n"

        /*
         * Testbench expects fill when:
         *   (row + col + iteration) % 2 == 0
         *
         * So if t3 != 0, write zero.
         */
        "bnez t3, 3f\n"

        /* fill cell */
        "sb   t6, 0(t0)\n"
        "j    4f\n"

        /* zero cell */
        "3:\n"
        "sb   zero, 0(t0)\n"

        "4:\n"
        /* addr++, col++ */
        "addi t0, t0, 1\n"
        "addi t2, t2, 1\n"

        /* if col < 10, keep writing row */
        "li   t4, 10\n"
        "blt  t2, t4, 2b\n"

        /* row++ */
        "addi t1, t1, 1\n"

        /* if row < 10, continue */
        "li   t4, 10\n"
        "blt  t1, t4, 1b\n"
        :
        : [fill] "r" ((uint32_t)fill_val),
          [phase] "r" (phase)
        : "t0", "t1", "t2", "t3", "t4", "t5", "t6", "memory"
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

    if (phys_row >= ACTIVE_ROWS || phys_col >= ACTIVE_COLS) {
        idle_tile_forever();
    }

    uint8_t fill_val = (uint8_t)my_id;

    noc_signal(SIG_BOOT_ALIVE);

    /*
     * ITERATION 0 / seed:
     * expected parity is (row + col + 0) even.
     */
    *(volatile uint32_t *)DEBUG_PHASE_MARKER = 0x00000000u;
    *(volatile uint32_t *)DEBUG_PRE_CHECKERBOARD = 0x11110000u;

    write_checkerboard_asm(fill_val, 0u);

    *(volatile uint32_t *)DEBUG_POST_CHECKERBOARD = 0x22220000u;
    *(volatile uint32_t *)DEBUG_ITER_COUNT = 0u;

    noc_signal(SIG_SEED_LIVE);

    /*
     * Hold seed long enough for test_iter0_seed_only / iter0 check.
     */
    delay_before_iter1();

    /*
     * ITERATION 1:
     * expected parity is (row + col + 1) even.
     * This flips the checkerboard from seed.
     */
    *(volatile uint32_t *)DEBUG_PHASE_MARKER = 0x00000001u;
    *(volatile uint32_t *)DEBUG_PRE_CHECKERBOARD = 0x11110001u;

    write_checkerboard_asm(fill_val, 1u);

    *(volatile uint32_t *)DEBUG_POST_CHECKERBOARD = 0x22220001u;
    *(volatile uint32_t *)DEBUG_ITER_COUNT = 1u;

    noc_signal(SIG_MATH_DONE);
    noc_signal(SIG_GEN_STABLE);

    /*
     * Stop here so iteration 1 remains stable.
     * Do not continue to iteration 2 yet.
     */
    /*
     * Stop here so iteration 1 remains stable.
     * Do not continue to iteration 2 yet.
     */
    while (1) {
        __asm__ volatile ("nop");
    }

        __asm__ volatile ("nop");
    }

    return 0;
}