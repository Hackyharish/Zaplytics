/**
 * @file ssd1306.c
 * @brief SSD1306 OLED Display Driver for the CZT Harmonic Analyser.
 * * This driver controls a 128x64 pixel OLED display via an I2C interface 
 * operating at 400 kHz[cite: 475, 534]. It uses a DMA-driven framebuffer 
 * architecture (DMA1 Channel 2) to ensure the DSP pipeline and CPU are 
 * never stalled during screen refreshes[cite: 477, 536].
 */

#include "ssd1306.h"

/* ── SSD1306 command bytes ─────────────────────────────────────────── */
/* * The SSD1306 requires a control byte before the payload to indicate 
 * whether the following bytes are commands or display data.
 */
#define SSD1306_CMD_BYTE        0x00   /* Co=0, D/C#=0 → command stream */
#define SSD1306_DATA_BYTE       0x40   /* Co=0, D/C#=1 → data stream    */

/* ── Internal state ────────────────────────────────────────────────── */
/* * Hardware abstraction layer I2C handle pointer and a volatile busy flag 
 * to prevent overlapping DMA transfers from corrupting the display[cite: 591].
 */
static I2C_HandleTypeDef *_hi2c;
static volatile uint8_t   _txBusy = 0;

/*
 * Frame buffer: 1 control byte + 1024 pixel bytes[cite: 584].
 * Laid out as 8 pages × 128 columns (each byte = 8 vertical pixels).
 * buf[0] is the I2C data-stream control byte (0x40), sent once per DMA transfer[cite: 584].
 * This static buffer allows offloading the 1025-byte transfer entirely to DMA[cite: 260].
 */
static uint8_t _buf[1 + SSD1306_WIDTH * (SSD1306_HEIGHT / 8)];

/* Tiny 5×7 ASCII font (printable chars 0x20–0x7E) */
/* * This compact lookup table maps standard ASCII characters to their visual 
 * representation, conserving memory while providing legible text for 
 * telemetry outputs like Vrms, Irms, and Active Power[cite: 176].
 */
