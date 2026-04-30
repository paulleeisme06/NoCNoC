#include <stdint.h>

#define NOC_INJECT_BASE  0x80000000u
#define NOC_RECV_BASE    0x80000004u
#define NOC_ID_BASE      0x80000008u

#define TILE_ID(r,c)     (((uint32_t)(r) << 2) | (uint32_t)(c))
#define FLIT_DEST_SHIFT  28u
#define FLIT_BMAP_MASK   0x3FFu
#define FLIT_VALID_BIT   0x400u

#define SIG_BOOT_ALIVE   0xF0000001u
#define SIG_SEED_LIVE    0xF0000002u
#define SIG_MATH_DONE    0xF0000003u
#define SIG_GEN_STABLE   0xF0000004u

#define SIZE       10
#define MESH_ROWS  3
#define MESH_COLS  3

#define GRID_BASE       0x0500u
#define GHOST_BASE      0x0600u
#define NEXT_GRID_BASE  0x0640u

#define DEBUG_BASE           0x0700u
#define DEBUG_LAST_RECV_N    (DEBUG_BASE +  0)
#define DEBUG_LAST_RECV_S    (DEBUG_BASE +  4)
#define DEBUG_LAST_RECV_W    (DEBUG_BASE +  8)
#define DEBUG_LAST_RECV_E    (DEBUG_BASE + 12)
#define DEBUG_NEIGHBOR_HIST  (DEBUG_BASE + 16)
#define DEBUG_ITER_COUNT     (DEBUG_BASE + 28)
#define DEBUG_GHOST_FLAGS    (DEBUG_BASE + 32)
#define DEBUG_LIVE_COUNT     (DEBUG_BASE + 36)
#define DEBUG_COL0_BM        (DEBUG_BASE + 40)
#define DEBUG_MY_ID          (DEBUG_BASE + 44)
#define DEBUG_SEND_BM        (DEBUG_BASE + 48)
#define DEBUG_ROW_TRACE_BASE (DEBUG_BASE + 52)   /* 0x0734: 10 words, rows 0-9 */
#define DEBUG_ROW8_AT_CALL   (DEBUG_BASE + 52)   /* 0x0734 */
#define DEBUG_ROW9_AT_CALL   (DEBUG_BASE + 56)   /* 0x0738 */

#define grid      ((volatile uint8_t *)GRID_BASE)
#define ghost_N   ((volatile uint8_t *)(GHOST_BASE +  0))
#define ghost_S   ((volatile uint8_t *)(GHOST_BASE + 10))
#define ghost_W   ((volatile uint8_t *)(GHOST_BASE + 20))
#define ghost_E   ((volatile uint8_t *)(GHOST_BASE + 30))
#define next_grid ((volatile uint8_t *)NEXT_GRID_BASE)

#define debug_last_recv_n   ((volatile uint32_t *)DEBUG_LAST_RECV_N)
#define debug_last_recv_s   ((volatile uint32_t *)DEBUG_LAST_RECV_S)
#define debug_last_recv_w   ((volatile uint32_t *)DEBUG_LAST_RECV_W)
#define debug_last_recv_e   ((volatile uint32_t *)DEBUG_LAST_RECV_E)
#define debug_neighbor_hist ((volatile uint8_t  *)DEBUG_NEIGHBOR_HIST)
#define debug_iter_count    ((volatile uint32_t *)DEBUG_ITER_COUNT)
#define debug_ghost_flags   ((volatile uint32_t *)DEBUG_GHOST_FLAGS)
#define debug_send_bm       ((volatile uint32_t *)DEBUG_SEND_BM)

static inline void noc_write(uint32_t word)
{
    *(volatile uint32_t *)NOC_INJECT_BASE = word;
}

static inline uint32_t noc_recv_raw(void)
{
    return *(volatile uint32_t *)NOC_RECV_BASE;
}

static inline uint32_t noc_read_my_id(void)
{
    return *(volatile uint32_t *)NOC_ID_BASE & 0xFu;
}

static inline void noc_signal(uint32_t sig_word)
{
    noc_write(sig_word);
}

