/*
 * oled.c — driver do SSD1306 128x64 (baseado no projeto display_oled do Diego, com a altura de 64
 * linhas, fonte completa e envio só quando a imagem muda).
 */
#include "oled.h"

#include <string.h>

#include "config.h"
#include "fonte5x7.h"
#include "hardware/i2c.h"
#include "pico/stdlib.h"

#define I2C_OLED       i2c1
#define PAGINAS        (OLED_ALTURA / 8)
#define TAMANHO_TELA   (OLED_LARGURA * PAGINAS)
#define LIMITE_I2C_US  50000 /* display desligado/solto não trava a placa */

static uint8_t tela[TAMANHO_TELA];          /* rascunho: cada byte = 8 pixels em pé */
static uint8_t enviada[TAMANHO_TELA];       /* o que o display está mostrando */
static uint8_t pacote[TAMANHO_TELA + 1];    /* 0x40 ("são dados") + a tela */
static bool presente = false, ja_enviou = false;

static bool comando(uint8_t c) {
    uint8_t dados[2] = {0x00, c}; /* 0x00 = "o próximo byte é comando" */
    return i2c_write_timeout_us(I2C_OLED, OLED_ENDERECO, dados, 2, false, LIMITE_I2C_US) == 2;
}

bool oled_iniciar(void) {
    i2c_init(I2C_OLED, 400 * 1000);
    gpio_set_function(PINO_OLED_SDA, GPIO_FUNC_I2C);
    gpio_set_function(PINO_OLED_SCL, GPIO_FUNC_I2C);
    gpio_pull_up(PINO_OLED_SDA);
    gpio_pull_up(PINO_OLED_SCL);
    static const uint8_t INICIO[] = {
        0xAE,       /* desliga enquanto configura */
        0x20, 0x00, /* endereçamento horizontal: a tela inteira de uma vez */
        0x40,       /* começa na linha 0 */
        0xA1,       /* espelha as colunas (como a BitDogLab monta o display) */
        0xA8, 0x3F, /* 64 linhas */
        0xC8,       /* varre de baixo para cima (idem) */
        0xD3, 0x00, /* sem deslocamento */
        0xDA, 0x12, /* ligação das linhas para 128x64 */
        0xD5, 0x80, /* relógio interno */
        0xD9, 0xF1, /* pré-carga */
        0xDB, 0x30, /* nível VCOMH */
        0x81, 0xCF, /* contraste */
        0xA4,       /* mostra a RAM */
        0xA6,       /* normal (não invertido) */
        0x8D, 0x14, /* liga a bomba de carga interna */
        0x2E,       /* sem rolagem */
        0xAF,       /* liga */
    };
    presente = true;
    for (size_t i = 0; i < sizeof INICIO && presente; i++) {
        presente = comando(INICIO[i]);
    }
    oled_limpar();
    ja_enviou = false;
    oled_atualizar();
    return presente;
}

void oled_limpar(void) { memset(tela, 0, sizeof tela); }

void oled_pixel(int x, int y, bool aceso) {
    if (x < 0 || x >= OLED_LARGURA || y < 0 || y >= OLED_ALTURA) {
        return;
    }
    uint8_t *byte = &tela[(y / 8) * OLED_LARGURA + x];
    if (aceso) {
        *byte |= (uint8_t)(1u << (y % 8));
    } else {
        *byte &= (uint8_t)~(1u << (y % 8));
    }
}

void oled_retangulo(int x, int y, int largura, int altura, bool aceso) {
    for (int i = x; i < x + largura; i++) {
        for (int j = y; j < y + altura; j++) {
            oled_pixel(i, j, aceso);
        }
    }
}

void oled_inverter(int x, int y, int largura, int altura) {
    for (int i = x; i < x + largura; i++) {
        for (int j = y; j < y + altura; j++) {
            if (i >= 0 && i < OLED_LARGURA && j >= 0 && j < OLED_ALTURA) {
                tela[(j / 8) * OLED_LARGURA + i] ^= (uint8_t)(1u << (j % 8));
            }
        }
    }
}

int oled_largura_texto(const char *texto, int escala) {
    int n = (int)strlen(texto);
    return n == 0 ? 0 : n * (FONTE_LARGURA + 1) * escala - escala;
}

static void letra(int x, int y, char c, int escala) {
    if (c < FONTE_PRIMEIRO || c > FONTE_ULTIMO) {
        c = '?';
    }
    const uint8_t *colunas = FONTE5X7[c - FONTE_PRIMEIRO];
    for (int coluna = 0; coluna < FONTE_LARGURA; coluna++) {
        for (int linha = 0; linha < FONTE_ALTURA; linha++) {
            if (colunas[coluna] & (1u << linha)) {
                oled_retangulo(x + coluna * escala, y + linha * escala, escala, escala, true);
            }
        }
    }
}

void oled_texto(int x, int y, const char *texto, int escala) {
    for (; *texto; texto++) {
        letra(x, y, *texto, escala);
        x += (FONTE_LARGURA + 1) * escala;
    }
}

int oled_texto_centro(int y, const char *texto, int escala) {
    if (escala > 1 && oled_largura_texto(texto, escala) > OLED_LARGURA) {
        escala = 1;
    }
    int x = (OLED_LARGURA - oled_largura_texto(texto, escala)) / 2;
    oled_texto(x < 0 ? 0 : x, y, texto, escala);
    return escala;
}

void oled_atualizar(void) {
    if (!presente || (ja_enviou && memcmp(tela, enviada, sizeof tela) == 0)) {
        return; /* nada mudou: não ocupa o I2C */
    }
    static const uint8_t JANELA[] = {0x21, 0, OLED_LARGURA - 1, 0x22, 0, PAGINAS - 1};
    for (size_t i = 0; i < sizeof JANELA; i++) {
        if (!comando(JANELA[i])) {
            return;
        }
    }
    pacote[0] = 0x40;
    memcpy(pacote + 1, tela, sizeof tela);
    if (i2c_write_timeout_us(I2C_OLED, OLED_ENDERECO, pacote, sizeof pacote, false, LIMITE_I2C_US * 4) ==
        (int)sizeof pacote) {
        memcpy(enviada, tela, sizeof tela);
        ja_enviou = true;
    }
}