static const uint8_t font5x7[][5] = {
    {0x00,0x00,0x00,0x00,0x00}, /* ' ' */
    {0x00,0x00,0x5F,0x00,0x00}, /* '!' */
    {0x00,0x07,0x00,0x07,0x00}, /* '"' */
    {0x14,0x7F,0x14,0x7F,0x14}, /* '#' */
    {0x24,0x2A,0x7F,0x2A,0x12}, /* '$' */
    {0x23,0x13,0x08,0x64,0x62}, /* '%' */
    {0x36,0x49,0x55,0x22,0x50}, /* '&' */
    {0x00,0x05,0x03,0x00,0x00}, /* ''' */
    {0x00,0x1C,0x22,0x41,0x00}, /* '(' */
    {0x00,0x41,0x22,0x1C,0x00}, /* ')' */
    {0x08,0x2A,0x1C,0x2A,0x08}, /* '*' */
    {0x08,0x08,0x3E,0x08,0x08}, /* '+' */
    {0x00,0x50,0x30,0x00,0x00}, /* ',' */
    {0x08,0x08,0x08,0x08,0x08}, /* '-' */
    {0x00,0x60,0x60,0x00,0x00}, /* '.' */
    {0x20,0x10,0x08,0x04,0x02}, /* '/' */
    {0x3E,0x51,0x49,0x45,0x3E}, /* '0' */
    {0x00,0x42,0x7F,0x40,0x00}, /* '1' */
    {0x42,0x61,0x51,0x49,0x46}, /* '2' */
    {0x21,0x41,0x45,0x4B,0x31}, /* '3' */
    {0x18,0x14,0x12,0x7F,0x10}, /* '4' */
    {0x27,0x45,0x45,0x45,0x39}, /* '5' */
    {0x3C,0x4A,0x49,0x49,0x30}, /* '6' */
    {0x01,0x71,0x09,0x05,0x03}, /* '7' */
    {0x36,0x49,0x49,0x49,0x36}, /* '8' */
    {0x06,0x49,0x49,0x29,0x1E}, /* '9' */
    {0x00,0x36,0x36,0x00,0x00}, /* ':' */
    {0x00,0x56,0x36,0x00,0x00}, /* ';' */
    {0x08,0x14,0x22,0x41,0x00}, /* '<' */
    {0x14,0x14,0x14,0x14,0x14}, /* '=' */
    {0x00,0x41,0x22,0x14,0x08}, /* '>' */
    {0x02,0x01,0x51,0x09,0x06}, /* '?' */
    {0x32,0x49,0x79,0x41,0x3E}, /* '@' */
    {0x7E,0x11,0x11,0x11,0x7E}, /* 'A' */
    {0x7F,0x49,0x49,0x49,0x36}, /* 'B' */
    {0x3E,0x41,0x41,0x41,0x22}, /* 'C' */
    {0x7F,0x41,0x41,0x22,0x1C}, /* 'D' */
    {0x7F,0x49,0x49,0x49,0x41}, /* 'E' */
    {0x7F,0x09,0x09,0x09,0x01}, /* 'F' */
    {0x3E,0x41,0x49,0x49,0x7A}, /* 'G' */
    {0x7F,0x08,0x08,0x08,0x7F}, /* 'H' */
    {0x00,0x41,0x7F,0x41,0x00}, /* 'I' */
    {0x20,0x40,0x41,0x3F,0x01}, /* 'J' */
    {0x7F,0x08,0x14,0x22,0x41}, /* 'K' */
    {0x7F,0x40,0x40,0x40,0x40}, /* 'L' */
    {0x7F,0x02,0x0C,0x02,0x7F}, /* 'M' */
    {0x7F,0x04,0x08,0x10,0x7F}, /* 'N' */
    {0x3E,0x41,0x41,0x41,0x3E}, /* 'O' */
    {0x7F,0x09,0x09,0x09,0x06}, /* 'P' */
    {0x3E,0x41,0x51,0x21,0x5E}, /* 'Q' */
    {0x7F,0x09,0x19,0x29,0x46}, /* 'R' */
    {0x46,0x49,0x49,0x49,0x31}, /* 'S' */
    {0x01,0x01,0x7F,0x01,0x01}, /* 'T' */
    {0x3F,0x40,0x40,0x40,0x3F}, /* 'U' */
    {0x1F,0x20,0x40,0x20,0x1F}, /* 'V' */
    {0x3F,0x40,0x38,0x40,0x3F}, /* 'W' */
    {0x63,0x14,0x08,0x14,0x63}, /* 'X' */
    {0x07,0x08,0x70,0x08,0x07}, /* 'Y' */
    {0x61,0x51,0x49,0x45,0x43}, /* 'Z' */
    {0x00,0x7F,0x41,0x41,0x00}, /* '[' */
    {0x02,0x04,0x08,0x10,0x20}, /* '\' */
    {0x00,0x41,0x41,0x7F,0x00}, /* ']' */
    {0x04,0x02,0x01,0x02,0x04}, /* '^' */
    {0x40,0x40,0x40,0x40,0x40}, /* '_' */
    {0x00,0x01,0x02,0x04,0x00}, /* '`' */
    {0x20,0x54,0x54,0x54,0x78}, /* 'a' */
    {0x7F,0x48,0x44,0x44,0x38}, /* 'b' */
    {0x38,0x44,0x44,0x44,0x20}, /* 'c' */
    {0x38,0x44,0x44,0x48,0x7F}, /* 'd' */
    {0x38,0x54,0x54,0x54,0x18}, /* 'e' */
    {0x08,0x7E,0x09,0x01,0x02}, /* 'f' */
    {0x0C,0x52,0x52,0x52,0x3E}, /* 'g' */
    {0x7F,0x08,0x04,0x04,0x78}, /* 'h' */
    {0x00,0x44,0x7D,0x40,0x00}, /* 'i' */
    {0x20,0x40,0x44,0x3D,0x00}, /* 'j' */
    {0x7F,0x10,0x28,0x44,0x00}, /* 'k' */
    {0x00,0x41,0x7F,0x40,0x00}, /* 'l' */
    {0x7C,0x04,0x18,0x04,0x78}, /* 'm' */
    {0x7C,0x08,0x04,0x04,0x78}, /* 'n' */
    {0x38,0x44,0x44,0x44,0x38}, /* 'o' */
    {0x7C,0x14,0x14,0x14,0x08}, /* 'p' */
    {0x08,0x14,0x14,0x18,0x7C}, /* 'q' */
    {0x7C,0x08,0x04,0x04,0x08}, /* 'r' */
    {0x48,0x54,0x54,0x54,0x20}, /* 's' */
    {0x04,0x3F,0x44,0x40,0x20}, /* 't' */
    {0x3C,0x40,0x40,0x40,0x7C}, /* 'u' */
    {0x1C,0x20,0x40,0x20,0x1C}, /* 'v' */
    {0x3C,0x40,0x30,0x40,0x3C}, /* 'w' */
    {0x44,0x28,0x10,0x28,0x44}, /* 'x' */
    {0x0C,0x50,0x50,0x50,0x3C}, /* 'y' */
    {0x44,0x64,0x54,0x4C,0x44}, /* 'z' */
    {0x00,0x08,0x36,0x41,0x00}, /* '{' */
    {0x00,0x00,0x7F,0x00,0x00}, /* '|' */
    {0x00,0x41,0x36,0x08,0x00}, /* '}' */
    {0x0C,0x02,0x0C,0x10,0x0C}, /* '~' */
};