static inline uint32_t recv_ghost(void)
{
    uint32_t p;
    do { p = *(volatile uint32_t *)NOC_RECV_BASE; } while (!(p & FLIT_VALID_BIT));
    return p & FLIT_BMAP_MASK;
}

/*__attribute__((noinline))
static uint32_t col_bitmap(int col)
{
    volatile uint8_t *g = (volatile uint8_t *)GRID_BASE;
    uint32_t bm = 0;
    uint32_t i;
    for (i = 0; i < 8; i++) {
        uint32_t cell = g[i * 10 + col];
        uint32_t bit  = cell & 1u;
        uint32_t shifted = bit << i;
        bm = bm | shifted;
    }
    return bm;
}*/

__attribute__((noinline))
static uint32_t col_bitmap_lo(int col)
{
    uint32_t bm = 0;
    int i;
    for (i = 0; i < 8; i++) {
        if (grid[i * SIZE + col] & 1u)
            bm |= (1u << i);   /* shifts 0-7 only, always safe */
    }
    return bm;
}

__attribute__((noinline))
static uint32_t col_bitmap_hi(int col)
{
    uint32_t bm = 0;
    if (grid[8 * SIZE + col] & 1u) bm |= 1u;   /* row 8 → bit 0 */
    if (grid[9 * SIZE + col] & 1u) bm |= 2u;   /* row 9 → bit 1 */
    return bm;
}


__attribute__((noinline))
static int neighbour_count(int row, int col)
{
    int idx   = row * SIZE + col;
    int above = (row > 0);
    int below = (row < SIZE - 1);
    int left  = (col > 0);
    int right = (col < SIZE - 1);
    int n = 0;
    if (above) {
        if (left)  n += grid[idx - SIZE - 1] & 1;
                n += grid[idx - SIZE    ] & 1;
        if (right) n += grid[idx - SIZE + 1] & 1;
    } else {
        if (left)  n += ghost_N[col - 1] & 1;
                n += ghost_N[col    ] & 1;
        if (right) n += ghost_N[col + 1] & 1;
    }
    if (left)  n += grid[idx - 1] & 1;
    else       n += ghost_W[row] & 1;
    if (right) n += grid[idx + 1] & 1;
    else       n += ghost_E[row] & 1;
    if (below) {
        if (left)  n += grid[idx + SIZE - 1] & 1;
                n += grid[idx + SIZE    ] & 1;
        if (right) n += grid[idx + SIZE + 1] & 1;
    } else {
        if (left)  n += ghost_S[col - 1] & 1;
                n += ghost_S[col    ] & 1;
        if (right) n += ghost_S[col + 1] & 1;
    }
    return n;
}

__attribute__((section(".text.init"), naked))
void _start(void)
{
    __asm__ volatile (
        "li   sp, 0x7fc\n"
        /* zero ghost region 0x0600..0x0627 */
        "li   t0, 0x0600\n"
        "li   t1, 0x0628\n"
        "1: bge  t0, t1, 2f\n"
        "   sb   zero, 0(t0)\n"
        "   addi t0, t0, 1\n"
        "   j    1b\n"
        /* zero next_grid region 0x0640..0x06a3 */
        "2: li   t0, 0x0640\n"
        "li   t1, 0x06a4\n"
        "3: bge  t0, t1, 4f\n"
        "   sb   zero, 0(t0)\n"
        "   addi t0, t0, 1\n"
        "   j    3b\n"
        /* zero debug region 0x0700..0x0723 */
        "4: li   t0, 0x0700\n"
        "li   t1, 0x0780\n"
        "5: bge  t0, t1, 6f\n"
        "   sb   zero, 0(t0)\n"
        "   addi t0, t0, 1\n"
        "   j    5b\n"
        "6: call main\n"
        "7: j    7b\n"
    );
}

