#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <linux/spi/spidev.h>
#include <lgpio.h>

#define DC  25
#define RST 24

// V2: 4 MHz instead of 1 MHz. A full 8192-byte framebuffer is therefore
// transferred much faster, which gives the scene animations/transitions room
// to run smoothly.
#define SPI_SPEED 4000000

static int gpio = -1;
static int spi_fd = -1;

static int spi_write_buf(const uint8_t *buf, size_t len)
{
    struct spi_ioc_transfer tr = {
        .tx_buf = (unsigned long)buf,
        .len = len,
        .speed_hz = SPI_SPEED,
        .bits_per_word = 8,
    };
    return ioctl(spi_fd, SPI_IOC_MESSAGE(1), &tr);
}

static void write_cmd(uint8_t c)
{
    lgGpioWrite(gpio, DC, 0);
    spi_write_buf(&c, 1);
}

static void write_data(const uint8_t *data, size_t len)
{
    lgGpioWrite(gpio, DC, 1);
    size_t offset = 0;
    while (offset < len) {
        size_t chunk = len - offset;
        if (chunk > 4096) chunk = 4096;
        spi_write_buf(data + offset, chunk);
        offset += chunk;
    }
}

static void cmd1(uint8_t c, uint8_t a)
{
    write_cmd(c);
    write_data(&a, 1);
}

static void cmd2(uint8_t c, uint8_t a, uint8_t b)
{
    uint8_t d[2] = {a, b};
    write_cmd(c);
    write_data(d, 2);
}

int oled_init(void)
{
    gpio = lgGpiochipOpen(0);
    if (gpio < 0) return -1;

    lgGpioClaimOutput(gpio, 0, DC, 0);
    lgGpioClaimOutput(gpio, 0, RST, 1);

    spi_fd = open("/dev/spidev0.0", O_RDWR);
    if (spi_fd < 0) return -2;

    uint8_t mode = SPI_MODE_0;
    uint8_t bits = 8;
    uint32_t speed = SPI_SPEED;
    ioctl(spi_fd, SPI_IOC_WR_MODE, &mode);
    ioctl(spi_fd, SPI_IOC_WR_BITS_PER_WORD, &bits);
    ioctl(spi_fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed);

    lgGpioWrite(gpio, RST, 0);
    usleep(300000);
    lgGpioWrite(gpio, RST, 1);
    usleep(300000);

    // Newhaven NHD-5.5-25664UCG3 / SSD1322 init sequence.
    cmd1(0xFD, 0x12);
    cmd1(0xB3, 0xD1);
    cmd1(0xCA, 0x3F);
    cmd1(0xA2, 0x00);
    cmd1(0xA1, 0x00);
    cmd2(0xA0, 0x14, 0x11);
    cmd1(0xB5, 0x00);
    cmd1(0xAB, 0x01);
    cmd2(0xB4, 0xA0, 0xB5);
    cmd1(0xC1, 0x9F);
    cmd1(0xC7, 0x0F);
    write_cmd(0xB9);
    cmd1(0xB1, 0x74);
    cmd2(0xD1, 0xA2, 0x20);
    cmd1(0xBB, 0x17);
    cmd1(0xB6, 0x08);
    cmd1(0xBE, 0x07);
    write_cmd(0xA6);
    write_cmd(0xA9);
    write_cmd(0xAF);
    usleep(100000);
    return 0;
}

void oled_send_frame(uint8_t *frame)
{
    uint8_t cols[2] = {0x1C, 0x5B};
    uint8_t rows[2] = {0x00, 0x3F};
    write_cmd(0x15);
    write_data(cols, 2);
    write_cmd(0x75);
    write_data(rows, 2);
    write_cmd(0x5C);
    write_data(frame, 8192);
}

void oled_close(void)
{
    if (spi_fd >= 0) close(spi_fd);
    if (gpio >= 0) lgGpiochipClose(gpio);
}