/* ── Helpers ───────────────────────────────────────────────────────── */

/* * Send a command byte (interrupt mode, short transfer) 
 * Used for sending isolated commands to the OLED controller safely.
 */
static HAL_StatusTypeDef _sendCmd(uint8_t cmd)
{
    uint8_t buf[2] = { SSD1306_CMD_BYTE, cmd };
    /* Wait for any ongoing transfer to avoid bus collisions */
    uint32_t t = HAL_GetTick();
    while (_txBusy && (HAL_GetTick() - t < 10));
    _txBusy = 1;
    return HAL_I2C_Master_Transmit_IT(_hi2c, SSD1306_I2C_ADDR, buf, 2);
}

/* * Send multiple commands (blocking shortcut used only during init) 
 * Iterates through a given array of commands and dispatches them sequentially.
 */
static void _sendCmdList(const uint8_t *cmds, uint8_t len)
{
    for (uint8_t i = 0; i < len; i++) _sendCmd(cmds[i]);
    /* Allow last IT transfer to finish before returning */
    uint32_t t = HAL_GetTick();
    while (_txBusy && (HAL_GetTick() - t < 20));
}

/* ── Public functions ──────────────────────────────────────────────── */

/*
 * @brief Initializes the OLED display with the required charge pump and multiplexer settings.
 * @param hi2c Pointer to the pre-configured I2C peripheral handle.
 */
void SSD1306_Init(I2C_HandleTypeDef *hi2c)
{
    _hi2c = hi2c;
    _txBusy = 0;

    /* Control byte prefix for the DMA frame buffer; establishes this stream as image data[cite: 584]. */
    _buf[0] = SSD1306_DATA_BYTE;

    HAL_Delay(100); /* wait for display power-on to stabilize */

    /* Standard initialization sequence for a 128x64 SSD1306 controller */
    static const uint8_t initSeq[] = {
        0xAE,       /* display off */
        0xD5, 0x80, /* clock divide / osc freq */
        0xA8, 0x3F, /* mux ratio = 64 */
        0xD3, 0x00, /* display offset = 0 */
        0x40,       /* start line = 0 */
        0x8D, 0x14, /* charge pump ON */
        0x20, 0x00, /* horizontal addressing mode */
        0xA1,       /* seg remap (mirror X) */
        0xC8,       /* com scan dec (mirror Y) */
        0xDA, 0x12, /* com pins config */
        0x81, 0xCF, /* contrast */
        0xD9, 0xF1, /* pre-charge */
        0xDB, 0x40, /* vcomh deselect */
        0xA4,       /* entire display on (RAM content) */
        0xA6,       /* normal display (not inverted) */
        0xAF,       /* display ON */
    };
    _sendCmdList(initSeq, sizeof(initSeq));

    SSD1306_Clear();
    SSD1306_UpdateScreen();
}