int main(void)
{
    /* MAGIC CHECK — proves testbench can read what firmware writes to 0x0730 */
    *(volatile uint32_t *)0x0730u = 0xDEADBEEFu;
    *(volatile uint32_t *)0x0734u = 0xCAFEBABEu;

    uint32_t my_id = noc_read_my_id();
    *(volatile uint32_t *)DEBUG_MY_ID = my_id;
    int my_row = (int)((my_id >> 2) & 0x3u);
    int my_col = (int)(my_id & 0x3u);

    noc_signal(SIG_BOOT_ALIVE);

    for (int i = 0; i < SIZE * SIZE; i++) grid[i] = 0u;
    grid[4 * SIZE + 5] = 1; grid[5 * SIZE + 5] = 1; grid[6 * SIZE + 5] = 1;
    grid[8 * SIZE + 0] = 1; grid[9 * SIZE + 0] = 1;
    grid[8 * SIZE + 9] = 1; grid[9 * SIZE + 9] = 1;

    noc_signal(SIG_SEED_LIVE);

    uint32_t iter = 0;
    while (1) {
        *debug_iter_count = iter;

        __sync_synchronize();  

        if (my_col > 0) {
            uint32_t dest = TILE_ID(my_row, my_col - 1);
            //uint32_t bm0  = col_bitmap(0);
            //uint32_t bm0  = 0x300;
            uint32_t bm0_lo = col_bitmap_lo(0);
            uint32_t bm0_hi = col_bitmap_hi(0);
            /* shift hi into bits 8-9 using 8 safe shifts of 1 */
            bm0_hi = bm0_hi << 1; bm0_hi = bm0_hi << 1; bm0_hi = bm0_hi << 1; bm0_hi = bm0_hi << 1;
            bm0_hi = bm0_hi << 1; bm0_hi = bm0_hi << 1; bm0_hi = bm0_hi << 1; bm0_hi = bm0_hi << 1;
            uint32_t bm0 = bm0_lo | bm0_hi;
            noc_write((dest << FLIT_DEST_SHIFT) | FLIT_VALID_BIT | (bm0 & FLIT_BMAP_MASK));
            *debug_ghost_flags |= 0x8;
        }

        if (my_col < MESH_COLS - 1) {
            uint32_t dest = TILE_ID(my_row, my_col + 1);
            //uint32_t bm9  = col_bitmap(SIZE - 1);
            //uint32_t bm9  = 0x300;
            uint32_t bm9_lo = col_bitmap_lo(SIZE - 1);
            uint32_t bm9_hi = col_bitmap_hi(SIZE - 1);
            /* shift hi into bits 8-9 using 8 safe shifts of 1 */
            bm9_hi = bm9_hi << 1; bm9_hi = bm9_hi << 1; bm9_hi = bm9_hi << 1; bm9_hi = bm9_hi << 1;
            bm9_hi = bm9_hi << 1; bm9_hi = bm9_hi << 1; bm9_hi = bm9_hi << 1; bm9_hi = bm9_hi << 1;
            uint32_t bm9 = bm9_lo | bm9_hi;
            noc_write((dest << FLIT_DEST_SHIFT) | FLIT_VALID_BIT | (bm9 & FLIT_BMAP_MASK));
            *debug_ghost_flags |= 0x4;
        }

        if (my_col > 0) {
            uint32_t bmr0 = recv_ghost();
            *debug_last_recv_w = bmr0;
            for (int i = 0; i < SIZE; i++) ghost_W[i] = (bmr0 >> i) & 1u;
        }

        if (my_col < MESH_COLS - 1) {
            uint32_t bmr9 = recv_ghost();
            *debug_last_recv_e = bmr9;
            for (int i = 0; i < SIZE; i++) ghost_E[i] = (bmr9 >> i) & 1u;
        }

        uint8_t neighbor_counts[9] = {0};
        for (int row = 0; row < SIZE; row++) {
            for (int col = 0; col < SIZE; col++) {
                int alive = grid[row * SIZE + col] & 1;
                int n     = neighbour_count(row, col);
                next_grid[row * SIZE + col] =
                    (uint8_t)(alive ? (n == 2 || n == 3) : (n == 3));
                if (n <= 8) neighbor_counts[n]++;
            }
        }
        for (int i = 0; i <= 8; i++)
            debug_neighbor_hist[i] = neighbor_counts[i];

        noc_signal(SIG_MATH_DONE);

        for (int i = 0; i < SIZE * SIZE; i++)
            grid[i] = next_grid[i];

        noc_signal(SIG_GEN_STABLE);
        iter++;
    }
    return 0;
}