/*
 * @brief Wipes the internal SRAM framebuffer memory clean. 
 * Note: Does not push the clear to the screen immediately; requires SSD1306_UpdateScreen().
 */
void SSD1306_Clear(void)
{
    memset(&_buf[1], 0x00, SSD1306_WIDTH * (SSD1306_HEIGHT / 8));
}

/*
 * @brief Fills the entire framebuffer with a specific bit pattern.
 */
void SSD1306_Fill(uint8_t pattern)
{
    memset(&_buf[1], pattern, SSD1306_WIDTH * (SSD1306_HEIGHT / 8));
}

/*
 * @brief Safely alters a single bit in the 1024-byte framebuffer corresponding to (x,y)[cite: 585].
 * @param color Non-zero turns the pixel ON, zero turns it OFF.
 */
void SSD1306_DrawPixel(uint8_t x, uint8_t y, uint8_t color)
{
    if (x >= SSD1306_WIDTH || y >= SSD1306_HEIGHT) return;
    
    /* Calculate the 1D buffer index based on 8-pixel vertical pages[cite: 586]. */
    uint16_t idx = 1 + x + (y / 8) * SSD1306_WIDTH;
    if (color)
        _buf[idx] |=  (1 << (y % 8));
    else
        _buf[idx] &= ~(1 << (y % 8));
}

/*
 * @brief Renders a null-terminated string onto a specific page (row) and column.
 * @param page The vertical row (0 to 7) corresponding to the display layout[cite: 597].
 */
void SSD1306_WriteString(uint8_t x, uint8_t page, const char *str)
{
    /* page: 0-7, x: pixel column */
    while (*str && x + 5 <= SSD1306_WIDTH) {
        uint8_t c = (uint8_t)*str++;
        
        /* Map unsupported characters to '?' */
        if (c < 0x20 || c > 0x7E) c = '?';
        const uint8_t *glyph = font5x7[c - 0x20];
        
        /* Draw the 5 columns of the current character */
        for (uint8_t col = 0; col < 5; col++) {
            _buf[1 + x + page * SSD1306_WIDTH] = glyph[col];
            x++;
        }
        /* 1-pixel gap between characters for readability */
        _buf[1 + x + page * SSD1306_WIDTH] = 0x00;
        x++;
    }
}

/*
 * @brief Fires the DMA transfer to push the 1025-byte framebuffer to the OLED.
 * This function triggers the transfer and returns immediately, freeing the CPU[cite: 477, 590].
 */
void SSD1306_UpdateScreen(void)
{
    /* Set column and page address back to (0,0) before each DMA burst[cite: 590]. */
    static const uint8_t addrReset[] = {
        0x21, 0x00, 0x7F,   /* column address: 0–127 */
        0x22, 0x00, 0x07,   /* page address:   0–7   */
    };
    _sendCmdList(addrReset, sizeof(addrReset));

    /* Kick off DMA transfer of the entire frame buffer (1025 bytes) */
    uint32_t t = HAL_GetTick();
    while (_txBusy && (HAL_GetTick() - t < 20));
    _txBusy = 1;
    
    /* Initiates non-blocking I2C transfer via DMA1 Channel 2[cite: 260]. */
    HAL_I2C_Master_Transmit_DMA(_hi2c,
                                 SSD1306_I2C_ADDR,
                                 _buf,
                                 sizeof(_buf));
}

/*
 * @brief Checks if the display is ready to accept a new framebuffer transfer.
 * @return 1 if idle, 0 if a DMA or IT transfer is currently active[cite: 729].
 */
uint8_t SSD1306_IsReady(void)
{
    return !_txBusy;
}

/*
 * @brief I2C transmit complete callback triggered by the DMA interrupt.
 * This clears the busy flag once the 1025 bytes have successfully clocked out[cite: 591].
 */
void SSD1306_TxCpltCallback(I2C_HandleTypeDef *hi2c)
{
    if (hi2c->Instance == _hi2c->Instance)
        _txBusy = 0;
}